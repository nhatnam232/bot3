"""
utils/backup.py — Backup & restore cấu trúc server (roles, channels, permissions).
- Backup ra file JSON trong data/backups/
- Restore: tạo lại + cập nhật tên/quyền/position cho role/channel bị sửa/xóa sau nuke

Cải tiến so với bản cũ:
  - So theo ID thay vì tên → phát hiện đổi tên chính xác
  - Restore position sau khi tạo → đúng thứ tự
  - Restore tên + overwrites của kênh/role bị sửa (không chỉ bù phần thiếu)
"""

import os
import json
import time
import logging
import asyncio

import discord

import config

log = logging.getLogger("bot.backup")


# ============================================================
# 💾 BACKUP — chụp lại cấu trúc guild ra JSON
# ============================================================
def _serialize_overwrites(channel) -> list:
    """Chuyển permission overwrites thành list JSON-able."""
    result = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        result.append({
            "type": "role" if isinstance(target, discord.Role) else "member",
            "id": target.id,
            "name": getattr(target, "name", str(target)),
            "allow": allow.value,
            "deny": deny.value,
        })
    return result


async def create_backup(guild: discord.Guild) -> str | None:
    """
    Backup roles + channels + permissions của guild ra file JSON.
    Trả về đường dẫn file (None nếu lỗi).
    """
    try:
        data = {
            "guild_id": guild.id,
            "guild_name": guild.name,
            "created_at": int(time.time()),
            "roles": [],
            "categories": [],
            "channels": [],
        }

        # Roles (bỏ @everyone và role do bot/integration quản lý)
        for role in guild.roles:
            if role.is_default() or role.managed:
                continue
            data["roles"].append({
                "id": role.id,
                "name": role.name,
                "permissions": role.permissions.value,
                "color": role.color.value,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
                "position": role.position,
            })

        # Categories
        for cat in guild.categories:
            data["categories"].append({
                "id": cat.id,
                "name": cat.name,
                "position": cat.position,
                "overwrites": _serialize_overwrites(cat),
            })

        # Channels (text + voice)
        for channel in guild.channels:
            if isinstance(channel, discord.CategoryChannel):
                continue
            entry = {
                "id": channel.id,
                "name": channel.name,
                "type": str(channel.type),
                "position": channel.position,
                "category_id": channel.category_id,
                "overwrites": _serialize_overwrites(channel),
            }
            if isinstance(channel, discord.TextChannel):
                entry["topic"] = channel.topic
                entry["slowmode"] = channel.slowmode_delay
                entry["nsfw"] = channel.is_nsfw()
            data["channels"].append(entry)

        # Ghi file: backups/<guild_id>_<timestamp>.json
        os.makedirs(config.BACKUP_DIR, exist_ok=True)
        file_path = os.path.join(
            config.BACKUP_DIR, f"{guild.id}_{data['created_at']}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # Dọn backup cũ: giữ tối đa 10 file / guild
        _cleanup_old_backups(guild.id, keep=10)

        log.info("Backup created for guild %s: %s", guild.id, file_path)
        return file_path
    except Exception:
        log.exception("Failed to create backup for guild %s", guild.id)
        return None


def _cleanup_old_backups(guild_id: int, keep: int = 10):
    """Xóa backup cũ, chỉ giữ `keep` file mới nhất của guild."""
    try:
        files = sorted(
            f for f in os.listdir(config.BACKUP_DIR)
            if f.startswith(f"{guild_id}_") and f.endswith(".json")
        )
        for old in files[:-keep]:
            os.remove(os.path.join(config.BACKUP_DIR, old))
    except Exception:
        log.exception("Failed to cleanup old backups")


# ============================================================
# ♻️ RESTORE — tạo lại + sửa role/channel theo snapshot (100% accurate)
# ============================================================
async def restore_from_backup(guild: discord.Guild, file_path: str) -> dict:
    """
    Restore guild từ file backup:
    - So theo ID (không phải tên) → phát hiện đúng kể cả khi bị đổi tên
    - Tạo lại thứ bị XÓA
    - Sửa lại thứ bị ĐỔI TÊN / ĐỔI QUYỀN
    - Restore position đúng thứ tự
    Trả về dict thống kê {roles, categories, channels, updated}.
    """
    stats = {"roles": 0, "categories": 0, "channels": 0, "updated": 0}
    try:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log.exception("Cannot read backup file %s", file_path)
        return stats

    try:
        # ================================================================
        # 1. RESTORE ROLES
        # ================================================================
        # Map id cũ → role object hiện tại (dùng lại khi build overwrites)
        role_map: dict[int, discord.Role] = {}

        # Index role hiện tại theo id
        current_roles = {r.id: r for r in guild.roles}

        for r in sorted(data.get("roles", []), key=lambda x: x["position"]):
            existing = current_roles.get(r["id"])

            if existing:
                role_map[r["id"]] = existing
                # Kiểm tra có bị sửa không → patch lại
                needs_update = (
                    existing.name != r["name"]
                    or existing.permissions.value != r["permissions"]
                    or existing.color.value != r["color"]
                    or existing.hoist != r["hoist"]
                    or existing.mentionable != r["mentionable"]
                )
                if needs_update:
                    try:
                        await existing.edit(
                            name=r["name"],
                            permissions=discord.Permissions(r["permissions"]),
                            color=discord.Color(r["color"]),
                            hoist=r["hoist"],
                            mentionable=r["mentionable"],
                            reason="Restore từ backup (anti-nuke)",
                        )
                        stats["updated"] += 1
                    except discord.HTTPException:
                        pass
            else:
                # Role bị xóa → tạo lại
                try:
                    new_role = await guild.create_role(
                        name=r["name"],
                        permissions=discord.Permissions(r["permissions"]),
                        color=discord.Color(r["color"]),
                        hoist=r["hoist"],
                        mentionable=r["mentionable"],
                        reason="Restore từ backup (anti-nuke)",
                    )
                    role_map[r["id"]] = new_role
                    stats["roles"] += 1
                except discord.HTTPException:
                    continue

        # Restore position roles (batch move từ thấp lên cao)
        positions: dict[discord.Role, int] = {}
        for r in data.get("roles", []):
            role_obj = role_map.get(r["id"])
            if role_obj:
                positions[role_obj] = r["position"]
        if positions:
            try:
                await guild.edit_role_positions(positions, reason="Restore positions (anti-nuke)")
            except discord.HTTPException:
                pass

        # ================================================================
        # 2. HELPERS
        # ================================================================
        def _build_overwrites(raw_list) -> dict:
            overwrites = {}
            for ow in raw_list:
                if ow["type"] == "role":
                    target = role_map.get(ow["id"]) or guild.get_role(ow["id"])
                    if target is None and ow["id"] == data["guild_id"]:
                        target = guild.default_role
                else:
                    target = guild.get_member(ow["id"])
                if target is None:
                    continue
                overwrites[target] = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(ow["allow"]),
                    discord.Permissions(ow["deny"]),
                )
            return overwrites

        async def _sync_overwrites(obj, raw_list):
            """Cập nhật overwrites nếu khác snapshot."""
            expected = _build_overwrites(raw_list)
            current = {
                t: discord.PermissionOverwrite.from_pair(*ow.pair())
                for t, ow in obj.overwrites.items()
            }
            if expected != current:
                try:
                    await obj.edit(
                        overwrites=expected,
                        reason="Restore permissions từ backup (anti-nuke)",
                    )
                    return True
                except discord.HTTPException:
                    pass
            return False

        # ================================================================
        # 3. RESTORE CATEGORIES
        # ================================================================
        current_cats = {c.id: c for c in guild.categories}
        cat_map: dict[int, discord.CategoryChannel] = {}

        for c in sorted(data.get("categories", []), key=lambda x: x["position"]):
            existing = current_cats.get(c["id"])

            if existing:
                cat_map[c["id"]] = existing
                # Sửa tên nếu bị đổi
                if existing.name != c["name"]:
                    try:
                        await existing.edit(
                            name=c["name"],
                            reason="Restore từ backup (anti-nuke)",
                        )
                        stats["updated"] += 1
                    except discord.HTTPException:
                        pass
                # Sync overwrites
                if await _sync_overwrites(existing, c["overwrites"]):
                    stats["updated"] += 1
            else:
                try:
                    new_cat = await guild.create_category(
                        name=c["name"],
                        overwrites=_build_overwrites(c["overwrites"]),
                        reason="Restore từ backup (anti-nuke)",
                    )
                    cat_map[c["id"]] = new_cat
                    stats["categories"] += 1
                except discord.HTTPException:
                    continue

        # Restore position categories
        for c in data.get("categories", []):
            cat_obj = cat_map.get(c["id"])
            if cat_obj and cat_obj.position != c["position"]:
                try:
                    await cat_obj.edit(
                        position=c["position"],
                        reason="Restore position (anti-nuke)",
                    )
                except discord.HTTPException:
                    pass

        # ================================================================
        # 4. RESTORE CHANNELS
        # ================================================================
        current_channels = {ch.id: ch for ch in guild.channels
                            if not isinstance(ch, discord.CategoryChannel)}

        for ch in sorted(data.get("channels", []), key=lambda x: x["position"]):
            existing = current_channels.get(ch["id"])
            category = cat_map.get(ch.get("category_id")) or (
                guild.get_channel(ch["category_id"])
                if ch.get("category_id") else None
            )

            if existing:
                # Sửa tên nếu bị đổi
                edits = {}
                if existing.name != ch["name"]:
                    edits["name"] = ch["name"]
                if isinstance(existing, discord.TextChannel):
                    if existing.topic != ch.get("topic"):
                        edits["topic"] = ch.get("topic")
                    if existing.slowmode_delay != ch.get("slowmode", 0):
                        edits["slowmode_delay"] = ch.get("slowmode", 0)
                    if existing.is_nsfw() != ch.get("nsfw", False):
                        edits["nsfw"] = ch.get("nsfw", False)
                if edits:
                    try:
                        await existing.edit(
                            **edits,
                            reason="Restore từ backup (anti-nuke)",
                        )
                        stats["updated"] += 1
                    except discord.HTTPException:
                        pass
                # Sync overwrites
                if await _sync_overwrites(existing, ch["overwrites"]):
                    stats["updated"] += 1
                # Restore position
                if existing.position != ch["position"]:
                    try:
                        await existing.edit(
                            position=ch["position"],
                            reason="Restore position (anti-nuke)",
                        )
                    except discord.HTTPException:
                        pass
            else:
                # Channel bị xóa → tạo lại
                overwrites = _build_overwrites(ch["overwrites"])
                try:
                    if ch["type"] == "voice":
                        new_ch = await guild.create_voice_channel(
                            name=ch["name"], category=category,
                            overwrites=overwrites,
                            reason="Restore từ backup (anti-nuke)",
                        )
                    else:
                        new_ch = await guild.create_text_channel(
                            name=ch["name"], category=category,
                            overwrites=overwrites,
                            topic=ch.get("topic"),
                            slowmode_delay=ch.get("slowmode", 0),
                            nsfw=ch.get("nsfw", False),
                            reason="Restore từ backup (anti-nuke)",
                        )
                    # Set position sau khi tạo
                    try:
                        await new_ch.edit(
                            position=ch["position"],
                            reason="Restore position (anti-nuke)",
                        )
                    except discord.HTTPException:
                        pass
                    stats["channels"] += 1
                except discord.HTTPException:
                    continue

        log.info("Restore done for guild %s: %s", guild.id, stats)
    except Exception:
        log.exception("Error during restore for guild %s", guild.id)
    return stats