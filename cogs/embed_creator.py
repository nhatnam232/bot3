"""
cogs/embed_creator.py — Tạo embed có PREVIEW giống bot Mimu.
Flow: /embed create → Modal nhập nội dung → preview ephemeral
      → nút [📤 Gửi] [✏️ Sửa] [🎨 Đổi màu] [❌ Hủy]
- Chỉ người tạo thao tác được với preview
- /embed edit <message_id>: sửa embed bot đã gửi
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("bot.embed_creator")

# Bảng màu chọn nhanh
COLOR_CHOICES = {
    "🔴 Đỏ": discord.Color.red(),
    "🟠 Cam": discord.Color.orange(),
    "🟡 Vàng": discord.Color.gold(),
    "🟢 Xanh lá": discord.Color.green(),
    "🔵 Xanh dương": discord.Color.blue(),
    "🟣 Tím": discord.Color.purple(),
    "🩷 Hồng": discord.Color.pink(),
    "⚫ Đen": discord.Color.from_str("#2b2d31"),
    "⚪ Trắng": discord.Color.from_str("#ffffff"),
}


def build_embed(data: dict) -> discord.Embed:
    """Dựng embed từ dict dữ liệu modal."""
    embed = discord.Embed(
        title=data.get("title") or None,
        description=data.get("description") or None,
        color=data.get("color", discord.Color.blurple()),
    )
    if data.get("image"):
        embed.set_image(url=data["image"])
    if data.get("thumbnail"):
        embed.set_thumbnail(url=data["thumbnail"])
    if data.get("footer"):
        embed.set_footer(text=data["footer"])
    return embed


class EmbedModal(discord.ui.Modal):
    """Modal nhập nội dung embed (mở khi tạo mới hoặc bấm Sửa)."""

    def __init__(self, parent_view, data: dict = None):
        super().__init__(title="🖌️ Tạo Embed", timeout=600)
        self.parent_view = parent_view
        data = data or {}

        self.f_title = discord.ui.TextInput(
            label="Tiêu đề", required=False, max_length=256,
            default=data.get("title", ""))
        self.f_desc = discord.ui.TextInput(
            label="Nội dung", style=discord.TextStyle.paragraph,
            required=False, max_length=4000, default=data.get("description", ""))
        self.f_image = discord.ui.TextInput(
            label="Link ảnh lớn (để trống nếu không có)", required=False,
            default=data.get("image", ""))
        self.f_thumb = discord.ui.TextInput(
            label="Link ảnh nhỏ góc phải", required=False,
            default=data.get("thumbnail", ""))
        self.f_footer = discord.ui.TextInput(
            label="Footer (dòng chữ nhỏ dưới cùng)", required=False,
            max_length=2048, default=data.get("footer", ""))
        for item in (self.f_title, self.f_desc, self.f_image,
                     self.f_thumb, self.f_footer):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            v = self.parent_view
            v.data.update({
                "title": self.f_title.value.strip(),
                "description": self.f_desc.value.strip(),
                "image": self.f_image.value.strip(),
                "thumbnail": self.f_thumb.value.strip(),
                "footer": self.f_footer.value.strip(),
            })
            if not v.data["title"] and not v.data["description"]:
                return await interaction.response.send_message(
                    "❌ Cần ít nhất tiêu đề hoặc nội dung.", ephemeral=True)
            # Cập nhật preview
            try:
                await interaction.response.edit_message(
                    content="👀 **Preview** — bấm nút để thao tác:",
                    embed=build_embed(v.data), view=v)
            except discord.HTTPException:
                # Link ảnh lỗi → bỏ ảnh, báo người dùng
                v.data["image"] = v.data["thumbnail"] = ""
                await interaction.response.edit_message(
                    content="⚠️ Link ảnh không hợp lệ (đã bỏ). **Preview:**",
                    embed=build_embed(v.data), view=v)
        except Exception:
            log.exception("Error in embed modal submit")

    async def on_error(self, interaction, error):
        log.exception("Embed modal error: %s", error)


class ColorSelect(discord.ui.Select):
    """Dropdown đổi màu embed trong preview."""

    def __init__(self):
        super().__init__(
            placeholder="🎨 Chọn màu embed...",
            options=[discord.SelectOption(label=name) for name in COLOR_CHOICES],
            row=1)

    async def callback(self, interaction: discord.Interaction):
        try:
            view: EmbedPreviewView = self.view
            view.data["color"] = COLOR_CHOICES[self.values[0]]
            await interaction.response.edit_message(
                embed=build_embed(view.data), view=view)
        except Exception:
            log.exception("Error in color select")


class EmbedPreviewView(discord.ui.View):
    """View preview: Gửi / Sửa / Đổi màu / Hủy — chỉ người tạo dùng được."""

    def __init__(self, author_id: int, target_channel: discord.TextChannel,
                 edit_message: discord.Message = None):
        super().__init__(timeout=600)
        self.author_id = author_id
        self.target_channel = target_channel
        self.edit_message = edit_message  # khác None = đang sửa embed cũ
        self.data: dict = {"color": discord.Color.blurple()}
        self.add_item(ColorSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Preview này không phải của bạn.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="📤 Gửi", style=discord.ButtonStyle.green, row=0)
    async def send_btn(self, interaction: discord.Interaction, _):
        try:
            embed = build_embed(self.data)
            if self.edit_message:
                await self.edit_message.edit(embed=embed)
                msg = f"✅ Đã cập nhật embed trong {self.edit_message.channel.mention}."
            else:
                sent = await self.target_channel.send(embed=embed)
                msg = f"✅ Đã gửi embed vào {self.target_channel.mention} — [Xem]({sent.jump_url})"
            self.stop()
            await interaction.response.edit_message(content=msg, embed=None, view=None)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot không có quyền gửi/sửa trong kênh đó.", ephemeral=True)
        except Exception:
            log.exception("Error sending embed")

    @discord.ui.button(label="✏️ Sửa", style=discord.ButtonStyle.blurple, row=0)
    async def edit_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(EmbedModal(self, self.data))

    @discord.ui.button(label="❌ Hủy", style=discord.ButtonStyle.red, row=0)
    async def cancel_btn(self, interaction: discord.Interaction, _):
        self.stop()
        await interaction.response.edit_message(
            content="🗑️ Đã hủy.", embed=None, view=None)


class EmbedCreator(commands.Cog):
    """Cog tạo/sửa embed có preview."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    embed_group = app_commands.Group(
        name="embed", description="Tạo embed đẹp có preview",
        default_permissions=discord.Permissions(manage_messages=True))

    @embed_group.command(name="create", description="Tạo embed mới (có preview trước khi gửi)")
    @app_commands.describe(channel="Kênh sẽ gửi embed (bỏ trống = kênh hiện tại)")
    async def embed_create(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel = None,
    ):
        try:
            target = channel or interaction.channel
            view = EmbedPreviewView(interaction.user.id, target)
            # Mở modal nhập nội dung ngay
            modal = EmbedModal(view)

            # Modal cần được gửi trước, preview tạo trong on_submit —
            # nhưng on_submit cần message để edit → gửi placeholder preview trước
            async def open_modal(inter: discord.Interaction):
                await inter.response.send_modal(modal)

            # Gửi preview placeholder kèm nút "Bắt đầu nhập"
            start_view = discord.ui.View(timeout=300)
            start_btn = discord.ui.Button(
                label="🖌️ Nhập nội dung embed", style=discord.ButtonStyle.blurple)

            async def start_cb(inter: discord.Interaction):
                if inter.user.id != interaction.user.id:
                    return await inter.response.send_message(
                        "❌ Không phải của bạn.", ephemeral=True)
                await inter.response.send_modal(EmbedModal(view, view.data))

            start_btn.callback = start_cb
            start_view.add_item(start_btn)
            await interaction.response.send_message(
                f"📍 Embed sẽ gửi vào {target.mention} — bấm nút để nhập nội dung:",
                view=start_view, ephemeral=True)
        except Exception:
            log.exception("Error in /embed create")

    @embed_group.command(name="edit", description="Sửa embed bot đã gửi")
    @app_commands.describe(
        message_id="ID tin nhắn chứa embed (chuột phải → Copy Message ID)",
        channel="Kênh chứa tin (bỏ trống = kênh hiện tại)")
    async def embed_edit(
        self, interaction: discord.Interaction,
        message_id: str, channel: discord.TextChannel = None,
    ):
        try:
            target = channel or interaction.channel
            try:
                msg = await target.fetch_message(int(message_id))
            except (ValueError, discord.NotFound):
                return await interaction.response.send_message(
                    "❌ Không tìm thấy tin nhắn với ID đó trong kênh.", ephemeral=True)
            if msg.author.id != self.bot.user.id:
                return await interaction.response.send_message(
                    "❌ Chỉ sửa được embed do bot này gửi.", ephemeral=True)

            view = EmbedPreviewView(interaction.user.id, target, edit_message=msg)
            # Nạp dữ liệu embed cũ vào preview
            if msg.embeds:
                old = msg.embeds[0]
                view.data.update({
                    "title": old.title or "",
                    "description": old.description or "",
                    "image": old.image.url if old.image else "",
                    "thumbnail": old.thumbnail.url if old.thumbnail else "",
                    "footer": old.footer.text if old.footer else "",
                    "color": old.color or discord.Color.blurple(),
                })
            await interaction.response.send_message(
                "👀 **Preview embed đang sửa** — bấm nút để thao tác:",
                embed=build_embed(view.data), view=view, ephemeral=True)
        except Exception:
            log.exception("Error in /embed edit")


async def setup(bot: commands.Bot):
    await bot.add_cog(EmbedCreator(bot))
