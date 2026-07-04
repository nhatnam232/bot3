"""
cogs/music.py — 24/7 Voice + Phát nhạc đa nền tảng (SoundCloud / YouTube / Spotify).
- /join, /leave, /247 <on|off> — treo voice 24/7 (tự vào lại nếu bị kick/disconnect)
- /play <query> — phát nhạc THÔNG MINH:
    * Dán link YouTube / SoundCloud / Spotify -> tự nhận nền tảng
    * Ghim nền tảng bằng đuôi: 'tên bài -spotify', 'tên bài -yt', 'tên bài -sc'
    * Không ghi gì -> tự mò trên YouTube và chọn bản NHIỀU VIEW NHẤT (fallback SoundCloud)
- /skip, /stop, /nowplaying — điều khiển hàng đợi
- FFmpeg lấy từ imageio-ffmpeg (cài qua pip/uv)
- Spotify không stream trực tiếp (DRM) -> lấy metadata rồi tìm trên YouTube
  (cần SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET trong biến môi trường)
"""

import os
import re
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("bot.music")

# yt-dlp: import mềm — nếu thiếu sẽ báo, không làm sập bot
try:
    import yt_dlp
    YTDLP_OK = True
except ImportError:
    YTDLP_OK = False

# ffmpeg: lấy binary từ imageio-ffmpeg (cài qua pip/uv), fallback 'ffmpeg' trong PATH
try:
    import imageio_ffmpeg
    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
    log.info("Dùng ffmpeg từ imageio-ffmpeg: %s", FFMPEG_EXE)
except Exception:
    FFMPEG_EXE = "ffmpeg"
    log.warning("Không tìm thấy imageio-ffmpeg, dùng 'ffmpeg' trong PATH")

# Spotify (tuỳ chọn) — CHỈ để lấy metadata, KHÔNG stream nhạc
SPOTIFY_OK = False
sp = None
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    _cid = os.getenv("SPOTIFY_CLIENT_ID")
    _csecret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if _cid and _csecret:
        sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(client_id=_cid, client_secret=_csecret))
        SPOTIFY_OK = True
        log.info("Spotify metadata đã sẵn sàng")
except Exception:
    SPOTIFY_OK = False

# ============================================================
# ⚙️ CẤU HÌNH yt-dlp + FFmpeg
# ============================================================
YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    "extract_flat": False,
}
# Opts nhẹ để XẾP HẠNG kết quả search (chỉ lấy metadata: view_count, id...)
YTDL_FLAT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "noplaylist": True,
    "source_address": "0.0.0.0",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS) if YTDLP_OK else None
ytdl_flat = yt_dlp.YoutubeDL(YTDL_FLAT_OPTS) if YTDLP_OK else None

FFMPEG_OPTS = {
    # reconnect để stream ổn định khi mạng chập chờn
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# ============================================================
# 🏷️ NHẬN DIỆN NỀN TẢNG
# ============================================================
PLATFORM_TAGS = {
    "spotify": "spotify",
    "youtube": "youtube", "yt": "youtube",
    "soundcloud": "soundcloud", "sc": "soundcloud",
}
_TAG_RE = re.compile(r"[-\s]+(spotify|youtube|yt|soundcloud|sc)\s*$", re.IGNORECASE)


def detect_platform(query: str):
    """Trả về (query_sạch, platform|None, is_url)."""
    q = query.strip()
    low = q.lower()
    # 1) Link trực tiếp
    if "open.spotify.com" in low:
        return q, "spotify", True
    if "youtube.com" in low or "youtu.be" in low:
        return q, "youtube", True
    if "soundcloud.com" in low:
        return q, "soundcloud", True
    # 2) Ghim nền tảng bằng đuôi: '... -spotify' / '... -yt' / '... -sc'
    m = _TAG_RE.search(q)
    if m:
        platform = PLATFORM_TAGS[m.group(1).lower()]
        clean = _TAG_RE.sub("", q).strip()
        return clean, platform, False
    # 3) Không ghim -> để None (tự mò)
    return q, None, False


class Track:
    """Một bài nhạc trong hàng đợi."""
    def __init__(self, url, title, duration, requester=None, webpage=None, views=None):
        self.url = url            # URL stream trực tiếp
        self.title = title
        self.duration = duration
        self.requester = requester
        self.webpage = webpage    # link trang gốc
        self.views = views        # lượt xem (nếu có)


class MusicPlayer:
    """Quản lý hàng đợi + phát nhạc cho 1 guild."""
    def __init__(self, cog, guild: discord.Guild):
        self.cog = cog
        self.guild = guild
        self.queue: asyncio.Queue = asyncio.Queue()
        self.next = asyncio.Event()
        self.current = None
        self.volume = 0.5
        self.task = cog.bot.loop.create_task(self._player_loop())

    async def _player_loop(self):
        """Vòng lặp: lấy bài kế tiếp trong queue -> phát."""
        await self.cog.bot.wait_until_ready()
        while True:
            self.next.clear()
            try:
                # Chờ tối đa 5 phút, không có bài mới thì thoát (trừ khi bật 24/7)
                async with asyncio.timeout(300):
                    track = await self.queue.get()
            except (asyncio.TimeoutError, TimeoutError):
                if not await self.cog._is_247(self.guild.id):
                    return await self._cleanup()
                continue

            self.current = track
            vc = self.guild.voice_client
            if vc is None:
                continue

            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(track.url, executable=FFMPEG_EXE, **FFMPEG_OPTS),
                volume=self.volume,
            )
            vc.play(source, after=lambda e: self.cog.bot.loop.call_soon_threadsafe(self.next.set))
            log.info("Đang phát: %s (guild %s)", track.title, self.guild.id)
            await self.next.wait()
            self.current = None

    async def _cleanup(self):
        vc = self.guild.voice_client
        if vc:
            await vc.disconnect(force=True)
        self.cog.players.pop(self.guild.id, None)
        if self.task:
            self.task.cancel()


class Music(commands.Cog):
    """Cog nhạc đa nền tảng + treo voice 24/7."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.players: dict = {}

    # ---- Helpers cơ bản ----
    async def _is_247(self, guild_id: int) -> bool:
        try:
            return bool(await self.db.get_config(guild_id, "music_247"))
        except Exception:
            return False

    def _get_player(self, guild: discord.Guild) -> "MusicPlayer":
        player = self.players.get(guild.id)
        if player is None:
            player = MusicPlayer(self, guild)
            self.players[guild.id] = player
        return player

    async def _ensure_voice(self, interaction: discord.Interaction):
        """Đảm bảo bot đang ở voice cùng người dùng."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "❌ Bạn phải vào một voice channel trước.", ephemeral=True)
            return None
        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        if vc is None:
            return await channel.connect(self_deaf=True)
        if vc.channel != channel:
            await vc.move_to(channel)
        return vc

    # ---- Resolver nhạc ----
    async def _extract(self, target):
        """Full-extract 1 nguồn (link/scsearch...) -> Track (chạy trong thread)."""
        def _do():
            try:
                data = ytdl.extract_info(target, download=False)
            except Exception:
                return None
            if not data:
                return None
            if "entries" in data:
                entries = [e for e in data["entries"] if e]
                if not entries:
                    return None
                data = entries[0]
            return data
        data = await self.bot.loop.run_in_executor(None, _do)
        if not data:
            return None
        return Track(data.get("url"), data.get("title", "Unknown"),
                     data.get("duration", 0), None,
                     data.get("webpage_url"), data.get("view_count"))

    async def _yt_best(self, q):
        """Search YouTube top 10 -> chọn bản NHIỀU VIEW NHẤT -> Track."""
        def _rank():
            try:
                info = ytdl_flat.extract_info(f"ytsearch10:{q}", download=False)
            except Exception:
                return None
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                return None
            entries.sort(key=lambda e: e.get("view_count") or 0, reverse=True)
            return entries[0]
        top = await self.bot.loop.run_in_executor(None, _rank)
        if not top:
            return None
        vid = top.get("id") or top.get("url")
        if vid and "http" not in str(vid):
            watch = f"https://www.youtube.com/watch?v={vid}"
        else:
            watch = vid
        return await self._extract(watch)

    async def _spotify_query(self, q, is_url):
        """Lấy chuỗi 'nghệ sĩ - tên bài' từ Spotify để đi tìm trên YouTube."""
        if not SPOTIFY_OK:
            return None
        def _do():
            try:
                if is_url:
                    tr = sp.track(q)
                else:
                    res = sp.search(q=q, type="track", limit=1)
                    items = res.get("tracks", {}).get("items", [])
                    if not items:
                        return None
                    tr = items[0]
                artists = ", ".join(a["name"] for a in tr["artists"])
                return f"{artists} - {tr['name']}"
            except Exception:
                return None
        return await self.bot.loop.run_in_executor(None, _do)

    async def _resolve(self, query):
        """Nhận diện nền tảng + trả về (Track|None, err|None)."""
        clean, platform, is_url = detect_platform(query)

        if platform == "spotify":
            if not SPOTIFY_OK:
                return None, "spotify"
            name = await self._spotify_query(clean, is_url)
            if not name:
                return None, None
            return await self._yt_best(name), None  # Spotify -> tìm trên YouTube

        if platform == "youtube":
            if is_url:
                return await self._extract(clean), None
            return await self._yt_best(clean), None

        if platform == "soundcloud":
            if is_url:
                return await self._extract(clean), None
            return await self._extract(f"scsearch1:{clean}"), None

        # Không ghim nền tảng -> YouTube nhiều view nhất, fallback SoundCloud
        track = await self._yt_best(clean)
        if track is None:
            track = await self._extract(f"scsearch1:{clean}")
        return track, None

    # ========================================================
    # 🔊 24/7 VOICE
    # ========================================================
    @app_commands.command(name="join", description="Bot vào voice channel của bạn")
    async def join(self, interaction: discord.Interaction):
        vc = await self._ensure_voice(interaction)
        if vc:
            await interaction.response.send_message(
                f"✅ Đã vào **{vc.channel.name}**.", ephemeral=True)

    @app_commands.command(name="leave", description="Bot rời voice channel")
    async def leave(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc:
            await self.db.set_config(interaction.guild.id, "music_247", 0)  # tắt 24/7
            player = self.players.pop(interaction.guild.id, None)
            if player and player.task:
                player.task.cancel()
            await vc.disconnect(force=True)
            await interaction.response.send_message("👋 Đã rời voice.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Bot không ở trong voice.", ephemeral=True)

    @app_commands.command(name="247", description="Bật/tắt chế độ treo voice 24/7")
    @app_commands.describe(mode="on = bật, off = tắt")
    @app_commands.choices(mode=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def stay247(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if mode.value == "on":
            vc = await self._ensure_voice(interaction)
            if not vc:
                return
            await self.db.set_config(interaction.guild.id, "music_247", 1)
            await self.db.set_config(interaction.guild.id, "music_247_channel", vc.channel.id)
            self._get_player(interaction.guild)  # khởi tạo player để giữ kết nối
            await interaction.response.send_message(
                f"🔒 Đã BẬT 24/7 tại **{vc.channel.name}**. Bot sẽ tự vào lại nếu bị ngắt.",
                ephemeral=True)
        else:
            await self.db.set_config(interaction.guild.id, "music_247", 0)
            await interaction.response.send_message("🔓 Đã TẮT chế độ 24/7.", ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Tự vào lại voice nếu 24/7 đang bật mà bot bị kick/disconnect."""
        if member.id != self.bot.user.id:
            return
        if before.channel and after.channel is None:  # bot vừa bị ngắt
            guild = before.channel.guild
            if await self._is_247(guild.id):
                channel_id = await self.db.get_config(guild.id, "music_247_channel")
                channel = guild.get_channel(channel_id) if channel_id else before.channel
                if channel:
                    await asyncio.sleep(3)  # chờ chút rồi vào lại
                    try:
                        await channel.connect(self_deaf=True)
                        log.info("🔁 Tự vào lại voice 24/7: %s", guild.name)
                    except Exception:
                        log.exception("Không thể vào lại voice 24/7")

    # ========================================================
    # 🎵 PHÁT NHẠC (đa nền tảng)
    # ========================================================
    @app_commands.command(
        name="play",
        description="Phát nhạc (YouTube/SoundCloud/Spotify — tự mò nếu chỉ ghi tên)")
    @app_commands.describe(
        query="Tên bài hoặc link. Ghim nền tảng: 'tên bài -spotify' / '-yt' / '-sc'")
    async def play(self, interaction: discord.Interaction, query: str):
        if not YTDLP_OK:
            return await interaction.response.send_message(
                "❌ Thiếu thư viện `yt-dlp`. Kiểm tra requirements.txt.", ephemeral=True)
        vc = await self._ensure_voice(interaction)
        if not vc:
            return
        await interaction.response.defer()
        track, err = await self._resolve(query)
        if err == "spotify":
            return await interaction.followup.send(
                "❌ Spotify chưa được cấu hình. Cần đặt biến môi trường "
                "`SPOTIFY_CLIENT_ID` và `SPOTIFY_CLIENT_SECRET`. "
                "Tạm thời dùng YouTube/SoundCloud hoặc dán link trực tiếp nhé.")
        if not track:
            return await interaction.followup.send("❌ Không tìm thấy bài nhạc.")
        track.requester = interaction.user
        player = self._get_player(interaction.guild)
        await player.queue.put(track)
        mins, secs = divmod(track.duration or 0, 60)
        views = f" · {track.views:,} views" if track.views else ""
        await interaction.followup.send(
            f"🎵 Đã thêm vào hàng đợi: **{track.title}** `[{mins}:{secs:02d}]`{views}")

    @app_commands.command(name="skip", description="Bỏ qua bài đang phát")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message("⏭️ Đã skip.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Không có bài nào đang phát.", ephemeral=True)

    @app_commands.command(name="stop", description="Dừng nhạc & xóa hàng đợi")
    async def stop(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild.id)
        vc = interaction.guild.voice_client
        if player:
            while not player.queue.empty():
                player.queue.get_nowait()
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await interaction.response.send_message("⏹️ Đã dừng & xóa hàng đợi.", ephemeral=True)

    @app_commands.command(name="nowplaying", description="Xem bài đang phát")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild.id)
        if player and player.current:
            t = player.current
            views = f" · {t.views:,} views" if t.views else ""
            link = f"\n{t.webpage}" if t.webpage else ""
            await interaction.response.send_message(
                f"🎧 Đang phát: **{t.title}**{views}{link}", ephemeral=True)
        else:
            await interaction.response.send_message("📭 Không có bài nào đang phát.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
