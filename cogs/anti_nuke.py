"""
cogs/anti_nuke.py — Module chống nuke (phá hoại server).
Cơ chế (đếm hành động per-actor qua audit log):
  1. Xóa 3 channel / 10s → ban actor + log
  2. Xóa 3 role / 10s → ban actor + log
  3. Tạo 5 channel/role / 10s → revoke + log
  4. Đổi tên/avatar server 3 lần / 1 phút → revoke + log
  5. Cấp quyền admin đột ngột → revoke role admin ngay + log
  6. Add bot không whitelist → kick bot + log
  7. Tạo 3 webhook / 1 phút → xóa webhook + revoke + log
  8. Backup tự động mỗi 6 giờ; auto-restore sau khi detect nuke
- Owner/Co-owner + whitelist bypass (nhưng VẪN được log để audit)
"""

import time
import logging
from collections import defaultdict, deque

import discord
from discord.ext import commands, tasks

import config
from utils import backup as backup_util
from utils.audit_cache import audit_cache

log = logging.getLogger("bot.anti_nuke")


class AntiNuke(commands.Cog):
    """Cog chống nuke — theo dõi hành động phá hoại qua audit log."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        # Đếm hành động: {(guild_id, actor_id, action_type): deque[timestamp]}
        self._actions: dict = defaultdict(lambda: deque(maxlen=50))
        # Actor đã bị xử lý gần đây (tránh revoke lặp): {(guild_id, actor_id): timestamp}
        self._punished: dict = {}
        self.backup_task.start()

    def cog_unload(self):
        self.backup_task.cancel()

    # ========================================================
    # 🔧 HELPERS
    # ========================================================
    async def _is_bypassed(self, guild_id: int, user_id: int) -> bool:
        """Owner/Co-owner + whitelist bypass anti-nuke (vẫn log)."""
        if user_id == self.bot.user.id:
            return True  # hành động của chính bot (restore...) không tự trigger
        if config.is_owner_or_coowner(user_id):
            return True
        return await self.db.is_whitelisted(guild_id, user_id)

    async def _log(self, guild, title, desc, color=discord.Color.red(), **kw):
        logger = self.bot.get_cog("Logger")
        if logger:
            await logger.send_log(guild, title, desc, color, **kw)

    async def _find_actor(
        self, guild: discord.Guild, action: discord.AuditLogAction,
        target_id: int = None,
    ) -> discord.Member | None:
        """Tra audit log tìm ai vừa thực hiện hành động (entry < 10s tuổi).
        
        Dùng audit_cache dùng chung — tránh rate limit 429 khi nhiều cog
        cùng gọi audit log trong 1 sự kiện.
        """
        entries = await audit_cache.get_entries(guild, action)
        for entry in entries:
            # Entry quá cũ → không phải hành động này
            if (discord.utils.utcnow() - entry.created_at).total_seconds() > 10:
                continue
            if target_id and entry.target and entry.target.id != target_id:
                continue
            if isinstance(entry.user, discord.Member):
                return entry.user
            return guild.get_member(entry.user.id) if entry.user else None
        return None

    def _count_action(self, guild_id: int, actor_id: int, action: str) -> int:
        """Ghi nhận 1 hành động, trả về tổng trong deque (lọc window ở caller)."""
        key = (guild_id, actor_id, action)
        self._actions[key].append(time.time())
        return len(self._actions[key])

    def _recent_count(self, guild_id: int, actor_id: int, action: str, window: int) -> int:
        """Đếm hành động trong window giây."""
        key = (guild_id, actor_id, action)
        now = time.time()
        return sum(1 for ts in self._actions[key] if now - ts <= window)

    # ========================================================
    # ⚔️ TRỪNG PHẠT — revoke toàn bộ quyền kẻ phá hoại
    # ========================================================
    async def _punish_nuker(self, guild: discord.Guild, actor: discord.Member, reason: str):
        """Gỡ toàn bộ role có quyền nguy hiểm của kẻ phá + log + auto restore."""
        # Chống xử lý lặp trong 60s
        key = (guild.id, actor.id)
        now = time.time()
        if now - self._punished.get(key, 0) < 60:
            return
        self._punished[key] = now

        removed_roles = []
        try:
            # Gỡ mọi role có quyền nguy hiểm (admin, manage...)
            dangerous = []
            for role in actor.roles:
                if role.is_default():
                    continue
                p = role.permissions
                if (p.administrator or p.manage_guild or p.manage_channels
                        or p.manage_roles or p.manage_webhooks or p.ban_members
                        or p.kick_members):
                    dangerous.append(role)
            if dangerous:
                await actor.remove_roles(*dangerous, reason=f"ANTI-NUKE: {reason}")
                removed_roles = [r.name for r in dangerous]
        except discord.Forbidden:
            log.warning("Cannot remove roles from %s (hierarchy?)", actor)
        except Exception:
            log.exception("Error punishing nuker")

        await self._log(
            guild, "🚨 ANTI-NUKE KÍCH HOẠT",
            f"**{actor.mention}** bị phát hiện phá hoại!\n**Lý do:** {reason}",
            discord.Color.red(),
            fields=[("Role đã thu hồi", ", ".join(removed_roles) or "Không gỡ được (hierarchy)")],
            user=actor,
        )

        # ♻️ Auto restore nếu bật
        if await self.db.get_config(guild.id, "antinuke_auto_restore"):
            await self._auto_restore(guild)

    async def _ban_nuker(self, guild: discord.Guild, actor: discord.Member, reason: str):
        """Ban thẳng kẻ phá hoại (dùng cho xóa channel/role hàng loạt + mention @everyone)."""
        key = (guild.id, actor.id)
        now = time.time()
        if now - self._punished.get(key, 0) < 60:
            return
        self._punished[key] = now

        try:
            await guild.ban(actor, reason=f"ANTI-NUKE: {reason}", delete_message_days=1)
        except discord.Forbidden:
            log.warning("Cannot ban %s (hierarchy?)", actor)
            # Fallback: revoke roles nếu không ban được
            await self._punish_nuker(guild, actor, reason)
            return
        except Exception:
            log.exception("Error banning nuker")
            return

        await self._log(
            guild, "🔨 ANTI-NUKE: BAN",
            f"**{actor.mention}** bị **BAN** vì phá hoại!\n**Lý do:** {reason}",
            discord.Color.dark_red(),
            user=actor,
        )

        if await self.db.get_config(guild.id, "antinuke_auto_restore"):
            await self._auto_restore(guild)

    async def _auto_restore(self, guild: discord.Guild):
        """Restore từ backup mới nhất sau khi detect nuke."""
        try:
            file_path = await self.db.get_latest_backup(guild.id)
            if not file_path:
                await self._log(
                    guild, "♻️ Auto-restore",
                    "Không có backup nào để restore!", discord.Color.orange())
                return
            stats = await backup_util.restore_from_backup(guild, file_path)
            await self._log(
                guild, "♻️ Auto-restore hoàn tất",
                f"Đã tạo lại: **{stats['roles']}** role, "
                f"**{stats['categories']}** category, **{stats['channels']}** channel.",
                discord.Color.green(),
            )
        except Exception:
            log.exception("Error in auto restore")

    # ========================================================
    # 1️⃣ XÓA CHANNEL HÀNG LOẠT
    # ========================================================
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        try:
            guild = channel.guild
            if not await self.db.is_module_enabled(guild.id, "antinuke"):
                return
            actor = await self._find_actor(guild, discord.AuditLogAction.channel_delete)
            if actor is None or await self._is_bypassed(guild.id, actor.id):
                return

            limit = await self.db.get_config(guild.id, "antinuke_channel_delete_limit")
            window = await self.db.get_config(guild.id, "antinuke_channel_delete_window")
            self._count_action(guild.id, actor.id, "channel_delete")
            if self._recent_count(guild.id, actor.id, "channel_delete", window) >= limit:
                await self._ban_nuker(
                    guild, actor, f"Xóa {limit}+ channel trong {window}s")
        except Exception:
            log.exception("Error in anti-nuke channel delete")

    # ========================================================
    # 2️⃣ XÓA ROLE HÀNG LOẠT
    # ========================================================
    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        try:
            guild = role.guild
            if not await self.db.is_module_enabled(guild.id, "antinuke"):
                return
            actor = await self._find_actor(guild, discord.AuditLogAction.role_delete)
            if actor is None or await self._is_bypassed(guild.id, actor.id):
                return

            limit = await self.db.get_config(guild.id, "antinuke_role_delete_limit")
            window = await self.db.get_config(guild.id, "antinuke_role_delete_window")
            self._count_action(guild.id, actor.id, "role_delete")
            if self._recent_count(guild.id, actor.id, "role_delete", window) >= limit:
                await self._ban_nuker(
                    guild, actor, f"Xóa {limit}+ role trong {window}s")
        except Exception:
            log.exception("Error in anti-nuke role delete")

    # ========================================================
    # 3️⃣ TẠO CHANNEL/ROLE BẤT THƯỜNG
    # ========================================================
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        await self._handle_create(channel.guild, discord.AuditLogAction.channel_create)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        await self._handle_create(role.guild, discord.AuditLogAction.role_create)

    async def _handle_create(self, guild: discord.Guild, audit_action):
        try:
            if not await self.db.is_module_enabled(guild.id, "antinuke"):
                return
            actor = await self._find_actor(guild, audit_action)
            if actor is None or await self._is_bypassed(guild.id, actor.id):
                return

            limit = await self.db.get_config(guild.id, "antinuke_create_limit")
            window = await self.db.get_config(guild.id, "antinuke_create_window")
            self._count_action(guild.id, actor.id, "create")
            if self._recent_count(guild.id, actor.id, "create", window) >= limit:
                await self._punish_nuker(
                    guild, actor, f"Tạo {limit}+ channel/role trong {window}s")
        except Exception:
            log.exception("Error in anti-nuke create handler")

    # ========================================================
    # 4️⃣ ĐỔI TÊN/AVATAR SERVER LIÊN TỤC
    # ========================================================
    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        try:
            if before.name == after.name and before.icon == after.icon:
                return
            if not await self.db.is_module_enabled(after.id, "antinuke"):
                return
            actor = await self._find_actor(after, discord.AuditLogAction.guild_update)
            if actor is None or await self._is_bypassed(after.id, actor.id):
                return

            limit = await self.db.get_config(after.id, "antinuke_guild_update_limit")
            window = await self.db.get_config(after.id, "antinuke_guild_update_window")
            self._count_action(after.id, actor.id, "guild_update")
            if self._recent_count(after.id, actor.id, "guild_update", window) >= limit:
                await self._punish_nuker(
                    after, actor, f"Đổi tên/avatar server {limit}+ lần trong {window}s")
        except Exception:
            log.exception("Error in anti-nuke guild update")

    # ========================================================
    # 5️⃣ CẤP QUYỀN ADMIN ĐỘT NGỘT → revoke ngay
    # ========================================================
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        try:
            guild = after.guild
            if not await self.db.is_module_enabled(guild.id, "antinuke"):
                return
            if not await self.db.get_config(guild.id, "antinuke_admin_grant_protect"):
                return

            # Role admin vừa được thêm?
            added = set(after.roles) - set(before.roles)
            admin_roles = [r for r in added if r.permissions.administrator]
            if not admin_roles:
                return

            actor = await self._find_actor(
                guild, discord.AuditLogAction.member_role_update, after.id)
            # Người CẤP nằm trong whitelist → cho phép
            if actor and await self._is_bypassed(guild.id, actor.id):
                return
            # Người NHẬN là Owner/Co-owner/whitelist → cho phép
            if await self._is_bypassed(guild.id, after.id):
                return

            # Revoke ngay role admin vừa cấp
            try:
                await after.remove_roles(
                    *admin_roles, reason="ANTI-NUKE: cấp quyền admin trái phép")
            except discord.Forbidden:
                log.warning("Cannot revoke admin role from %s", after)

            await self._log(
                guild, "🚨 CHẶN CẤP QUYỀN ADMIN",
                f"{after.mention} vừa được cấp role admin **trái phép** — đã thu hồi!",
                discord.Color.red(),
                fields=[
                    ("Role", ", ".join(r.name for r in admin_roles)),
                    ("Người cấp", actor.mention if actor else "Không rõ"),
                ],
                user=after,
            )
            # Trừng phạt người cấp
            if actor:
                await self._punish_nuker(guild, actor, "Cấp quyền admin trái phép")
        except Exception:
            log.exception("Error in anti-nuke admin grant")

    # ========================================================
    # 6️⃣ ADD BOT LẠ → kick
    # ========================================================
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            if not member.bot:
                return
            guild = member.guild
            if not await self.db.is_module_enabled(guild.id, "antinuke"):
                return
            if not await self.db.get_config(guild.id, "antinuke_bot_add_protect"):
                return
            # Bot đã whitelist → cho phép
            if await self.db.is_bot_whitelisted(guild.id, member.id):
                return

            actor = await self._find_actor(
                guild, discord.AuditLogAction.bot_add, member.id)
            # Người add là Owner/Co-owner/whitelist → tự whitelist bot luôn
            if actor and await self._is_bypassed(guild.id, actor.id):
                await self.db.add_whitelist_bot(guild.id, member.id, actor.id)
                return

            try:
                await member.kick(reason="ANTI-NUKE: bot không có trong whitelist")
            except discord.Forbidden:
                log.warning("Cannot kick bot %s", member)
                return

            await self._log(
                guild, "🤖 KICK BOT LẠ",
                f"Bot **{member}** bị kick — không có trong whitelist.",
                discord.Color.red(),
                fields=[("Người add", actor.mention if actor else "Không rõ")],
                user=member,
            )
            if actor:
                await self._punish_nuker(guild, actor, "Add bot lạ không whitelist")
        except Exception:
            log.exception("Error in anti-nuke bot add")

    # ========================================================
    # 7️⃣ WEBHOOK TẠO BẤT THƯỜNG
    # ========================================================
    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        try:
            guild = channel.guild
            if not await self.db.is_module_enabled(guild.id, "antinuke"):
                return
            actor = await self._find_actor(guild, discord.AuditLogAction.webhook_create)
            if actor is None or await self._is_bypassed(guild.id, actor.id):
                return

            limit = await self.db.get_config(guild.id, "antinuke_webhook_limit")
            window = await self.db.get_config(guild.id, "antinuke_webhook_window")
            self._count_action(guild.id, actor.id, "webhook")
            if self._recent_count(guild.id, actor.id, "webhook", window) < limit:
                return

            # Xóa các webhook actor vừa tạo trong channel
            try:
                for wh in await channel.webhooks():
                    if wh.user and wh.user.id == actor.id:
                        await wh.delete(reason="ANTI-NUKE: webhook bất thường")
            except discord.HTTPException:
                pass
            await self._punish_nuker(
                guild, actor, f"Tạo {limit}+ webhook trong {window}s")
        except Exception:
            log.exception("Error in anti-nuke webhook")

    # ========================================================
    # 8️⃣ MENTION @everyone / @here → BAN NGAY
    # ========================================================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            if not message.guild or not message.mention_everyone:
                return
            if not await self.db.is_module_enabled(message.guild.id, "antinuke"):
                return
            if not await self.db.get_config(message.guild.id, "antinuke_mention_protect"):
                return

            actor = message.author
            # Bot / webhook → bỏ qua
            if not isinstance(actor, discord.Member):
                return
            # Bypass cho Owner/Co-owner/whitelist
            if await self._is_bypassed(message.guild.id, actor.id):
                return

            # Xóa tin nhắn trước khi ban
            try:
                await message.delete()
            except discord.HTTPException:
                pass

            await self._ban_nuker(
                message.guild, actor,
                f"Mention @everyone/@here trái phép trong #{message.channel.name}",
            )
        except Exception:
            log.exception("Error in anti-nuke mention check")

    # ========================================================
    # 9️⃣ BACKUP TỰ ĐỘNG — mỗi 6 giờ
    # ========================================================
    @tasks.loop(minutes=30)
    async def backup_task(self):
        """Chạy mỗi 30 phút — guild nào đến hạn (interval riêng) thì backup."""
        try:
            for guild in self.bot.guilds:
                try:
                    if not await self.db.is_module_enabled(guild.id, "antinuke"):
                        continue
                    interval = await self.db.get_config(
                        guild.id, "antinuke_backup_interval")
                    # Check backup gần nhất
                    last_file = await self.db.get_latest_backup(guild.id)
                    if last_file:
                        # Lấy timestamp từ tên file: <guild_id>_<ts>.json
                        try:
                            last_ts = int(last_file.rsplit("_", 1)[1].split(".")[0])
                            if time.time() - last_ts < interval:
                                continue  # chưa đến hạn
                        except (ValueError, IndexError):
                            pass
                    file_path = await backup_util.create_backup(guild)
                    if file_path:
                        await self.db.add_backup(guild.id, file_path)
                except Exception:
                    log.exception("Error backing up guild %s", guild.id)
        except Exception:
            log.exception("Error in backup task")

    @backup_task.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()
        # Backup ngay lần đầu khi bot khởi động (nếu guild chưa có backup nào)
        for guild in self.bot.guilds:
            try:
                if await self.db.get_latest_backup(guild.id) is None:
                    file_path = await backup_util.create_backup(guild)
                    if file_path:
                        await self.db.add_backup(guild.id, file_path)
            except Exception:
                log.exception("Error in initial backup for guild %s", guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiNuke(bot))

# ✅ Done: cogs/anti_nuke.py — Tiếp theo: cogs/config_commands.py + utils/helpers.py