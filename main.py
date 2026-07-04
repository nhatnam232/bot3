"""
main.py — Entry point của Discord Security Bot. ◄◄◄ ĐÂY LÀ FILE CHÍNH ĐỂ CHẠY: python main.py
- Auto-install thư viện thiếu (check rồi mới cài — cài rồi thì bỏ qua)
- Khởi tạo bot + intents
- Load database, load toàn bộ cogs
- Sync slash commands
- Global check: rate limit lệnh mod (Owner/Co-owner bypass)
"""

import os
import sys
import time
import logging
import importlib.util

# ============================================================
# 📦 AUTO-INSTALL THƯ VIỆN — chạy TRƯỚC khi import discord
# Check từng package: đã cài → bỏ qua, thiếu → pip install
# Deploy lên host mới / deploy lại đều tự cài, không cần làm gì
# ============================================================
# (tên import, tên package trên pip)
REQUIRED_PACKAGES = [
    ("discord", "discord.py>=2.3.0"),
    ("aiosqlite", "aiosqlite>=0.19.0"),
    ("aiohttp", "aiohttp>=3.8.0"),
    ("yt_dlp", "yt-dlp>=2024.1.0"),
    ("nacl", "PyNaCl>=1.5.0"),
]


def auto_install_packages():
    """Kiểm tra và tự cài thư viện thiếu. Đã cài đủ → không làm gì."""
    import subprocess

    missing = [
        pip_name
        for import_name, pip_name in REQUIRED_PACKAGES
        if importlib.util.find_spec(import_name) is None  # check đã cài chưa
    ]
    if not missing:
        return  # ✅ đủ hết — bỏ qua, không cài lại

    print(f"[SETUP] Thiếu thư viện: {', '.join(missing)} — đang tự cài...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--no-input", *missing]
        )
        print("[SETUP] ✅ Cài xong toàn bộ thư viện!")
    except subprocess.CalledProcessError:
        print("[SETUP] ❌ Cài thư viện thất bại! Chạy tay: pip install -r requirements.txt")
        sys.exit(1)


auto_install_packages()

# ---- Từ đây mới import được các thư viện ngoài ----
import asyncio
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import db

# ============================================================
# 📋 LOGGING SETUP
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot.main")

# ============================================================
# DANH SÁCH COGS — load theo thứ tự
# ============================================================
COGS = [
    "cogs.logger",
    "cogs.warn_system",
    "cogs.anti_spam",
    "cogs.anti_content",
    "cogs.anti_raid",
    "cogs.anti_nuke",
    "cogs.config_commands",
    "cogs.auto_respond",
    "cogs.embed_creator",
    "cogs.pick_role",
    "cogs.music",
]


class SecurityBot(commands.Bot):
    """Bot chính — chứa db + rate limit lệnh mod."""

    def __init__(self):
        # Cần đầy đủ intents để theo dõi member join, message content, audit log...
        intents = discord.Intents.default()
        intents.members = True          # on_member_join, member update
        intents.message_content = True  # đọc nội dung tin nhắn (anti-spam/content)
        intents.guilds = True
        intents.moderation = True
        intents.voice_states = True     # theo dõi voice cho tính năng 24/7

        super().__init__(
            command_prefix=commands.when_mentioned,  # chỉ dùng slash, prefix là mention
            intents=intents,
            help_command=None,
        )

        self.db = db
        # Rate limit lệnh mod: {(guild_id, user_id): deque[timestamp]}
        self._cmd_usage: dict[tuple, deque] = defaultdict(deque)
        # Đã deploy slash per-guild chưa (on_ready chạy lại mỗi lần reconnect)
        self._slash_deployed = False

    # ========================================================
    # SETUP — chạy 1 lần trước khi connect
    # ========================================================
    async def setup_hook(self):
        # Tạo thư mục data nếu chưa có
        os.makedirs(config.DATA_DIR, exist_ok=True)
        os.makedirs(config.BACKUP_DIR, exist_ok=True)

        # Khởi tạo database
        await self.db.init()

        # Load toàn bộ cogs — lỗi 1 cog không làm sập bot
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info("Loaded cog: %s", cog)
            except Exception:
                log.exception("Failed to load cog: %s", cog)

        # Đăng ký global check cho slash commands (rate limit)
        self.tree.interaction_check = self._slash_rate_limit

        # 🔄 AUTO DEPLOY SLASH: CHỈ sync per-guild (trong on_ready) để tránh DUPE.
        # KHÔNG sync global ở đây — sync cả 2 nơi là nguyên nhân lệnh bị x2!

    # ========================================================
    # 🔄 AUTO DEPLOY SLASH per-guild — hiện lệnh NGAY không chờ cache
    # ========================================================
    async def deploy_slash_to_guild(self, guild: discord.Guild):
        """Copy lệnh global vào guild + sync → lệnh mới hiện NGAY lập tức."""
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Deployed %d slash commands to guild: %s", len(synced), guild.name)
        except discord.HTTPException as e:
            log.warning("Failed to deploy slash to guild %s: %s", guild.id, e)
        except Exception:
            log.exception("Error deploying slash to guild %s", guild.id)

    # ========================================================
    # 🔐 RATE LIMIT LỆNH MOD — 10 lệnh / phút / người (chỉnh được)
    # Owner/Co-owner + whitelist bypass
    # ========================================================
    async def _slash_rate_limit(self, interaction: discord.Interaction) -> bool:
        try:
            # Lệnh trong DM hoặc Owner/Co-owner → bypass
            if interaction.guild is None:
                return True
            if config.is_owner_or_coowner(interaction.user.id):
                return True
            if await self.db.is_whitelisted(interaction.guild.id, interaction.user.id):
                return True

            limit = await self.db.get_config(interaction.guild.id, "ratelimit_mod_commands")
            window = await self.db.get_config(interaction.guild.id, "ratelimit_mod_window")

            key = (interaction.guild.id, interaction.user.id)
            now = time.time()
            usage = self._cmd_usage[key]

            # Bỏ các timestamp ngoài window
            while usage and now - usage[0] > window:
                usage.popleft()

            if len(usage) >= limit:
                try:
                    await interaction.response.send_message(
                        f"⏳ Bạn dùng lệnh quá nhanh! Giới hạn {limit} lệnh/{window}s.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass
                return False

            usage.append(now)
            return True
        except Exception:
            log.exception("Error in rate limit check")
            return True  # lỗi check không được chặn lệnh

    # ========================================================
    # EVENTS
    # ========================================================
    async def on_ready(self):
        log.info("Bot ready: %s (ID: %s) — %d guilds",
                 self.user, self.user.id, len(self.guilds))

        # 🔄 AUTO DEPLOY SLASH vào từng server đang tham gia (chỉ chạy 1 lần
        # mỗi lần khởi động — on_ready có thể fire lại khi reconnect)
        if not self._slash_deployed:
            self._slash_deployed = True

            # 🧹 FIX DUPE: xóa sạch lệnh GLOBAL trên Discord (bản cũ đã lỡ sync
            # trước đây). Gọi thẳng API nên không đụng tree local → per-guild
            # deploy bên dưới vẫn đầy đủ lệnh. Từ giờ lệnh CHỈ tồn tại per-guild.
            try:
                await self.http.bulk_upsert_global_commands(self.application_id, [])
                log.info("Cleared global slash commands (fix dupe)")
            except Exception:
                log.exception("Failed to clear global slash commands")

            for guild in self.guilds:
                await self.deploy_slash_to_guild(guild)
            log.info("✅ Slash commands deployed to all %d guilds", len(self.guilds))

        try:
            # 🟣 Trạng thái Streaming → chấm màu TÍM thay vì xanh online
            # (Discord không cho đổi màu trực tiếp — đây là cách duy nhất;
            #  URL Twitch chỉ để Discord nhận diện, không cần stream thật)
            await self.change_presence(
                activity=discord.Streaming(
                    name="🕊️ Wings sheltering Sol'S RNG VN",
                    url="https://www.twitch.tv/discord",
                )
            )
        except Exception:
            log.exception("Failed to set presence")

    async def on_guild_join(self, guild: discord.Guild):
        """Bot được add vào server mới → deploy slash ngay, không chờ restart."""
        log.info("Joined new guild: %s (ID: %s)", guild.name, guild.id)
        await self.deploy_slash_to_guild(guild)

    async def on_app_command_error(self, interaction: discord.Interaction, error):
        """Bắt mọi lỗi slash command — không để bot crash."""
        log.exception("App command error: %s", error)
        msg = "❌ Có lỗi xảy ra khi chạy lệnh."
        if isinstance(error, app_commands.CheckFailure):
            msg = "🚫 Bạn không có quyền dùng lệnh này."
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Lệnh đang cooldown, thử lại sau {error.retry_after:.0f}s."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def close(self):
        """Đóng bot + database an toàn."""
        log.info("Shutting down...")
        await self.db.close()
        await super().close()


# ============================================================
# CHẠY BOT
# ============================================================
async def main():
    if not config.BOT_TOKEN:
        log.error("BOT_TOKEN chưa được đặt! Đặt biến môi trường BOT_TOKEN hoặc sửa config.py")
        return

    bot = SecurityBot()
    # Gắn error handler cho app commands tree
    bot.tree.on_error = bot.on_app_command_error

    try:
        async with bot:
            await bot.start(config.BOT_TOKEN)
    except discord.LoginFailure:
        log.error("Token không hợp lệ!")
    except KeyboardInterrupt:
        log.info("Stopped by user")
    except Exception:
        log.exception("Bot crashed unexpectedly")


if __name__ == "__main__":
    asyncio.run(main())

# ✅ Done: main.py — Tiếp theo: cogs/logger.py
