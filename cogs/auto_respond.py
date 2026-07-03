"""
cogs/auto_respond.py — Auto responder giống bot Mimu.
- /autorespond add <trigger> <reply> [exact] — bot tự trả lời khi member nhắn trigger
- /autorespond remove <trigger>, /autorespond list
- Lưu SQLite per-guild + cache RAM, hỗ trợ placeholder {user} {server}
- Cooldown 3s/trigger/channel để không spam reply
"""

import time
import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("bot.auto_respond")

MAX_RESPONSES_PER_GUILD = 50  # giới hạn để không bị lạm dụng


class AutoRespond(commands.Cog):
    """Cog auto respond — check trigger trong mọi tin nhắn."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        # Cache: {guild_id: list[(trigger, reply, exact)]}
        self._cache: dict = {}
        # Cooldown: {(channel_id, trigger): timestamp}
        self._cooldown: dict = {}

    async def _get_responses(self, guild_id: int) -> list:
        """Lấy auto responses từ cache (load DB lần đầu)."""
        if guild_id not in self._cache:
            self._cache[guild_id] = await self.db.get_auto_responses(guild_id)
        return self._cache[guild_id]

    def _invalidate(self, guild_id: int):
        self._cache.pop(guild_id, None)

    # ========================================================
    # 📨 LISTENER — trả lời khi khớp trigger
    # ========================================================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            if message.guild is None or message.author.bot:
                return
            responses = await self._get_responses(message.guild.id)
            if not responses:
                return

            content = message.content.lower().strip()
            if not content:
                return

            for trigger, reply, exact in responses:
                matched = (content == trigger) if exact else (trigger in content)
                if not matched:
                    continue

                # Cooldown 3s/trigger/channel — tránh spam reply
                key = (message.channel.id, trigger)
                now = time.time()
                if now - self._cooldown.get(key, 0) < 3:
                    return
                self._cooldown[key] = now

                # Placeholder: {user} = mention, {server} = tên server
                text = (reply
                        .replace("{user}", message.author.mention)
                        .replace("{server}", message.guild.name))
                try:
                    await message.channel.send(text[:2000])
                except discord.HTTPException:
                    pass
                return  # chỉ trả lời 1 trigger/tin
        except Exception:
            log.exception("Error in auto respond listener")

    # ========================================================
    # 📝 SLASH COMMANDS
    # ========================================================
    ar_group = app_commands.Group(
        name="autorespond", description="Quản lý auto respond",
        default_permissions=discord.Permissions(manage_guild=True))

    @ar_group.command(name="add", description="Thêm auto respond (trigger → reply)")
    @app_commands.describe(
        trigger="Từ khóa kích hoạt",
        reply="Nội dung trả lời ({user} = mention, {server} = tên server)",
        exact="True = khớp cả câu chính xác, False = chứa từ khóa (mặc định)")
    async def ar_add(
        self, interaction: discord.Interaction,
        trigger: str, reply: str, exact: bool = False,
    ):
        try:
            existing = await self._get_responses(interaction.guild.id)
            if len(existing) >= MAX_RESPONSES_PER_GUILD:
                return await interaction.response.send_message(
                    f"❌ Đã đạt giới hạn {MAX_RESPONSES_PER_GUILD} auto respond.",
                    ephemeral=True)
            trigger_norm = trigger.lower().strip()
            if len(trigger_norm) < 2:
                return await interaction.response.send_message(
                    "❌ Trigger phải dài ít nhất 2 ký tự.", ephemeral=True)

            await self.db.add_auto_response(
                interaction.guild.id, trigger_norm, reply,
                1 if exact else 0, interaction.user.id)
            self._invalidate(interaction.guild.id)
            mode = "khớp cả câu" if exact else "chứa từ khóa"
            await interaction.response.send_message(
                f"✅ Đã thêm auto respond ({mode}):\n"
                f"**Trigger:** `{trigger_norm}`\n**Reply:** {reply[:500]}",
                ephemeral=True)
            logger = self.bot.get_cog("Logger")
            if logger:
                await logger.send_log(
                    interaction.guild, "💬 Auto respond +",
                    f"{interaction.user.mention} thêm trigger `{trigger_norm}`.")
        except Exception:
            log.exception("Error in /autorespond add")

    @ar_group.command(name="remove", description="Xóa auto respond")
    @app_commands.describe(trigger="Trigger cần xóa")
    async def ar_remove(self, interaction: discord.Interaction, trigger: str):
        try:
            ok = await self.db.remove_auto_response(interaction.guild.id, trigger)
            self._invalidate(interaction.guild.id)
            if ok:
                await interaction.response.send_message(
                    f"✅ Đã xóa trigger `{trigger.lower().strip()}`.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"❌ Không tìm thấy trigger `{trigger}`.", ephemeral=True)
        except Exception:
            log.exception("Error in /autorespond remove")

    @ar_group.command(name="list", description="Xem danh sách auto respond")
    async def ar_list(self, interaction: discord.Interaction):
        try:
            responses = await self._get_responses(interaction.guild.id)
            if not responses:
                return await interaction.response.send_message(
                    "📭 Chưa có auto respond nào. Thêm bằng `/autorespond add`.",
                    ephemeral=True)
            lines = []
            for trigger, reply, exact in responses[:25]:
                mode = "=" if exact else "~"
                lines.append(f"`{mode}` **{trigger}** → {reply[:80]}")
            embed = discord.Embed(
                title=f"💬 Auto respond ({len(responses)})",
                description="\n".join(lines)[:4000],
                color=discord.Color.blurple(),
            )
            embed.set_footer(text="= khớp cả câu • ~ chứa từ khóa")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Error in /autorespond list")


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoRespond(bot))
