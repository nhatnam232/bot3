"""
cogs/warn_system.py — Hệ thống warn tích lũy theo user + guild.
Thang phạt:
  - Lần 1: DM cảnh báo
  - Lần 2: mute 10 phút (warn_mute1_duration)
  - Lần 3: mute 1 giờ (warn_mute2_duration)
  - Lần 4: kick
  - Lần 5: ban vĩnh viễn
- Warn auto expire sau 30 ngày (warn_expire_days, chỉnh được)
- Mute persist qua restart (bảng active_mutes) — task định kỳ unmute
- Owner/Co-owner + whitelist bypass (không bị warn)
- Slash commands: /warn, /warns, /clearwarns, /unmute
"""

import time
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config

log = logging.getLogger("bot.warn_system")


class WarnSystem(commands.Cog):
    """Cog warn — các cog anti-* gọi self.bot.get_cog('WarnSystem').warn_user(...)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.unmute_task.start()       # check unmute mỗi 30 giây
        self.expire_warn_task.start()  # expire warn cũ mỗi giờ

    def cog_unload(self):
        self.unmute_task.cancel()
        self.expire_warn_task.cancel()

    # ========================================================
    # 🔧 HELPERS
    # ========================================================
    async def _get_logger(self):
        return self.bot.get_cog("Logger")

    async def _is_bypassed(self, guild_id: int, user_id: int) -> bool:
        """Owner/Co-owner + whitelist bypass warn system."""
        if config.is_owner_or_coowner(user_id):
            return True
        return await self.db.is_whitelisted(guild_id, user_id)

    async def _get_muted_role(self, guild: discord.Guild) -> discord.Role:
        """Lấy role Muted (tạo nếu chưa có) + chặn send_messages mọi channel."""
        role_id = await self.db.get_config(guild.id, "role_muted")
        role = guild.get_role(role_id) if role_id else None
        if role:
            return role

        # Chưa có → tạo mới
        role = await guild.create_role(
            name="Muted", reason="Auto-created by security bot",
            color=discord.Color.dark_gray(),
        )
        await self.db.set_config(guild.id, "role_muted", role.id)

        # Chặn send_messages ở mọi text channel (best-effort)
        for channel in guild.channels:
            try:
                await channel.set_permissions(
                    role, send_messages=False, add_reactions=False,
                    speak=False, reason="Setup Muted role",
                )
            except discord.HTTPException:
                continue
        return role

    async def _dm_user(self, user: discord.abc.User, message: str):
        """DM user, bỏ qua nếu họ tắt DM."""
        try:
            await user.send(message)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ========================================================
    # 🧹 PURGE — xóa sạch tin gần đây của đối tượng bị warn/mute
    # ========================================================
    async def purge_user_messages(
        self, member: discord.Member,
        channel: discord.TextChannel = None,
    ) -> int:
        """
        Xóa tin nhắn gần đây (trong warn_purge_window giây) của member.
        - channel truyền vào → chỉ quét kênh đó (nhanh)
        - channel = None → quét toàn bộ text channel (chậm hơn, dùng cho mute)
        Trả về số tin đã xóa.
        """
        guild = member.guild
        if not await self.db.get_config(guild.id, "warn_purge_messages"):
            return 0
        window = await self.db.get_config(guild.id, "warn_purge_window")
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window)
        deleted = 0
        try:
            channels = [channel] if channel else guild.text_channels
            for ch in channels:
                try:
                    # purge với bulk delete (tin <14 ngày) — check đúng author + sau cutoff
                    msgs = await ch.purge(
                        limit=200, after=cutoff,
                        check=lambda m: m.author.id == member.id,
                        reason=f"Dọn tin nhắn của {member} (bị warn/mute)",
                    )
                    deleted += len(msgs)
                except (discord.Forbidden, discord.HTTPException):
                    continue  # kênh không có quyền → bỏ qua
                if not channel:
                    await asyncio.sleep(0.3)  # quét cả server → nhẹ tay với rate limit
            if deleted:
                logger = await self._get_logger()
                if logger:
                    await logger.send_log(
                        guild, "🧹 Dọn tin nhắn vi phạm",
                        f"Đã xóa **{deleted}** tin gần đây của {member.mention}.",
                        discord.Color.dark_gray(), user=member,
                    )
        except Exception:
            log.exception("Error purging messages of %s", member)
        return deleted

    # ========================================================
    # ⚠️ CORE — warn_user: API cho mọi cog anti-* gọi
    # ========================================================
    async def warn_user(
        self,
        member: discord.Member,
        moderator_id: int,
        reason: str,
        channel: discord.TextChannel = None,
    ) -> int:
        """
        Thêm warn + áp dụng thang phạt theo số warn tích lũy.
        channel: kênh vi phạm — bot sẽ xóa sạch tin gần đây của đối tượng trong kênh đó.
        Trả về số warn hiện tại (0 nếu bypass).
        """
        try:
            guild = member.guild
            if not await self.db.is_module_enabled(guild.id, "warn"):
                return 0
            # 🔐 Check bypass TRƯỚC khi xử lý
            if await self._is_bypassed(guild.id, member.id):
                return 0

            count = await self.db.add_warn(guild.id, member.id, moderator_id, reason)

            # 🧹 Bị warn → dọn sạch tin gần đây trong kênh vi phạm
            if channel:
                await self.purge_user_messages(member, channel)
            logger = await self._get_logger()
            if logger:
                await logger.send_log(
                    guild, "⚠️ Warn",
                    f"{member.mention} bị warn (lần **{count}**)",
                    discord.Color.orange(),
                    fields=[("Lý do", reason), ("Mod", f"<@{moderator_id}>")],
                    user=member,
                )

            # Áp dụng thang phạt
            await self._apply_punishment(member, count, reason)
            return count
        except Exception:
            log.exception("Error in warn_user")
            return 0

    async def _apply_punishment(self, member: discord.Member, count: int, reason: str):
        """Thang phạt: 1=DM, 2=mute 10p, 3=mute 1h, 4=kick, 5+=ban."""
        guild = member.guild
        try:
            if count == 1:
                # Lần 1: DM cảnh báo
                await self._dm_user(
                    member,
                    f"⚠️ Bạn bị cảnh báo tại **{guild.name}**.\n"
                    f"Lý do: {reason}\nWarn tiếp theo sẽ bị mute!",
                )

            elif count == 2:
                duration = await self.db.get_config(guild.id, "warn_mute1_duration")
                await self.mute_member(member, duration, f"Warn lần 2: {reason}")

            elif count == 3:
                duration = await self.db.get_config(guild.id, "warn_mute2_duration")
                await self.mute_member(member, duration, f"Warn lần 3: {reason}")

            elif count == 4:
                # Lần 4: kick
                await self._dm_user(
                    member,
                    f"👢 Bạn bị KICK khỏi **{guild.name}** (warn lần 4).\nLý do: {reason}",
                )
                await member.kick(reason=f"Warn lần 4: {reason}")
                logger = await self._get_logger()
                if logger:
                    await logger.send_log(
                        guild, "👢 Kick (warn lần 4)",
                        f"**{member}** bị kick do tích lũy 4 warn.",
                        discord.Color.red(), user=member,
                    )

            elif count >= 5:
                # Lần 5: ban vĩnh viễn
                await self._dm_user(
                    member,
                    f"🔨 Bạn bị BAN vĩnh viễn khỏi **{guild.name}** (warn lần 5).\nLý do: {reason}",
                )
                await member.ban(reason=f"Warn lần 5: {reason}", delete_message_days=0)
                # on_member_ban trong logger.py sẽ tự log

        except discord.Forbidden:
            log.warning("Missing permission to punish %s in guild %s", member, guild.id)
        except Exception:
            log.exception("Error applying punishment")

    # ========================================================
    # 🔇 MUTE / UNMUTE
    # ========================================================
    async def mute_member(self, member: discord.Member, duration: int, reason: str):
        """
        Mute member trong N giây — 2 LỚP:
        1. Discord Timeout (chắc chắn nhất — chặn mọi kênh, không cần chỉnh perm)
        2. Muted role + lưu DB để persist (phòng khi timeout fail do hierarchy)
        """
        try:
            guild = member.guild

            # Lớp 1: Discord Timeout chính chủ (tối đa 28 ngày)
            timeout_ok = False
            try:
                await member.timeout(
                    timedelta(seconds=min(duration, 28 * 86400)), reason=reason)
                timeout_ok = True
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Timeout failed for %s — fallback to Muted role", member)

            # Lớp 2: Muted role (nếu role chưa đè được kênh nào thì timeout đã lo)
            try:
                role = await self._get_muted_role(guild)
                await member.add_roles(role, reason=reason)
            except (discord.Forbidden, discord.HTTPException):
                if not timeout_ok:
                    raise  # cả 2 lớp đều fail → báo lỗi thật
            unmute_at = int(time.time()) + duration
            await self.db.add_mute(guild.id, member.id, unmute_at)

            # 🧹 Bị mute → quét xóa tin gần đây của đối tượng TOÀN BỘ kênh
            await self.purge_user_messages(member)

            await self._dm_user(
                member,
                f"🔇 Bạn bị mute tại **{guild.name}** trong **{duration // 60} phút**.\n"
                f"Lý do: {reason}",
            )
            logger = await self._get_logger()
            if logger:
                await logger.send_log(
                    guild, "🔇 Mute",
                    f"{member.mention} bị mute **{duration // 60} phút**.",
                    discord.Color.dark_gray(),
                    fields=[("Lý do", reason)],
                    user=member,
                )
        except discord.Forbidden:
            log.warning("Missing permission to mute %s", member)
        except Exception:
            log.exception("Error muting member")

    async def unmute_member(self, guild: discord.Guild, user_id: int, reason: str = "Hết hạn mute"):
        """Gỡ mute + xóa record DB."""
        try:
            await self.db.remove_mute(guild.id, user_id)
            member = guild.get_member(user_id)
            if member is None:
                return  # đã rời server
            # Gỡ Discord Timeout (nếu đang còn)
            try:
                if member.is_timed_out():
                    await member.timeout(None, reason=reason)
            except (discord.Forbidden, discord.HTTPException):
                pass
            role_id = await self.db.get_config(guild.id, "role_muted")
            role = guild.get_role(role_id) if role_id else None
            if role and role in member.roles:
                await member.remove_roles(role, reason=reason)
                logger = await self._get_logger()
                if logger:
                    await logger.send_log(
                        guild, "🔊 Unmute",
                        f"{member.mention} đã được unmute ({reason}).",
                        discord.Color.green(), user=member,
                    )
        except discord.Forbidden:
            log.warning("Missing permission to unmute user %s", user_id)
        except Exception:
            log.exception("Error unmuting member")

    # ========================================================
    # ⏲️ BACKGROUND TASKS
    # ========================================================
    @tasks.loop(seconds=30)
    async def unmute_task(self):
        """Check mỗi 30s — unmute những ai đã hết hạn (persist qua restart)."""
        try:
            expired = await self.db.get_expired_mutes()
            for guild_id, user_id in expired:
                guild = self.bot.get_guild(guild_id)
                if guild:
                    await self.unmute_member(guild, user_id)
                else:
                    await self.db.remove_mute(guild_id, user_id)  # bot đã rời guild
        except Exception:
            log.exception("Error in unmute task")

    @tasks.loop(hours=1)
    async def expire_warn_task(self):
        """Mỗi giờ: đánh dấu expire các warn quá hạn."""
        try:
            await self.db.expire_old_warns()
        except Exception:
            log.exception("Error in expire warn task")

    @unmute_task.before_loop
    @expire_warn_task.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ========================================================
    # 📝 SLASH COMMANDS
    # ========================================================
    @app_commands.command(name="warn", description="Warn một thành viên")
    @app_commands.describe(member="Thành viên cần warn", reason="Lý do warn")
    @app_commands.default_permissions(moderate_members=True)
    async def warn_cmd(
        self, interaction: discord.Interaction,
        member: discord.Member, reason: str = "Không có lý do",
    ):
        try:
            if member.bot:
                return await interaction.response.send_message(
                    "❌ Không thể warn bot.", ephemeral=True)
            if member.id == interaction.user.id:
                return await interaction.response.send_message(
                    "❌ Không thể tự warn chính mình.", ephemeral=True)
            if await self._is_bypassed(interaction.guild.id, member.id):
                return await interaction.response.send_message(
                    "🛡️ Người này nằm trong whitelist/Owner — không thể warn.",
                    ephemeral=True)

            await interaction.response.defer(ephemeral=True)
            count = await self.warn_user(member, interaction.user.id, reason)
            punishments = {1: "DM cảnh báo", 2: "Mute 10 phút", 3: "Mute 1 giờ",
                           4: "Kick", 5: "Ban vĩnh viễn"}
            action = punishments.get(min(count, 5), "Không rõ")
            await interaction.followup.send(
                f"⚠️ Đã warn {member.mention} — lần **{count}** → **{action}**.\n"
                f"Lý do: {reason}", ephemeral=True)
        except Exception:
            log.exception("Error in /warn")
            try:
                await interaction.followup.send("❌ Có lỗi khi warn.", ephemeral=True)
            except discord.HTTPException:
                pass

    @app_commands.command(name="warns", description="Xem danh sách warn của thành viên")
    @app_commands.describe(member="Thành viên cần xem")
    @app_commands.default_permissions(moderate_members=True)
    async def warns_cmd(self, interaction: discord.Interaction, member: discord.Member):
        try:
            warns = await self.db.get_warns(interaction.guild.id, member.id)
            if not warns:
                return await interaction.response.send_message(
                    f"✅ {member.mention} không có warn nào còn hiệu lực.", ephemeral=True)

            embed = discord.Embed(
                title=f"⚠️ Warn của {member.display_name} ({len(warns)})",
                color=discord.Color.orange(),
            )
            for warn_id, mod_id, reason, created_at in warns[:10]:
                embed.add_field(
                    name=f"#{warn_id} — <t:{created_at}:R>",
                    value=f"Mod: <@{mod_id}>\nLý do: {reason or 'Không có'}",
                    inline=False,
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Error in /warns")

    @app_commands.command(name="clearwarns", description="Xóa toàn bộ warn của thành viên")
    @app_commands.describe(member="Thành viên cần xóa warn")
    @app_commands.default_permissions(administrator=True)
    async def clearwarns_cmd(self, interaction: discord.Interaction, member: discord.Member):
        try:
            await self.db.clear_warns(interaction.guild.id, member.id)
            await interaction.response.send_message(
                f"🧹 Đã xóa toàn bộ warn của {member.mention}.", ephemeral=True)
            logger = await self._get_logger()
            if logger:
                await logger.send_log(
                    interaction.guild, "🧹 Clear warns",
                    f"{interaction.user.mention} đã xóa toàn bộ warn của {member.mention}.",
                    discord.Color.green(), user=member,
                )
        except Exception:
            log.exception("Error in /clearwarns")

    @app_commands.command(name="unmute", description="Gỡ mute cho thành viên")
    @app_commands.describe(member="Thành viên cần unmute")
    @app_commands.default_permissions(moderate_members=True)
    async def unmute_cmd(self, interaction: discord.Interaction, member: discord.Member):
        try:
            await interaction.response.defer(ephemeral=True)
            await self.unmute_member(
                interaction.guild, member.id,
                reason=f"Unmute bởi {interaction.user}",
            )
            await interaction.followup.send(
                f"🔊 Đã unmute {member.mention}.", ephemeral=True)
        except Exception:
            log.exception("Error in /unmute")


async def setup(bot: commands.Bot):
    await bot.add_cog(WarnSystem(bot))

# ✅ Done: cogs/warn_system.py — Tiếp theo: cogs/anti_spam.py
