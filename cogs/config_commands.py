"""
cogs/config_commands.py — Slash commands quản trị & cấu hình.
Nhóm lệnh:
  /config view|set|toggle    — xem/chỉnh threshold, bật/tắt module (admin)
  /setlog <channel>          — đặt channel log (admin)
  /whitelist add|remove|list — whitelist user (CHỈ Owner/Co-owner)
  /whitelistbot add|remove   — whitelist bot (CHỈ Owner/Co-owner)
  /blacklist add|remove|list|reload — từ cấm per-guild + reload file global
  /lockdown, /unlockdown     — khóa/mở toàn server (whitelist)
  /lock, /unlock             — khóa/mở 1 channel (mod)
  /backup, /restore          — backup/restore thủ công (Owner/Co-owner)
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from config import DEFAULT_CONFIG
from utils import backup as backup_util
from utils.helpers import is_owner_or_coowner, is_whitelisted, chunk_text

log = logging.getLogger("bot.config_commands")


class ConfigCommands(commands.Cog):
    """Cog chứa toàn bộ lệnh cấu hình & quản trị."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def _log(self, guild, title, desc, color=discord.Color.blue(), **kw):
        logger = self.bot.get_cog("Logger")
        if logger:
            await logger.send_log(guild, title, desc, color, **kw)

    # ========================================================
    # ⚙️ /config — view / set / toggle
    # ========================================================
    config_group = app_commands.Group(
        name="config",
        description="Xem/chỉnh cấu hình bot",
        default_permissions=discord.Permissions(administrator=True),
    )

    @config_group.command(name="view", description="Xem toàn bộ config hiện tại")
    @app_commands.describe(module="Lọc theo module (vd: antispam)")
    async def config_view(self, interaction: discord.Interaction, module: str = ""):
        try:
            lines = []
            for key in sorted(DEFAULT_CONFIG):
                if module and not key.startswith(module):
                    continue
                value = await self.db.get_config(interaction.guild.id, key)
                default = DEFAULT_CONFIG[key]
                mark = "" if value == default else " *(đã chỉnh)*"
                lines.append(f"`{key}` = **{value}**{mark}")
            if not lines:
                return await interaction.response.send_message(
                    f"❌ Không có config nào bắt đầu bằng `{module}`.", ephemeral=True)
            text = "\n".join(lines)
            chunks = chunk_text(text, 3900)
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚙️ Config hiện tại", description=chunks[0],
                    color=discord.Color.blue()),
                ephemeral=True)
            for extra in chunks[1:]:
                await interaction.followup.send(
                    embed=discord.Embed(description=extra, color=discord.Color.blue()),
                    ephemeral=True)
        except Exception:
            log.exception("Error in /config view")

    @config_group.command(name="set", description="Chỉnh 1 giá trị config")
    @app_commands.describe(key="Tên config (xem /config view)", value="Giá trị mới (số)")
    async def config_set(self, interaction: discord.Interaction, key: str, value: int):
        try:
            key = key.strip().lower()
            if key not in DEFAULT_CONFIG:
                return await interaction.response.send_message(
                    f"❌ Config `{key}` không tồn tại. Xem `/config view`.", ephemeral=True)
            await self.db.set_config(interaction.guild.id, key, value)
            await interaction.response.send_message(
                f"✅ Đã đặt `{key}` = **{value}**.", ephemeral=True)
            await self._log(
                interaction.guild, "⚙️ Config thay đổi",
                f"{interaction.user.mention} đặt `{key}` = **{value}**.",
                user=interaction.user)
        except Exception:
            log.exception("Error in /config set")

    @config_group.command(name="toggle", description="Bật/tắt 1 module")
    @app_commands.describe(module="Module cần bật/tắt")
    @app_commands.choices(module=[
        app_commands.Choice(name="🛡️ Anti-Raid", value="antiraid"),
        app_commands.Choice(name="💣 Anti-Nuke", value="antinuke"),
        app_commands.Choice(name="🔁 Anti-Spam", value="antispam"),
        app_commands.Choice(name="🈲 Anti-Content", value="anticontent"),
        app_commands.Choice(name="⚠️ Warn System", value="warn"),
        app_commands.Choice(name="📋 Log", value="log"),
    ])
    async def config_toggle(self, interaction: discord.Interaction, module: str):
        try:
            key = f"{module}_enabled"
            current = await self.db.get_config(interaction.guild.id, key)
            new_value = 0 if current else 1
            await self.db.set_config(interaction.guild.id, key, new_value)
            state = "✅ BẬT" if new_value else "⛔ TẮT"
            await interaction.response.send_message(
                f"Module **{module}** → {state}.", ephemeral=True)
            await self._log(
                interaction.guild, "⚙️ Toggle module",
                f"{interaction.user.mention} {state} module **{module}**.",
                user=interaction.user)
        except Exception:
            log.exception("Error in /config toggle")

    @config_group.command(
        name="reset",
        description="Reset config về mặc định (1 key hoặc toàn bộ)")
    @app_commands.describe(key="Key cần reset (bỏ trống = reset TOÀN BỘ config server này)")
    async def config_reset(self, interaction: discord.Interaction, key: str = ""):
        try:
            key = key.strip().lower()
            if key and key not in DEFAULT_CONFIG:
                return await interaction.response.send_message(
                    f"❌ Config `{key}` không tồn tại.", ephemeral=True)
            await self.db.delete_config(interaction.guild.id, key or None)
            target = f"`{key}`" if key else "**TOÀN BỘ config**"
            await interaction.response.send_message(
                f"♻️ Đã reset {target} của server này về mặc định.", ephemeral=True)
            await self._log(
                interaction.guild, "♻️ Config reset",
                f"{interaction.user.mention} reset {target} về mặc định.",
                user=interaction.user)
        except Exception:
            log.exception("Error in /config reset")

    # ========================================================
    # 📋 /setlog
    # ========================================================
    @app_commands.command(name="setlog", description="Đặt channel nhận log")
    @app_commands.describe(channel="Channel log")
    @app_commands.default_permissions(administrator=True)
    async def setlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        try:
            await self.db.set_config(interaction.guild.id, "log_channel_id", channel.id)
            await interaction.response.send_message(
                f"✅ Log sẽ gửi vào {channel.mention}.", ephemeral=True)
            await self._log(
                interaction.guild, "📋 Log channel",
                f"Channel log được đặt thành {channel.mention} bởi {interaction.user.mention}.")
        except Exception:
            log.exception("Error in /setlog")

    # ========================================================
    # ✅ /setup-verify — đặt kênh verify + verified role
    # ========================================================
    @app_commands.command(
        name="setup-verify",
        description="Đặt kênh verify + role cấp khi verify xong")
    @app_commands.describe(
        channel="Kênh gửi captcha cho member mới",
        verified_role="Role cấp khi verify thành công (bỏ trống = bot tự tạo role Verified)")
    @app_commands.default_permissions(administrator=True)
    async def setup_verify(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel,
        verified_role: discord.Role = None,
    ):
        try:
            await interaction.response.defer(ephemeral=True)
            guild = interaction.guild

            # Verified role: dùng role được chọn, hoặc tự tạo
            if verified_role is None:
                role_id = await self.db.get_config(guild.id, "role_verified")
                verified_role = guild.get_role(role_id) if role_id else None
                if verified_role is None:
                    verified_role = await guild.create_role(
                        name="Verified", color=discord.Color.green(),
                        reason="/setup-verify: tạo Verified role")
            await self.db.set_config(guild.id, "role_verified", verified_role.id)
            await self.db.set_config(guild.id, "channel_verify", channel.id)

            # Set permission kênh verify (best-effort):
            # - Unverified: thấy được kênh (để bấm nút verify), không gửi tin
            # - Verified: ẩn kênh (verify xong không thấy nữa, đỡ loãng)
            unverified_id = await self.db.get_config(guild.id, "role_unverified")
            unverified = guild.get_role(unverified_id) if unverified_id else None
            try:
                if unverified:
                    await channel.set_permissions(
                        unverified, view_channel=True, send_messages=False,
                        reason="/setup-verify")
                await channel.set_permissions(
                    verified_role, view_channel=False, reason="/setup-verify")
            except discord.Forbidden:
                pass  # thiếu quyền chỉnh permission → admin tự chỉnh tay

            await interaction.followup.send(
                f"✅ Setup verify xong!\n"
                f"• Kênh captcha: {channel.mention}\n"
                f"• Role sau verify: {verified_role.mention}\n"
                f"• Member mới sẽ nhận captcha trong {channel.mention} "
                f"(tin tự xóa, không loãng kênh).",
                ephemeral=True)
            await self._log(
                guild, "✅ Setup verify",
                f"{interaction.user.mention} đặt kênh verify {channel.mention}, "
                f"role {verified_role.mention}.")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Bot thiếu quyền Manage Roles/Channels.", ephemeral=True)
        except Exception:
            log.exception("Error in /setup-verify")

    # ========================================================
    # 🔐 /whitelist — CHỈ Owner/Co-owner
    # ========================================================
    whitelist_group = app_commands.Group(
        name="whitelist", description="Quản lý whitelist user (Owner/Co-owner)")

    @whitelist_group.command(name="add", description="Thêm user vào whitelist (bypass all)")
    @is_owner_or_coowner()
    async def wl_add(self, interaction: discord.Interaction, user: discord.Member):
        try:
            await self.db.add_whitelist_user(
                interaction.guild.id, user.id, interaction.user.id)
            await interaction.response.send_message(
                f"✅ {user.mention} đã vào whitelist — bypass toàn bộ module.",
                ephemeral=True)
            await self._log(
                interaction.guild, "🔐 Whitelist +",
                f"{interaction.user.mention} thêm {user.mention} vào whitelist.",
                discord.Color.green(), user=user)
        except Exception:
            log.exception("Error in /whitelist add")

    @whitelist_group.command(name="remove", description="Xóa user khỏi whitelist")
    @is_owner_or_coowner()
    async def wl_remove(self, interaction: discord.Interaction, user: discord.Member):
        try:
            await self.db.remove_whitelist_user(interaction.guild.id, user.id)
            await interaction.response.send_message(
                f"✅ Đã xóa {user.mention} khỏi whitelist.", ephemeral=True)
            await self._log(
                interaction.guild, "🔐 Whitelist −",
                f"{interaction.user.mention} xóa {user.mention} khỏi whitelist.",
                discord.Color.orange(), user=user)
        except Exception:
            log.exception("Error in /whitelist remove")

    @whitelist_group.command(name="list", description="Xem danh sách whitelist")
    @is_owner_or_coowner()
    async def wl_list(self, interaction: discord.Interaction):
        try:
            rows = await self.db.list_whitelist_users(interaction.guild.id)
            owners = "\n".join(f"<@{uid}> *(Owner)*" for uid in config.OWNER_IDS)
            coowners = "\n".join(f"<@{uid}> *(Co-owner)*" for uid in config.CO_OWNER_IDS)
            wl = "\n".join(
                f"<@{uid}> — thêm bởi <@{by}> <t:{at}:R>" for uid, by, at in rows)
            desc = "\n".join(x for x in (owners, coowners, wl) if x) or "*Trống*"
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🔐 Whitelist", description=desc[:4000],
                    color=discord.Color.blue()),
                ephemeral=True)
        except Exception:
            log.exception("Error in /whitelist list")

    # ========================================================
    # 🤖 /whitelistbot — CHỈ Owner/Co-owner
    # ========================================================
    wlbot_group = app_commands.Group(
        name="whitelistbot", description="Quản lý whitelist bot (Owner/Co-owner)")

    @wlbot_group.command(name="add", description="Cho phép 1 bot được add vào server")
    @app_commands.describe(bot_id="ID của bot")
    @is_owner_or_coowner()
    async def wlbot_add(self, interaction: discord.Interaction, bot_id: str):
        try:
            bid = int(bot_id)
        except ValueError:
            return await interaction.response.send_message(
                "❌ ID không hợp lệ.", ephemeral=True)
        await self.db.add_whitelist_bot(interaction.guild.id, bid, interaction.user.id)
        await interaction.response.send_message(
            f"✅ Bot `{bid}` đã được whitelist.", ephemeral=True)
        await self._log(
            interaction.guild, "🤖 Whitelist bot +",
            f"{interaction.user.mention} whitelist bot `{bid}`.")

    @wlbot_group.command(name="remove", description="Xóa bot khỏi whitelist")
    @app_commands.describe(bot_id="ID của bot")
    @is_owner_or_coowner()
    async def wlbot_remove(self, interaction: discord.Interaction, bot_id: str):
        try:
            bid = int(bot_id)
        except ValueError:
            return await interaction.response.send_message(
                "❌ ID không hợp lệ.", ephemeral=True)
        await self.db.remove_whitelist_bot(interaction.guild.id, bid)
        await interaction.response.send_message(
            f"✅ Đã xóa bot `{bid}` khỏi whitelist.", ephemeral=True)

    # ========================================================
    # 🈲 /blacklist — từ cấm per-guild
    # ========================================================
    blacklist_group = app_commands.Group(
        name="blacklist", description="Quản lý từ cấm",
        default_permissions=discord.Permissions(administrator=True))

    @blacklist_group.command(name="add", description="Thêm từ cấm cho server này")
    @app_commands.describe(word="Từ/cụm từ cấm")
    async def bl_add(self, interaction: discord.Interaction, word: str):
        try:
            await self.db.add_blacklist_word(interaction.guild.id, word)
            await interaction.response.send_message(
                f"✅ Đã cấm từ: `{word}`.", ephemeral=True)
            await self._log(
                interaction.guild, "🈲 Blacklist +",
                f"{interaction.user.mention} thêm từ cấm: `{word}`.")
        except Exception:
            log.exception("Error in /blacklist add")

    @blacklist_group.command(name="remove", description="Xóa từ cấm")
    @app_commands.describe(word="Từ cần bỏ cấm")
    async def bl_remove(self, interaction: discord.Interaction, word: str):
        try:
            await self.db.remove_blacklist_word(interaction.guild.id, word)
            await interaction.response.send_message(
                f"✅ Đã bỏ cấm từ: `{word}`.", ephemeral=True)
        except Exception:
            log.exception("Error in /blacklist remove")

    @blacklist_group.command(name="list", description="Xem danh sách từ cấm của server")
    async def bl_list(self, interaction: discord.Interaction):
        try:
            words = await self.db.get_blacklist_words(interaction.guild.id)
            desc = ", ".join(f"`{w}`" for w in sorted(words)) or "*Trống*"
            await interaction.response.send_message(
                embed=discord.Embed(
                    title=f"🈲 Từ cấm ({len(words)})", description=desc[:4000],
                    color=discord.Color.red()),
                ephemeral=True)
        except Exception:
            log.exception("Error in /blacklist list")

    @blacklist_group.command(
        name="reload", description="Reload file blacklist.txt (global)")
    async def bl_reload(self, interaction: discord.Interaction):
        try:
            cog = self.bot.get_cog("AntiContent")
            if cog:
                cog.reload_blacklist()
                count = len(cog._global_blacklist)
                await interaction.response.send_message(
                    f"✅ Đã reload blacklist.txt — **{count}** từ global.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "❌ Module AntiContent chưa load.", ephemeral=True)
        except Exception:
            log.exception("Error in /blacklist reload")

    # ========================================================
    # 🔒 /lockdown + /unlockdown — toàn server (whitelist)
    # ========================================================
    @app_commands.command(name="lockdown", description="🚨 Khóa toàn bộ server")
    @is_whitelisted()
    async def lockdown(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            cog = self.bot.get_cog("AntiRaid")
            if cog is None:
                return await interaction.followup.send(
                    "❌ Module AntiRaid chưa load.", ephemeral=True)
            await cog.lockdown_guild(
                interaction.guild, f"Lệnh /lockdown bởi {interaction.user}")
            await interaction.followup.send(
                "🚨 Đã **LOCKDOWN** toàn server.", ephemeral=True)
        except Exception:
            log.exception("Error in /lockdown")

    @app_commands.command(name="unlockdown", description="🔓 Mở khóa toàn server")
    @is_whitelisted()
    async def unlockdown(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            cog = self.bot.get_cog("AntiRaid")
            if cog is None:
                return await interaction.followup.send(
                    "❌ Module AntiRaid chưa load.", ephemeral=True)
            await cog.unlockdown_guild(
                interaction.guild, f"Lệnh /unlockdown bởi {interaction.user}")
            await interaction.followup.send(
                "🔓 Đã mở khóa toàn server.", ephemeral=True)
        except Exception:
            log.exception("Error in /unlockdown")

    # ========================================================
    # 🔒 /lock + /unlock — 1 channel (mod)
    # ========================================================
    @app_commands.command(name="lock", description="Khóa channel hiện tại")
    @app_commands.default_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction):
        try:
            channel = interaction.channel
            overwrite = channel.overwrites_for(interaction.guild.default_role)
            overwrite.send_messages = False
            await channel.set_permissions(
                interaction.guild.default_role, overwrite=overwrite,
                reason=f"/lock bởi {interaction.user}")
            await interaction.response.send_message(
                f"🔒 Đã khóa {channel.mention}.")
            await self._log(
                interaction.guild, "🔒 Lock channel",
                f"{interaction.user.mention} khóa {channel.mention}.")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot thiếu quyền Manage Channels.", ephemeral=True)
        except Exception:
            log.exception("Error in /lock")

    @app_commands.command(name="unlock", description="Mở khóa channel hiện tại")
    @app_commands.default_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        try:
            channel = interaction.channel
            overwrite = channel.overwrites_for(interaction.guild.default_role)
            overwrite.send_messages = None  # trả về mặc định
            await channel.set_permissions(
                interaction.guild.default_role, overwrite=overwrite,
                reason=f"/unlock bởi {interaction.user}")
            await interaction.response.send_message(
                f"🔓 Đã mở khóa {channel.mention}.")
            await self._log(
                interaction.guild, "🔓 Unlock channel",
                f"{interaction.user.mention} mở khóa {channel.mention}.")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot thiếu quyền Manage Channels.", ephemeral=True)
        except Exception:
            log.exception("Error in /unlock")

    # ========================================================
    # 💾 /backup + /restore — Owner/Co-owner
    # ========================================================
    @app_commands.command(name="backup", description="Backup server ngay (Owner/Co-owner)")
    @is_owner_or_coowner()
    async def backup_cmd(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            file_path = await backup_util.create_backup(interaction.guild)
            if file_path:
                await self.db.add_backup(interaction.guild.id, file_path)
                await interaction.followup.send(
                    f"💾 Backup thành công: `{file_path}`", ephemeral=True)
                await self._log(
                    interaction.guild, "💾 Backup thủ công",
                    f"{interaction.user.mention} vừa backup server.")
            else:
                await interaction.followup.send("❌ Backup thất bại.", ephemeral=True)
        except Exception:
            log.exception("Error in /backup")

    @app_commands.command(
        name="restore", description="Restore từ backup mới nhất (Owner/Co-owner)")
    @is_owner_or_coowner()
    async def restore_cmd(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            file_path = await self.db.get_latest_backup(interaction.guild.id)
            if not file_path:
                return await interaction.followup.send(
                    "❌ Chưa có backup nào.", ephemeral=True)
            stats = await backup_util.restore_from_backup(interaction.guild, file_path)
            await interaction.followup.send(
                f"♻️ Restore xong: **{stats['roles']}** role, "
                f"**{stats['categories']}** category, **{stats['channels']}** channel.",
                ephemeral=True)
            await self._log(
                interaction.guild, "♻️ Restore thủ công",
                f"{interaction.user.mention} restore từ backup: {stats}")
        except Exception:
            log.exception("Error in /restore")


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfigCommands(bot))

# ✅ Done: cogs/config_commands.py — Tiếp theo: HUONG_DAN.md
