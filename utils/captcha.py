"""
utils/captcha.py — Captcha verify cho thành viên mới.
- Sinh mã ngẫu nhiên 6 ký tự, gửi DM (fallback: channel verify)
- User bấm nút → nhập mã vào Modal
- anti_raid.py quản lý timeout: không verify trong N giây → kick
"""

import random
import string
import logging

import discord

log = logging.getLogger("bot.captcha")

# Bỏ ký tự dễ nhầm: 0/O, 1/I/l
CAPTCHA_CHARS = "".join(
    c for c in string.ascii_uppercase + string.digits if c not in "01OI"
)


def generate_code(length: int = 6) -> str:
    """Sinh mã captcha ngẫu nhiên."""
    return "".join(random.choices(CAPTCHA_CHARS, k=length))


class CaptchaModal(discord.ui.Modal):
    """Modal nhập mã captcha."""

    def __init__(self, code: str, on_success):
        super().__init__(title="🔐 Xác minh thành viên", timeout=120)
        self.code = code
        self.on_success = on_success  # callback async(interaction) khi đúng mã
        self.answer = discord.ui.TextInput(
            label=f"Nhập mã: {code}",
            placeholder="Nhập chính xác mã ở trên...",
            min_length=len(code),
            max_length=len(code),
            required=True,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if self.answer.value.strip().upper() == self.code:
                await self.on_success(interaction)
            else:
                await interaction.response.send_message(
                    "❌ Sai mã! Bấm nút **Verify** để thử lại.", ephemeral=True
                )
        except Exception:
            log.exception("Error in captcha modal submit")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("Captcha modal error: %s", error)
        try:
            await interaction.response.send_message(
                "❌ Có lỗi xảy ra, thử lại.", ephemeral=True
            )
        except discord.HTTPException:
            pass


class CaptchaView(discord.ui.View):
    """View chứa nút Verify — đính kèm tin nhắn captcha."""

    def __init__(self, code: str, member_id: int, on_success, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.code = code
        self.member_id = member_id  # chỉ đúng user này mới bấm được
        self.on_success = on_success

    @discord.ui.button(label="✅ Verify", style=discord.ButtonStyle.green)
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Chặn người khác bấm hộ (trong channel verify chung)
            if self.member_id and interaction.user.id != self.member_id:
                return await interaction.response.send_message(
                    "❌ Nút này không dành cho bạn.", ephemeral=True
                )
            await interaction.response.send_modal(
                CaptchaModal(self.code, self.on_success)
            )
        except Exception:
            log.exception("Error opening captcha modal")


async def send_captcha(
    member: discord.Member,
    on_success,
    timeout: int = 60,
    verify_channel: discord.TextChannel = None,
    fallback_channel: discord.TextChannel = None,
) -> bool:
    """
    Gửi captcha cho member. Thứ tự ưu tiên:
      1. verify_channel (kênh set qua /setup-verify) — tin tự xóa sau timeout, không loãng kênh
      2. DM (khi chưa setup kênh verify)
      3. fallback_channel (system channel)
    on_success: callback async(interaction) khi verify đúng.
    Trả về True nếu gửi được, False nếu không thể gửi.
    """
    code = generate_code()
    view = CaptchaView(code, member.id, on_success, timeout=max(timeout, 60))
    embed = discord.Embed(
        title="🔐 Xác minh thành viên",
        description=(
            f"Chào {member.mention}! Server **{member.guild.name}** yêu cầu xác minh.\n\n"
            f"Bấm nút **Verify** bên dưới và nhập mã hiện ra.\n"
            f"⏰ Bạn có **{timeout} giây** — không verify sẽ bị kick!"
        ),
        color=discord.Color.blurple(),
    )

    # 1️⃣ Kênh verify (ưu tiên) — tin tự xóa sau timeout + 10s để kênh sạch
    if verify_channel:
        try:
            await verify_channel.send(
                content=member.mention, embed=embed, view=view,
                delete_after=timeout + 10,
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Cannot send captcha in verify channel %s", verify_channel.id)

    # 2️⃣ DM
    try:
        await member.send(embed=embed, view=view)
        return True
    except (discord.Forbidden, discord.HTTPException):
        pass

    # 3️⃣ Fallback: system channel
    if fallback_channel:
        try:
            await fallback_channel.send(
                content=member.mention, embed=embed, view=view,
                delete_after=timeout + 10,
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            pass
    log.warning("Cannot send captcha to %s (no channel, DM closed)", member)
    return False

# ✅ Done: utils/captcha.py — Tiếp theo: cogs/anti_raid.py
