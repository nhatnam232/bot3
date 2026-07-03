"""
cogs/anti_content.py — Module lọc nội dung.
Cơ chế:
  1. Block CJK Unicode (tiếng Trung/Nhật/Hàn) — bật mặc định
  2. Blacklist từ ngữ: global từ blacklist.txt + per-guild từ SQLite
  3. Anti phishing: domain độc hại cập nhật từ GitHub (refresh mỗi 6h)
  4. Anti Nitro scam: detect link giả discord/nitro (typosquat)
  5. Anti invite: block discord.gg từ server không whitelist
  6. Anti NSFW: keyword detect trong link/text
- Tin chỉ là GIF/sticker/ảnh + domain media hợp lệ (Tenor/Giphy/Discord) → BỎ QUA (tránh oan)
- Owner/Co-owner + whitelist bypass — check TRƯỚC mọi xử lý
"""

import re
import time
import logging
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands, tasks

import config

log = logging.getLogger("bot.anti_content")

# ============================================================
# REGEX & HẰNG SỐ
# ============================================================
# CJK: Trung (Hán tự), Nhật (Hiragana/Katakana), Hàn (Hangul)
CJK_RE = re.compile(
    "["
    "\u4e00-\u9fff"    # CJK Unified Ideographs (Trung)
    "\u3400-\u4dbf"    # CJK Extension A
    "\u3040-\u309f"    # Hiragana (Nhật)
    "\u30a0-\u30ff"    # Katakana (Nhật)
    "\uac00-\ud7af"    # Hangul Syllables (Hàn)
    "\u1100-\u11ff"    # Hangul Jamo
    "]"
)

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
INVITE_RE = re.compile(
    r"(?:discord\.gg|discord(?:app)?\.com/invite|dsc\.gg)/([a-zA-Z0-9-]+)",
    re.IGNORECASE,
)

# Domain discord HỢP PHÁP — mọi domain "giống discord" khác là scam
LEGIT_DISCORD_DOMAINS = {
    "discord.com", "discord.gg", "discordapp.com", "discordapp.net",
    "discord.new", "discordstatus.com", "discord.dev",
}
# Pattern Nitro scam: domain chứa các từ này nhưng không phải domain thật
NITRO_SCAM_KEYWORDS = ("discord", "nitro", "dlscord", "discrod", "disc0rd", "steamcommunity")
LEGIT_SCAM_EXCEPTIONS = LEGIT_DISCORD_DOMAINS | {"steamcommunity.com", "steampowered.com"}

# Domain GIF/media HỢP LỆ — miễn mọi check link (tránh chặn GIF Tenor/Giphy/Discord)
ALLOWED_MEDIA_DOMAINS = (
    "tenor.com", "media.tenor.com", "c.tenor.com",
    "giphy.com", "media.giphy.com",
    "media.discordapp.net", "cdn.discordapp.com",
    "images-ext-1.discordapp.net", "images-ext-2.discordapp.net",
)

# Keyword NSFW cơ bản (check trong link + text)
NSFW_KEYWORDS = (
    "porn", "pornhub", "xvideos", "xnxx", "hentai", "xxx",
    "nsfw", "onlyfans", "rule34", "redtube",
)

# Nguồn danh sách domain phishing (cập nhật từ GitHub)
PHISHING_LIST_URLS = [
    "https://raw.githubusercontent.com/Discord-AntiScam/scam-links/main/list.txt",
]


class AntiContent(commands.Cog):
    """Cog lọc nội dung — chạy SAU anti-spam trong on_message."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        # Blacklist global từ file
        self._global_blacklist: set[str] = set()
        # Domain phishing từ GitHub
        self._phishing_domains: set[str] = set()
        # Cache invite code của chính guild: {guild_id: (timestamp, set[code])}
        self._own_invites: dict = {}
        # Đã nhắc trong kênh chưa: set[(guild_id, user_id)] — chỉ nhắc 1 LẦN,
        # tái phạm chỉ ghi log (tránh loãng chat)
        self._reminded: set = set()

        self._load_blacklist_file()
        self.refresh_phishing_task.start()

    def cog_unload(self):
        self.refresh_phishing_task.cancel()

    # ========================================================
    # 🔧 LOAD DATA
    # ========================================================
    def _load_blacklist_file(self):
        """Load blacklist.txt (global) — gọi lại khi /blacklist reload."""
        try:
            with open(config.BLACKLIST_FILE, encoding="utf-8") as f:
                self._global_blacklist = {
                    line.strip().lower()
                    for line in f
                    if line.strip() and not line.startswith("#")
                }
            log.info("Loaded %d global blacklist words", len(self._global_blacklist))
        except FileNotFoundError:
            log.warning("blacklist.txt not found — global blacklist empty")
            self._global_blacklist = set()
        except Exception:
            log.exception("Failed to load blacklist.txt")

    @tasks.loop(hours=6)
    async def refresh_phishing_task(self):
        """Tải danh sách domain phishing từ GitHub mỗi 6 giờ."""
        domains: set[str] = set()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as session:
                for url in PHISHING_LIST_URLS:
                    try:
                        async with session.get(url) as resp:
                            if resp.status != 200:
                                continue
                            text = await resp.text()
                            domains.update(
                                line.strip().lower()
                                for line in text.splitlines()
                                if line.strip() and not line.startswith("#")
                            )
                    except aiohttp.ClientError:
                        log.warning("Failed to fetch phishing list: %s", url)
            if domains:
                self._phishing_domains = domains
                log.info("Loaded %d phishing domains", len(domains))
        except Exception:
            log.exception("Error refreshing phishing list")

    @refresh_phishing_task.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ========================================================
    # 🔧 HELPERS
    # ========================================================
    async def _is_bypassed(self, guild_id: int, user_id: int) -> bool:
        if config.is_owner_or_coowner(user_id):
            return True
        return await self.db.is_whitelisted(guild_id, user_id)

    @staticmethod
    def _is_allowed_media_domain(domain: str) -> bool:
        """Domain GIF/media hợp lệ (Tenor/Giphy/Discord CDN) — miễn check link."""
        return any(domain == d or domain.endswith("." + d) for d in ALLOWED_MEDIA_DOMAINS)

    @staticmethod
    def _is_media_only(message: discord.Message) -> bool:
        """Tin chỉ là GIF/sticker/ảnh → miễn toàn bộ check nội dung."""
        if message.stickers:
            return True
        text = message.content.strip()
        if message.attachments and not text:
            return True
        # Chỉ chứa 1 link đơn (vd GIF Tenor/Giphy) → coi như media
        if text and URL_RE.fullmatch(text):
            return True
        return False

    async def _punish(self, message: discord.Message, reason: str, warn: bool = True):
        """Xóa tin + warn — nhắc trong kênh CHỈ 1 LẦN, tái phạm chỉ ghi log."""
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass
        if warn:
            warn_cog = self.bot.get_cog("WarnSystem")
            if warn_cog:
                # Truyền kênh vi phạm → warn system tự dọn sạch tin gần đây của đối tượng
                await warn_cog.warn_user(
                    message.author, self.bot.user.id, f"[AntiContent] {reason}",
                    channel=message.channel if isinstance(message.channel, discord.TextChannel) else None,
                )

        key = (message.guild.id, message.author.id)
        if key not in self._reminded:
            self._reminded.add(key)
            try:
                await message.channel.send(
                    f"🚫 {message.author.mention} — {reason} *(nhắc lần duy nhất)*",
                    delete_after=5)
            except discord.HTTPException:
                pass
        else:
            # Đã nhắc rồi → chỉ ghi log, không gửi vào kênh chat
            logger = self.bot.get_cog("Logger")
            if logger:
                await logger.send_log(
                    message.guild, "🚫 AntiContent (đã nhắc trước đó)",
                    f"{message.author.mention} tái phạm trong {message.channel.mention}: {reason}",
                    discord.Color.orange(), user=message.author,
                )

    def reload_blacklist(self):
        """Cho config_commands gọi sau khi sửa blacklist.txt."""
        self._load_blacklist_file()

    @staticmethod
    def _extract_domains(content: str) -> list[str]:
        """Lấy domain từ mọi URL trong tin nhắn."""
        domains = []
        for url in URL_RE.findall(content):
            try:
                netloc = urlparse(url).netloc.lower()
                if netloc.startswith("www."):
                    netloc = netloc[4:]
                if netloc:
                    domains.append(netloc)
            except ValueError:
                continue
        return domains

    # ========================================================
    # 📨 MAIN HANDLER
    # ========================================================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            if message.guild is None or message.author.bot:
                return
            guild = message.guild

            if not await self.db.is_module_enabled(guild.id, "anticontent"):
                return
            # 🔐 Bypass check TRƯỚC mọi xử lý
            if await self._is_bypassed(guild.id, message.author.id):
                return

            # GIF/sticker/ảnh đơn thuần → bỏ qua mọi check nội dung (tránh oan member)
            if self._is_media_only(message):
                return

            content_lower = message.content.lower()
            domains = self._extract_domains(message.content)

            # Thứ tự: nguy hiểm nhất trước
            if await self._check_phishing(message, domains):
                return
            if await self._check_nitro_scam(message, domains):
                return
            if await self._check_invite(message):
                return
            if await self._check_nsfw(message, content_lower, domains):
                return
            if await self._check_blacklist(message, content_lower):
                return
            if await self._check_cjk(message):
                return
        except Exception:
            log.exception("Error in anti-content on_message")

    # ========================================================
    # 3️⃣ ANTI PHISHING — so với domain list từ GitHub
    # ========================================================
    async def _check_phishing(self, message: discord.Message, domains: list[str]) -> bool:
        if not await self.db.get_config(message.guild.id, "anticontent_phishing"):
            return False
        for domain in domains:
            if self._is_allowed_media_domain(domain):
                continue
            if domain in self._phishing_domains:
                await self._punish(message, "Link phishing/scam bị chặn!")
                logger = self.bot.get_cog("Logger")
                if logger:
                    await logger.send_log(
                        message.guild, "🎣 Phishing bị chặn",
                        f"{message.author.mention} gửi domain độc hại: `{domain}`",
                        discord.Color.red(), user=message.author,
                    )
                return True
        return False

    # ========================================================
    # 4️⃣ ANTI NITRO SCAM — domain giả discord/nitro (typosquat)
    # ========================================================
    async def _check_nitro_scam(self, message: discord.Message, domains: list[str]) -> bool:
        if not await self.db.get_config(message.guild.id, "anticontent_nitro_scam"):
            return False
        for domain in domains:
            if self._is_allowed_media_domain(domain):
                continue
            if domain in LEGIT_SCAM_EXCEPTIONS:
                continue
            # Domain chứa keyword nhạy cảm nhưng KHÔNG phải domain thật → scam
            if any(kw in domain for kw in NITRO_SCAM_KEYWORDS):
                await self._punish(message, "Link giả mạo Discord/Nitro bị chặn!")
                logger = self.bot.get_cog("Logger")
                if logger:
                    await logger.send_log(
                        message.guild, "💎 Nitro scam bị chặn",
                        f"{message.author.mention} gửi domain giả mạo: `{domain}`",
                        discord.Color.red(), user=message.author,
                    )
                return True
        return False

    # ========================================================
    # 5️⃣ ANTI INVITE — block discord.gg không whitelist
    # ========================================================
    async def _get_own_invite_codes(self, guild: discord.Guild) -> set[str]:
        """Cache invite của chính guild 5 phút — link mời vào chính server thì cho phép."""
        cached = self._own_invites.get(guild.id)
        now = time.time()
        if cached and now - cached[0] < 300:
            return cached[1]
        codes: set[str] = set()
        try:
            invites = await guild.invites()
            codes = {inv.code for inv in invites}
            if guild.vanity_url_code:
                codes.add(guild.vanity_url_code)
        except (discord.Forbidden, discord.HTTPException):
            pass
        self._own_invites[guild.id] = (now, codes)
        return codes

    async def _check_invite(self, message: discord.Message) -> bool:
        guild = message.guild
        if not await self.db.get_config(guild.id, "anticontent_invite_block"):
            return False
        codes = INVITE_RE.findall(message.content)
        if not codes:
            return False

        own_codes = await self._get_own_invite_codes(guild)
        for code in codes:
            # Cho phép: invite của chính server hoặc đã whitelist
            if code in own_codes:
                continue
            if await self.db.is_invite_whitelisted(guild.id, code):
                continue
            await self._punish(message, "Không được gửi invite server khác!")
            logger = self.bot.get_cog("Logger")
            if logger:
                await logger.send_log(
                    guild, "🔗 Invite lạ bị chặn",
                    f"{message.author.mention} gửi invite: `discord.gg/{code}`",
                    discord.Color.orange(), user=message.author,
                )
            return True
        return False

    # ========================================================
    # 6️⃣ ANTI NSFW — keyword detect trong text + domain
    # ========================================================
    async def _check_nsfw(
        self, message: discord.Message, content_lower: str, domains: list[str]
    ) -> bool:
        if not await self.db.get_config(message.guild.id, "anticontent_nsfw"):
            return False
        # Channel đã đánh dấu NSFW → bỏ qua
        if isinstance(message.channel, discord.TextChannel) and message.channel.is_nsfw():
            return False

        # Check domain chứa keyword NSFW (bỏ qua domain media hợp lệ)
        for domain in domains:
            if self._is_allowed_media_domain(domain):
                continue
            if any(kw in domain for kw in NSFW_KEYWORDS):
                await self._punish(message, "Nội dung NSFW bị chặn!")
                return True
        # Check keyword trong text (word boundary để tránh false positive)
        for kw in NSFW_KEYWORDS:
            if re.search(rf"\b{re.escape(kw)}\b", content_lower):
                await self._punish(message, "Nội dung NSFW bị chặn!")
                return True
        return False

    # ========================================================
    # 2️⃣ BLACKLIST TỪ NGỮ — file global + SQLite per-guild
    # ========================================================
    async def _check_blacklist(self, message: discord.Message, content_lower: str) -> bool:
        guild_words = await self.db.get_blacklist_words(message.guild.id)
        all_words = self._global_blacklist | guild_words
        if not all_words:
            return False
        for word in all_words:
            if word and word in content_lower:
                await self._punish(message, "Tin nhắn chứa từ ngữ bị cấm!")
                return True
        return False

    # ========================================================
    # 1️⃣ BLOCK CJK — tiếng Trung/Nhật/Hàn
    # ========================================================
    async def _check_cjk(self, message: discord.Message) -> bool:
        if not await self.db.get_config(message.guild.id, "anticontent_block_cjk"):
            return False
        if CJK_RE.search(message.content):
            await self._punish(message, "Không được dùng ký tự Trung/Nhật/Hàn!")
            return True
        return False


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiContent(bot))

# ✅ Done: cogs/anti_content.py — Tiếp theo: cogs/anti_raid.py
