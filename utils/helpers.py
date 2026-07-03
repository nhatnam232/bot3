"""
utils/helpers.py — Decorator phân quyền + hàm tiện ích dùng chung.
- @is_owner_or_coowner(): chỉ Owner/Co-owner (hardcode config.py) dùng được lệnh
- @is_whitelisted(): Owner/Co-owner HOẶC user trong whitelist SQLite
- Hàm format thời gian, parse duration...
"""

import logging

import discord
from discord import app_commands

import config

log = logging.getLogger("bot.helpers")


# ============================================================
# 🔐 DECORATORS PHÂN QUYỀN — dùng cho mọi lệnh nhạy cảm
# ============================================================
def is_owner_or_coowner():
    """
    Decorator app_commands: CHỈ Owner/Co-owner (hardcode trong config.py).
    Dùng: @is_owner_or_coowner() dưới @app_commands.command(...)
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        if config.is_owner_or_coowner(interaction.user.id):
            return True
        try:
            await interaction.response.send_message(
                "🚫 Lệnh này chỉ dành cho **Owner/Co-owner** của bot.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        return False
    return app_commands.check(predicate)


def is_whitelisted():
    """
    Decorator app_commands: Owner/Co-owner HOẶC user trong whitelist (SQLite).
    Dùng cho các lệnh mod nhạy cảm (lockdown, backup, restore...).
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        if config.is_owner_or_coowner(interaction.user.id):
            return True
        if interaction.guild:
            try:
                # bot.db gắn trên instance SecurityBot
                if await interaction.client.db.is_whitelisted(
                        interaction.guild.id, interaction.user.id):
                    return True
            except Exception:
                log.exception("Error checking whitelist in decorator")
        try:
            await interaction.response.send_message(
                "🚫 Bạn cần nằm trong **whitelist** để dùng lệnh này.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        return False
    return app_commands.check(predicate)


# ============================================================
# 🛠️ TIỆN ÍCH
# ============================================================
def format_duration(seconds: int) -> str:
    """600 → '10 phút', 86400 → '1 ngày'..."""
    if seconds < 60:
        return f"{seconds} giây"
    if seconds < 3600:
        return f"{seconds // 60} phút"
    if seconds < 86400:
        return f"{seconds // 3600} giờ"
    return f"{seconds // 86400} ngày"


def parse_duration(text: str) -> int | None:
    """
    '10s'/'5m'/'2h'/'1d' → giây. Số trần coi là giây.
    Trả về None nếu không parse được.
    """
    text = text.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        if text[-1] in units:
            return int(text[:-1]) * units[text[-1]]
        return int(text)
    except (ValueError, IndexError):
        return None


def chunk_text(text: str, size: int = 1900) -> list[str]:
    """Cắt chuỗi dài thành nhiều đoạn (giới hạn tin nhắn Discord 2000)."""
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]

# ✅ Done: utils/helpers.py — Tiếp theo: cogs/config_commands.py
