"""
cogs/anti_spam.py — Module chống spam.
Cơ chế:
  1. Tin nhắn lặp: N tin giống nhau / window → xóa + warn
  2. Similarity check: >ngưỡng % giống nhau → tính là spam
  3. Mass mention: @everyone/@here hoặc mention quá nhiều → xóa + warn
  4. Emoji spam: > giới hạn emoji / tin → xóa + warn (đếm chuẩn theo grapheme)
  5. Caps spam: >% chữ hoa với tin dài → xóa + warn
  6. Auto slowmode: nhiều tin/giây trong channel → bật slowmode
  7. Mass spam: nhiều người spam cùng nội dung → lockdown channel
  8. Từ lặp trong câu: 1 từ lặp quá nhiều lần → xóa + warn
- Tin chỉ là GIF/sticker/ảnh → BỎ QUA check emoji/caps/duplicate (tránh oan member)
- Owner/Co-owner + whitelist bypass — check TRƯỚC mọi xử lý
- Mọi threshold lưu SQLite, chỉnh qua slash command
"""

import re
import time
import logging
from difflib import SequenceMatcher
from collections import defaultdict, deque, Counter

import discord
from discord.ext import commands

import config

log = logging.getLogger("bot.anti_spam")

# Regex emoji: custom emoji <a:name:id> + unicode emoji phổ biến
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
UNICODE_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # symbols, emoticons, transport, supplemental
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"   # cờ quốc gia
    "]",
    flags=re.UNICODE,
)
# Thành phần bổ trợ KHÔNG tính là emoji riêng
SKIN_TONE_RE = re.compile("[\U0001F3FB-\U0001F3FF]")   # tông da
VARIATION_RE = re.compile("[\uFE00-\uFE0F]")            # variation selector
REGIONAL_RE = re.compile("[\U0001F1E6-\U0001F1FF]")     # regional indicator (cờ)
ZWJ = "\u200d"                                            # zero-width joiner (ghép emoji)

# Link đơn (dùng để nhận diện tin chỉ là GIF/ảnh link)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


class AntiSpam(commands.Cog):
    """Cog chống spam — xử lý mọi tin nhắn qua on_message."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        # Lịch sử tin nhắn per user: {(guild_id, user_id): deque[(timestamp, content)]}
        self._user_msgs: dict = defaultdict(lambda: deque(maxlen=20))
        # Lịch sử tin per channel (auto slowmode): {channel_id: deque[timestamp]}
        self._channel_msgs: dict = defaultdict(lambda: deque(maxlen=50))
        # Mass spam detect: {(channel_id, content_norm): {user_id: timestamp}}
        self._mass_spam: dict = defaultdict(dict)
        # Cooldown để không warn 1 user dồn dập: {(guild_id, user_id): timestamp}
        self._warn_cooldown: dict = {}
        # Cooldown slowmode per channel
        self._slowmode_cooldown: dict = {}
        # Đã nhắc trong kênh chưa: set[(guild_id, user_id)] — chỉ nhắc 1 LẦN,
        # các lần sau chỉ ghi log (tránh loãng chat)
        self._reminded: set = set()

    # ========================================================
    # 🔧 HELPERS
    # ========================================================
    async def _is_bypassed(self, guild_id: int, user_id: int) -> bool:
        """Owner/Co-owner + whitelist bypass — check TRƯỚC mọi xử lý."""
        if config.is_owner_or_coowner(user_id):
            return True
        return await self.db.is_whitelisted(guild_id, user_id)

    @staticmethod
    def _is_media_only(message: discord.Message) -> bool:
        """Tin chỉ là GIF/sticker/ảnh (không phải spam chữ) → miễn check emoji/caps/dup."""
        if message.stickers:
            return True
        text = message.content.strip()
        if message.attachments and not text:
            return True
        # Chỉ là 1 link đơn (vd GIF Tenor/Giphy) → coi như media
        if text and URL_RE.fullmatch(text):
            return True
        return False

    @staticmethod
    def _count_emojis(content: str) -> int:
        """Đếm emoji theo 'grapheme' — gộp tông da / variation / ZWJ / cờ thành 1."""
        # Custom emoji <a:name:id> / <:name:id> — mỗi cái = 1
        custom = len(CUSTOM_EMOJI_RE.findall(content))
        text = CUSTOM_EMOJI_RE.sub("", content)
        # Cờ quốc gia: 2 regional indicator = 1 emoji
        flags = len(REGIONAL_RE.findall(text)) // 2
        text = REGIONAL_RE.sub("", text)
        # Bỏ tông da + variation selector (dính vào emoji gốc, không tính riêng)
        text = SKIN_TONE_RE.sub("", text)
        text = VARIATION_RE.sub("", text)
        # Emoji unicode gốc còn lại
        bases = UNICODE_EMOJI_RE.findall(text)
        # ZWJ gộp nhiều emoji thành 1 (vd 👨‍👩‍👧) → trừ số lần ghép
        zwj = text.count(ZWJ)
        unicode_count = max(0, len(bases) - zwj)
        return custom + flags + unicode_count

    async def _punish(self, message: discord.Message, reason: str):
        """Xóa tin + warn (cooldown 10s) — nhắc trong kênh CHỈ 1 LẦN, sau đó chỉ log."""
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

        # Cooldown warn: tối đa 1 warn / 10 giây / user
        key = (message.guild.id, message.author.id)
        now = time.time()
        if now - self._warn_cooldown.get(key, 0) < 10:
            return
        self._warn_cooldown[key] = now

        warn_cog = self.bot.get_cog("WarnSystem")
        if warn_cog:
            # Truyền kênh vi phạm → warn system tự dọn sạch tin gần đây của đối tượng
            await warn_cog.warn_user(
                message.author, self.bot.user.id, f"[AntiSpam] {reason}",
                channel=message.channel if isinstance(message.channel, discord.TextChannel) else None,
            )

        # Nhắc trong kênh CHỈ LẦN ĐẦU (tự xóa sau 5s) — tránh loãng chat;
        # các vi phạm sau chỉ ghi vào channel log
        if key not in self._reminded:
            self._reminded.add(key)
            try:
                await message.channel.send(
                    f"🚫 {message.author.mention} — {reason} *(nhắc lần duy nhất, "
                    f"tái phạm sẽ bị phạt thẳng)*",
                    delete_after=5,
                )
            except discord.HTTPException:
                pass
        else:
            # Đã nhắc rồi → chỉ ghi log, không gửi gì vào kênh chat
            logger = self.bot.get_cog("Logger")
            if logger:
                await logger.send_log(
                    message.guild, "🚫 AntiSpam (đã nhắc trước đó)",
                    f"{message.author.mention} tái phạm trong {message.channel.mention}: {reason}",
                    discord.Color.orange(), user=message.author,
                )

    @staticmethod
    def _normalize(content: str) -> str:
        """Chuẩn hóa nội dung để so sánh: lowercase + gộp whitespace."""
        return re.sub(r"\s+", " ", content.lower().strip())

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """% giống nhau giữa 2 chuỗi (0-100)."""
        return SequenceMatcher(None, a, b).ratio() * 100

    # ========================================================
    # 📨 MAIN HANDLER
    # ========================================================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            # Bỏ qua DM, bot, webhook
            if message.guild is None or message.author.bot:
                return
            guild = message.guild

            # Module tắt → bỏ qua
            if not await self.db.is_module_enabled(guild.id, "antispam"):
                return

            # 🔐 Check bypass TRƯỚC KHI xử lý bất kỳ check nào
            if await self._is_bypassed(guild.id, message.author.id):
                return

            # Tin chỉ là GIF/sticker/ảnh → KHÔNG chạy check cá nhân (tránh oan)
            media_only = self._is_media_only(message)

            # Mass mention luôn check (kể cả tin media)
            if await self._check_mass_mention(message):
                return

            if not media_only:
                if await self._check_emoji_spam(message):
                    return
                if await self._check_caps_spam(message):
                    return
                if await self._check_word_repeat(message):
                    return
                if await self._check_duplicate(message):
                    return

            # Các check cấp channel (không phạt cá nhân)
            await self._check_auto_slowmode(message)
            await self._check_mass_spam(message)
        except Exception:
            log.exception("Error in anti-spam on_message")

    # ========================================================
    # 1️⃣ + 2️⃣ TIN LẶP + SIMILARITY
    # ========================================================
    async def _check_duplicate(self, message: discord.Message) -> bool:
        content = self._normalize(message.content)
        if len(content) < 3:  # tin quá ngắn (ok, lol...) → bỏ qua
            return False

        guild_id = message.guild.id
        dup_limit = await self.db.get_config(guild_id, "antispam_dup_limit")
        dup_window = await self.db.get_config(guild_id, "antispam_dup_window")
        sim_threshold = await self.db.get_config(guild_id, "antispam_similarity")

        key = (guild_id, message.author.id)
        now = time.time()
        history = self._user_msgs[key]
        history.append((now, content))

        # Đếm tin giống nhau (>= sim_threshold %) trong window
        similar_count = 0
        for ts, old_content in history:
            if now - ts > dup_window:
                continue
            if old_content == content or self._similarity(old_content, content) >= sim_threshold:
                similar_count += 1

        if similar_count >= dup_limit:
            history.clear()  # reset tránh warn liên hoàn
            await self._punish(message, "Spam tin nhắn lặp lại!")
            return True
        return False

    # ========================================================
    # 3️⃣ MASS MENTION
    # ========================================================
    async def _check_mass_mention(self, message: discord.Message) -> bool:
        mention_limit = await self.db.get_config(message.guild.id, "antispam_mention_limit")

        # @everyone/@here — chặn cả khi user không có quyền ping (text thô)
        if message.mention_everyone or "@everyone" in message.content or "@here" in message.content:
            await self._punish(message, "Không được ping @everyone/@here!")
            return True

        total_mentions = len(message.mentions) + len(message.role_mentions)
        if total_mentions > mention_limit:
            await self._punish(message, f"Mention quá nhiều ({total_mentions} người/role)!")
            return True
        return False

    # ========================================================
    # 4️⃣ EMOJI SPAM (đếm chuẩn theo grapheme)
    # ========================================================
    async def _check_emoji_spam(self, message: discord.Message) -> bool:
        emoji_limit = await self.db.get_config(message.guild.id, "antispam_emoji_limit")
        count = self._count_emojis(message.content)
        if count > emoji_limit:
            await self._punish(message, f"Spam emoji ({count} emoji)!")
            return True
        return False

    # ========================================================
    # 5️⃣ CAPS SPAM
    # ========================================================
    async def _check_caps_spam(self, message: discord.Message) -> bool:
        guild_id = message.guild.id
        caps_percent = await self.db.get_config(guild_id, "antispam_caps_percent")
        min_len = await self.db.get_config(guild_id, "antispam_caps_minlen")

        letters = [c for c in message.content if c.isalpha()]
        if len(letters) <= min_len:
            return False
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters) * 100
        if upper_ratio > caps_percent:
            await self._punish(message, "Đừng spam chữ HOA!")
            return True
        return False

    # ========================================================
    # 8️⃣ TỪ LẶP TRONG CÂU
    # ========================================================
    async def _check_word_repeat(self, message: discord.Message) -> bool:
        repeat_limit = await self.db.get_config(message.guild.id, "antispam_word_repeat")
        words = self._normalize(message.content).split()
        if len(words) <= repeat_limit:
            return False
        counter = Counter(w for w in words if len(w) >= 2)  # bỏ từ 1 ký tự
        if counter and counter.most_common(1)[0][1] > repeat_limit:
            word, cnt = counter.most_common(1)[0]
            await self._punish(message, f"Spam lặp từ `{word}` ({cnt} lần)!")
            return True
        return False

    # ========================================================
    # 6️⃣ AUTO SLOWMODE — nhiều tin/giây trong channel
    # ========================================================
    async def _check_auto_slowmode(self, message: discord.Message):
        guild_id = message.guild.id
        msgs_limit = await self.db.get_config(guild_id, "antispam_slowmode_msgs")
        window = await self.db.get_config(guild_id, "antispam_slowmode_window")
        duration = await self.db.get_config(guild_id, "antispam_slowmode_duration")

        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return
        now = time.time()
        history = self._channel_msgs[channel.id]
        history.append(now)

        # Đếm tin trong window
        recent = sum(1 for ts in history if now - ts <= window)
        if recent <= msgs_limit:
            return
        # Cooldown 60s — không set slowmode liên tục
        if now - self._slowmode_cooldown.get(channel.id, 0) < 60:
            return
        if channel.slowmode_delay >= duration:
            return  # đã có slowmode

        try:
            await channel.edit(
                slowmode_delay=duration,
                reason=f"Auto slowmode: {recent} tin/{window}s",
            )
            self._slowmode_cooldown[channel.id] = now
            await channel.send(
                f"🐌 Channel đang quá nhanh — bật slowmode **{duration}s** tự động.",
                delete_after=10,
            )
            logger = self.bot.get_cog("Logger")
            if logger:
                await logger.send_log(
                    message.guild, "🐌 Auto slowmode",
                    f"{channel.mention} bật slowmode {duration}s ({recent} tin/{window}s).",
                    discord.Color.orange(),
                )
        except discord.Forbidden:
            log.warning("No permission to set slowmode in %s", channel.id)
        except Exception:
            log.exception("Error setting auto slowmode")

    # ========================================================
    # 7️⃣ MASS SPAM — nhiều người spam cùng nội dung → lockdown channel
    # ========================================================
    async def _check_mass_spam(self, message: discord.Message):
        content = self._normalize(message.content)
        if len(content) < 3:
            return
        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return

        guild_id = message.guild.id
        mass_users = await self.db.get_config(guild_id, "antispam_mass_users")
        mass_window = await self.db.get_config(guild_id, "antispam_mass_window")

        key = (channel.id, content)
        now = time.time()
        senders = self._mass_spam[key]
        senders[message.author.id] = now

        # Dọn user ngoài window
        for uid in [u for u, ts in senders.items() if now - ts > mass_window]:
            del senders[uid]

        if len(senders) >= mass_users:
            self._mass_spam.pop(key, None)
            await self._lockdown_channel(channel, mass_users, mass_window)

    async def _lockdown_channel(self, channel: discord.TextChannel, user_count: int, window: int):
        """Khóa send_messages của @everyone trong channel bị raid spam."""
        try:
            overwrite = channel.overwrites_for(channel.guild.default_role)
            if overwrite.send_messages is False:
                return  # đã khóa
            overwrite.send_messages = False
            await channel.set_permissions(
                channel.guild.default_role, overwrite=overwrite,
                reason=f"Mass spam: {user_count}+ người/{window}s",
            )
            await channel.send(
                f"🔒 **Channel bị khóa tự động** — phát hiện {user_count}+ người spam "
                f"cùng nội dung trong {window}s. Mod dùng `/unlock` để mở.",
            )
            logger = self.bot.get_cog("Logger")
            if logger:
                await logger.send_log(
                    channel.guild, "🔒 LOCKDOWN CHANNEL (mass spam)",
                    f"{channel.mention} bị khóa — {user_count}+ người spam cùng nội dung.",
                    discord.Color.red(),
                )
        except discord.Forbidden:
            log.warning("No permission to lockdown channel %s", channel.id)
        except Exception:
            log.exception("Error locking down channel")


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiSpam(bot))

# ✅ Done: cogs/anti_spam.py — Tiếp theo: cogs/anti_content.py
