"""
cogs/logger.py — Module log trung tâm.
- Log join/leave, message xóa/sửa (kèm nội dung gốc)
- Log ban/kick/mute/warn (cog khác gọi qua send_log)
- Log thay đổi role, channel, webhook, bot add/remove, cấp quyền admin
- Mỗi log gửi vào channel log riêng (set qua /config set log_channel_id)
- Hành động của Owner/Co-owner VẪN được log đầy đủ (audit)
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

from utils.audit_cache import audit_cache

log = logging.getLogger("bot.logger")

# Màu embed theo loại log
COLOR_GREEN = discord.Color.green()     # join, tạo mới
COLOR_RED = discord.Color.red()         # leave, xóa, ban
COLOR_ORANGE = discord.Color.orange()   # sửa, cảnh báo
COLOR_BLUE = discord.Color.blue()       # thông tin chung
COLOR_PURPLE = discord.Color.purple()   # admin/quyền hạn


class Logger(commands.Cog):
    """Cog log — các cog khác gọi self.bot.get_cog('Logger').send_log(...)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    # ========================================================
    # 🔧 HELPER — gửi log vào channel đã cấu hình
    # ========================================================
    async def get_log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """Lấy channel log của guild (None nếu chưa set hoặc tắt log)."""
        try:
            if not await self.db.is_module_enabled(guild.id, "log"):
                return None
            channel_id = await self.db.get_config(guild.id, "log_channel_id")
            if not channel_id:
                return None
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        except Exception:
            log.exception("Failed to get log channel for guild %s", guild.id)
        return None

    async def send_log(
        self,
        guild: discord.Guild,
        title: str,
        description: str = "",
        color: discord.Color = COLOR_BLUE,
        fields: Optional[list] = None,
        user: Optional[discord.abc.User] = None,
    ):
        """
        API chung cho mọi cog gửi log.
        fields: list các tuple (name, value)
        user: hiển thị tag + ID ở footer
        """
        try:
            channel = await self.get_log_channel(guild)
            if channel is None:
                return

            embed = discord.Embed(
                title=title,
                description=description[:4000],  # giới hạn Discord
                color=color,
                timestamp=datetime.now(timezone.utc),
            )
            if fields:
                for name, value in fields[:25]:
                    embed.add_field(name=name, value=str(value)[:1024], inline=False)
            if user:
                embed.set_footer(
                    text=f"{user} • ID: {user.id}",
                    icon_url=user.display_avatar.url,
                )
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("No permission to send log in guild %s", guild.id)
        except Exception:
            log.exception("Failed to send log in guild %s", guild.id)

    async def _find_audit_actor(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: Optional[int] = None,
    ) -> Optional[discord.abc.User]:
        """Tra audit log tìm ai thực hiện hành động (best-effort).
        
        Dùng audit_cache dùng chung — tránh rate limit 429 khi nhiều cog
        cùng gọi audit log trong 1 sự kiện.
        """
        entries = await audit_cache.get_entries(guild, action)
        for entry in entries:
            if target_id is None or (entry.target and entry.target.id == target_id):
                return entry.user
        return None

    # ========================================================
    # 👋 JOIN / LEAVE (+ BOT ADD)
    # ========================================================
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            created = discord.utils.format_dt(member.created_at, style="R")
            if member.bot:
                # 🤖 Log riêng khi bot được add (kèm ai add)
                actor = await self._find_audit_actor(
                    member.guild, discord.AuditLogAction.bot_add, member.id
                )
                await self.send_log(
                    member.guild,
                    "🤖 Bot được thêm vào server",
                    f"Bot {member.mention} vừa được add.",
                    COLOR_PURPLE,
                    fields=[("Người add", actor.mention if actor else "Không rõ")],
                    user=member,
                )
                return
            await self.send_log(
                member.guild,
                "📥 Thành viên vào server",
                f"{member.mention} đã tham gia.",
                COLOR_GREEN,
                fields=[("Account tạo", created)],
                user=member,
            )
        except Exception:
            log.exception("Error logging member join")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        try:
            title = "🤖 Bot bị xóa khỏi server" if member.bot else "📤 Thành viên rời server"
            roles = ", ".join(r.mention for r in member.roles[1:]) or "Không có"
            await self.send_log(
                member.guild,
                title,
                f"**{member}** đã rời đi.",
                COLOR_RED,
                fields=[("Roles", roles[:1024])],
                user=member,
            )
        except Exception:
            log.exception("Error logging member remove")

    # ========================================================
    # 💬 MESSAGE XÓA / SỬA (kèm nội dung gốc)
    # ========================================================
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        try:
            if message.guild is None or message.author.bot:
                return
            content = message.content or "*(không có text — có thể là ảnh/file)*"
            fields = [("Nội dung gốc", content[:1024])]
            attachments = "\n".join(a.url for a in message.attachments)
            if attachments:
                fields.append(("Đính kèm", attachments[:1024]))
            await self.send_log(
                message.guild,
                "🗑️ Tin nhắn bị xóa",
                f"Tin của {message.author.mention} trong {message.channel.mention}",
                COLOR_RED,
                fields=fields,
                user=message.author,
            )
        except Exception:
            log.exception("Error logging message delete")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        try:
            if before.guild is None or before.author.bot:
                return
            if before.content == after.content:  # chỉ embed thay đổi → bỏ qua
                return
            await self.send_log(
                before.guild,
                "✏️ Tin nhắn bị sửa",
                f"Tin của {before.author.mention} trong {before.channel.mention} "
                f"— [Nhảy tới tin]({after.jump_url})",
                COLOR_ORANGE,
                fields=[
                    ("Trước", (before.content or "*(trống)*")[:1024]),
                    ("Sau", (after.content or "*(trống)*")[:1024]),
                ],
                user=before.author,
            )
        except Exception:
            log.exception("Error logging message edit")

    # ========================================================
    # 🔨 BAN / UNBAN (kick được log từ warn_system/anti_raid)
    # ========================================================
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        try:
            actor = await self._find_audit_actor(guild, discord.AuditLogAction.ban, user.id)
            await self.send_log(
                guild,
                "🔨 Thành viên bị BAN",
                f"**{user}** đã bị ban.",
                COLOR_RED,
                fields=[("Người thực hiện", actor.mention if actor else "Không rõ")],
                user=user,
            )
        except Exception:
            log.exception("Error logging ban")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        try:
            await self.send_log(
                guild, "🔓 Thành viên được UNBAN",
                f"**{user}** đã được unban.", COLOR_GREEN, user=user,
            )
        except Exception:
            log.exception("Error logging unban")

    # ========================================================
    # 🎭 ROLE thành viên — ai cấp/thu hồi gì + CẤP QUYỀN ADMIN
    # ========================================================
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        try:
            added = set(after.roles) - set(before.roles)
            removed = set(before.roles) - set(after.roles)
            if not added and not removed:
                return

            actor = await self._find_audit_actor(
                after.guild, discord.AuditLogAction.member_role_update, after.id
            )
            fields = [("Người thực hiện", actor.mention if actor else "Không rõ")]
            if added:
                fields.append(("Role được cấp", ", ".join(r.mention for r in added)))
            if removed:
                fields.append(("Role bị thu hồi", ", ".join(r.mention for r in removed)))

            await self.send_log(
                after.guild,
                "🎭 Thay đổi role thành viên",
                f"Role của {after.mention} đã thay đổi.",
                COLOR_BLUE, fields=fields, user=after,
            )

            # ⚠️ Log riêng nếu role mới có quyền ADMIN
            admin_roles = [r for r in added if r.permissions.administrator]
            if admin_roles:
                await self.send_log(
                    after.guild,
                    "⚠️ CẤP QUYỀN ADMIN",
                    f"{after.mention} vừa nhận role có quyền **Administrator**: "
                    f"{', '.join(r.mention for r in admin_roles)}",
                    COLOR_PURPLE,
                    fields=[("Người cấp", actor.mention if actor else "Không rõ")],
                    user=after,
                )
        except Exception:
            log.exception("Error logging member update")

    # ========================================================
    # 🎭 ROLE — tạo/xóa/được thêm quyền admin
    # ========================================================
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        try:
            actor = await self._find_audit_actor(role.guild, discord.AuditLogAction.role_create)
            extra = " — ⚠️ **CÓ QUYỀN ADMIN**" if role.permissions.administrator else ""
            await self.send_log(
                role.guild, "➕ Role được tạo",
                f"Role {role.mention} (`{role.name}`) vừa được tạo{extra}.",
                COLOR_PURPLE if role.permissions.administrator else COLOR_GREEN,
                fields=[("Người tạo", actor.mention if actor else "Không rõ")],
            )
        except Exception:
            log.exception("Error logging role create")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        try:
            actor = await self._find_audit_actor(role.guild, discord.AuditLogAction.role_delete)
            await self.send_log(
                role.guild, "➖ Role bị xóa",
                f"Role `{role.name}` đã bị xóa.",
                COLOR_RED,
                fields=[("Người xóa", actor.mention if actor else "Không rõ")],
            )
        except Exception:
            log.exception("Error logging role delete")

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        try:
            # Chỉ log khi role ĐƯỢC THÊM quyền admin (quan trọng cho audit)
            if not before.permissions.administrator and after.permissions.administrator:
                actor = await self._find_audit_actor(
                    after.guild, discord.AuditLogAction.role_update, after.id
                )
                await self.send_log(
                    after.guild,
                    "⚠️ Role được THÊM quyền ADMIN",
                    f"Role {after.mention} vừa được cấp quyền **Administrator**!",
                    COLOR_PURPLE,
                    fields=[("Người thực hiện", actor.mention if actor else "Không rõ")],
                )
        except Exception:
            log.exception("Error logging role update")

    # ========================================================
    # 📺 CHANNEL — tạo/xóa/đổi tên
    # ========================================================
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        try:
            actor = await self._find_audit_actor(
                channel.guild, discord.AuditLogAction.channel_create
            )
            await self.send_log(
                channel.guild, "➕ Channel được tạo",
                f"Channel {channel.mention} (`{channel.name}`) vừa được tạo.",
                COLOR_GREEN,
                fields=[("Người tạo", actor.mention if actor else "Không rõ")],
            )
        except Exception:
            log.exception("Error logging channel create")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        try:
            actor = await self._find_audit_actor(
                channel.guild, discord.AuditLogAction.channel_delete
            )
            await self.send_log(
                channel.guild, "➖ Channel bị xóa",
                f"Channel `{channel.name}` đã bị xóa.",
                COLOR_RED,
                fields=[("Người xóa", actor.mention if actor else "Không rõ")],
            )
        except Exception:
            log.exception("Error logging channel delete")

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        try:
            if before.name == after.name:
                return  # chỉ log đổi tên (tránh spam log do sync permission)
            await self.send_log(
                after.guild, "✏️ Channel đổi tên",
                f"`{before.name}` → {after.mention} (`{after.name}`)",
                COLOR_ORANGE,
            )
        except Exception:
            log.exception("Error logging channel update")

    # ========================================================
    # 🪝 WEBHOOK tạo/xóa
    # ========================================================
    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        try:
            actor = await self._find_audit_actor(
                channel.guild, discord.AuditLogAction.webhook_create
            )
            await self.send_log(
                channel.guild, "🪝 Webhook thay đổi",
                f"Webhook trong {channel.mention} vừa được tạo/sửa/xóa.",
                COLOR_ORANGE,
                fields=[("Liên quan", actor.mention if actor else "Không rõ")],
            )
        except Exception:
            log.exception("Error logging webhook update")

    # ========================================================
    # 🏠 GUILD UPDATE — đổi tên/avatar server
    # ========================================================
    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        try:
            changes = []
            if before.name != after.name:
                changes.append(("Tên server", f"`{before.name}` → `{after.name}`"))
            if before.icon != after.icon:
                changes.append(("Avatar server", "Đã thay đổi"))
            if not changes:
                return
            actor = await self._find_audit_actor(after, discord.AuditLogAction.guild_update)
            changes.append(("Người thực hiện", actor.mention if actor else "Không rõ"))
            await self.send_log(
                after, "🏠 Server bị thay đổi", "", COLOR_ORANGE, fields=changes,
            )
        except Exception:
            log.exception("Error logging guild update")


async def setup(bot: commands.Bot):
    await bot.add_cog(Logger(bot))

# ✅ Done: cogs/logger.py — Tiếp theo: cogs/warn_system.py