"""
cogs/pick_role.py — Panel pick role bằng nút bấm.
- /pickrole send <title> <role1> [role2..role5] [channel] — gửi panel
- Member bấm nút: chưa có role → nhận, có rồi → gỡ (toggle)
- Panel lưu SQLite → bot restart vẫn hoạt động (persistent view)
- Chặn role nguy hiểm (admin/manage) + role cao hơn bot
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("bot.pick_role")


def _is_dangerous(role: discord.Role) -> bool:
    """Role có quyền nguy hiểm → cấm đưa vào panel."""
    p = role.permissions
    return (p.administrator or p.manage_guild or p.manage_roles
            or p.manage_channels or p.manage_webhooks or p.ban_members
            or p.kick_members or p.moderate_members or p.mention_everyone)


class RoleButton(discord.ui.Button):
    """Nút toggle 1 role — custom_id cố định để persist qua restart."""

    def __init__(self, role_id: int, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.blurple,
            custom_id=f"pickrole:{role_id}",  # custom_id cố định → persistent
        )
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        try:
            role = interaction.guild.get_role(self.role_id)
            if role is None:
                return await interaction.response.send_message(
                    "❌ Role này đã bị xóa khỏi server.", ephemeral=True)
            member = interaction.user
            if role in member.roles:
                await member.remove_roles(role, reason="Pick role: tự gỡ")
                await interaction.response.send_message(
                    f"➖ Đã gỡ role {role.mention}.", ephemeral=True)
            else:
                await member.add_roles(role, reason="Pick role: tự nhận")
                await interaction.response.send_message(
                    f"➕ Đã nhận role {role.mention}!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot không đủ quyền (role bot phải cao hơn role này).",
                ephemeral=True)
        except Exception:
            log.exception("Error in role button callback")


class RolePanelView(discord.ui.View):
    """View chứa các nút role — timeout=None để persist."""

    def __init__(self, role_pairs: list):
        """role_pairs: list[(role_id, label)]"""
        super().__init__(timeout=None)
        for role_id, label in role_pairs[:25]:  # Discord max 25 nút/view
            self.add_item(RoleButton(role_id, label))


class PickRole(commands.Cog):
    """Cog pick role — panel nút bấm tự nhận/gỡ role."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def cog_load(self):
        """Re-attach persistent views cho các panel cũ khi bot khởi động."""
        try:
            panels = await self.db.get_all_role_panels()
            for message_id, guild_id, channel_id, role_ids_str in panels:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    continue
                pairs = []
                for rid in role_ids_str.split(","):
                    try:
                        role = guild.get_role(int(rid))
                        if role:
                            pairs.append((role.id, role.name))
                    except ValueError:
                        continue
                if pairs:
                    # message_id gắn view vào đúng tin nhắn cũ
                    self.bot.add_view(RolePanelView(pairs), message_id=message_id)
            if panels:
                log.info("Re-attached %d role panels", len(panels))
        except Exception:
            log.exception("Error re-attaching role panels")

    # ========================================================
    # 📝 SLASH COMMANDS
    # ========================================================
    pickrole_group = app_commands.Group(
        name="pickrole", description="Panel tự nhận role",
        default_permissions=discord.Permissions(manage_roles=True))

    @pickrole_group.command(name="send", description="Gửi panel pick role (tối đa 5 role)")
    @app_commands.describe(
        title="Tiêu đề panel",
        role1="Role 1", role2="Role 2", role3="Role 3",
        role4="Role 4", role5="Role 5",
        channel="Kênh gửi panel (bỏ trống = kênh hiện tại)",
        description="Mô tả thêm (tùy chọn)")
    async def pickrole_send(
        self, interaction: discord.Interaction,
        title: str,
        role1: discord.Role,
        role2: discord.Role = None,
        role3: discord.Role = None,
        role4: discord.Role = None,
        role5: discord.Role = None,
        channel: discord.TextChannel = None,
        description: str = "",
    ):
        try:
            roles = [r for r in (role1, role2, role3, role4, role5) if r]
            target = channel or interaction.channel

            # Validate từng role
            errors = []
            valid_roles = []
            for role in roles:
                if role.is_default() or role.managed:
                    errors.append(f"{role.mention}: role hệ thống/bot — bỏ qua")
                elif _is_dangerous(role):
                    errors.append(f"{role.mention}: có quyền nguy hiểm — bỏ qua")
                elif role >= interaction.guild.me.top_role:
                    errors.append(f"{role.mention}: cao hơn role bot — bỏ qua")
                else:
                    valid_roles.append(role)
            if not valid_roles:
                return await interaction.response.send_message(
                    "❌ Không có role hợp lệ nào:\n" + "\n".join(errors),
                    ephemeral=True)

            # Dựng embed panel
            embed = discord.Embed(
                title=f"🎭 {title}",
                description=(description + "\n\n" if description else "")
                + "Bấm nút bên dưới để **nhận/gỡ** role:\n"
                + "\n".join(f"• {r.mention}" for r in valid_roles),
                color=discord.Color.blurple(),
            )
            view = RolePanelView([(r.id, r.name) for r in valid_roles])
            sent = await target.send(embed=embed, view=view)

            # Lưu DB để re-attach sau restart
            await self.db.add_role_panel(
                sent.id, interaction.guild.id, target.id,
                [r.id for r in valid_roles])

            msg = f"✅ Đã gửi panel vào {target.mention} — [Xem]({sent.jump_url})"
            if errors:
                msg += "\n⚠️ " + "\n⚠️ ".join(errors)
            await interaction.response.send_message(msg, ephemeral=True)

            logger = self.bot.get_cog("Logger")
            if logger:
                await logger.send_log(
                    interaction.guild, "🎭 Panel pick role",
                    f"{interaction.user.mention} tạo panel `{title}` trong {target.mention} "
                    f"({len(valid_roles)} role).")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot không có quyền gửi tin trong kênh đó.", ephemeral=True)
        except Exception:
            log.exception("Error in /pickrole send")

    @pickrole_group.command(name="delete", description="Xóa panel pick role")
    @app_commands.describe(
        message_id="ID tin nhắn panel (chuột phải → Copy Message ID)")
    async def pickrole_delete(self, interaction: discord.Interaction, message_id: str):
        try:
            try:
                mid = int(message_id)
            except ValueError:
                return await interaction.response.send_message(
                    "❌ ID không hợp lệ.", ephemeral=True)
            await self.db.remove_role_panel(mid)
            # Thử xóa luôn tin nhắn panel
            try:
                msg = await interaction.channel.fetch_message(mid)
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            await interaction.response.send_message(
                "✅ Đã xóa panel (nếu tin nhắn ở kênh khác, xóa tay giúp mình).",
                ephemeral=True)
        except Exception:
            log.exception("Error in /pickrole delete")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Panel bị xóa tay → dọn record DB."""
        try:
            await self.db.remove_role_panel(payload.message_id)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(PickRole(bot))
