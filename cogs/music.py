"""
cogs/music.py — 24/7 Voice + Phát nhạc đa nền tảng + Panel điều khiển.

Slash:
  /join, /leave, /247 <on|off> [channel] — treo voice 24/7 (có thể chọn kênh, không cần vào voice)
  /play <query> [channel] — phát nhạc thông minh (có thể chọn kênh)
  /randomsong [channel] — phát 1 bài ngẫu nhiên
  /panel — hiện bảng điều khiển nhạc
  /skip, /stop, /nowplaying
Prefix ('!'):
  !splay <query> — phát nhạc ngay
  !srandomsong — phát 1 bài ngẫu nhiên

Ghi chú /play:
  * Dán link YouTube / SoundCloud / Spotify -> tự nhận nền tảng
  * Ghim nền tảng bằng đuôi: 'tên bài -spotify' / '-yt' / '-sc'
  * Không ghi gì -> tự mò trên YouTube, chọn bản NHIỀU VIEW NHẤT (fallback SoundCloud)
  * Spotify không stream trực tiếp (DRM) -> lấy metadata rồi tìm trên YouTube
    (cần SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET)

FIX UDP (host chặn cổng ngẫu nhiên như Pterodactyl):
  Monkeypatch ép socket voice UDP bind vào đúng cổng được cấp (allocation).
  Đặt biến môi trường VOICE_UDP_PORT (mặc định 26236). CHỈ 1 voice cùng lúc.
"""

import os
import re
import random
import asyncio
import logging
import socket as _socket

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("bot.music")

# ============================================================
# 🔧 FIX UDP: ép socket voice bind vào cổng được cấp
# discord.py mở socket UDP ở cổng NGẪU NHIÊN -> host chặn -> timeout.
# Ta patch VoiceConnectionState._create_socket để bind cố định.
# Đổi cổng bằng biến môi trường VOICE_UDP_PORT (mặc định 26236).
# GIỚI HẠN: chỉ 1 kết nối voice tại một thời điểm (1 cổng).
# ============================================================
try:
    import discord.voice_state as _voice_state

    VOICE_UDP_PORT = int(os.getenv("VOICE_UDP_PORT", "26236"))

    def _patched_create_socket(self):
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        try:
            s.bind(("0.0.0.0", VOICE_UDP_PORT))
            log.info("🔌 Voice UDP bind vào cổng cố định %s", VOICE_UDP_PORT)
        except OSError as e:
            log.warning("Không bind được UDP %s (%s) -> dùng cổng ngẫu nhiên", VOICE_UDP_PORT, e)
        s.setblocking(False)
        self.socket = s
        self._socket_reader.resume()

    _voice_state.VoiceConnectionState._create_socket = _patched_create_socket
    log.info("✅ Đã patch _create_socket (bind UDP cố định %s)", VOICE_UDP_PORT)
except Exception:
    log.exception("Không patch được voice UDP bind (bỏ qua, dùng mặc định)")

# Tùy chỉnh thời gian chờ kết nối voice (giây)
VOICE_TIMEOUT = 20.0
VOICE_ERR = (
    "❌ Không kết nối được voice (handshake xong nhưng hết giờ ở bước UDP). "
    "Rất có thể host đang **chặn UDP outbound** — mà Discord voice bắt buộc cần UDP. "
    "Cần mở UDP outbound trên host hoặc dùng Lavalink ở nơi có UDP."
)

# yt-dlp: import mềm
try:
    import yt_dlp
    YTDLP_OK = True
except ImportError:
    YTDLP_OK = False

# ffmpeg từ imageio-ffmpeg (cài qua pip/uv), fallback 'ffmpeg' trong PATH
try:
    import imageio_ffmpeg
    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
    log.info("Dùng ffmpeg từ imageio-ffmpeg: %s", FFMPEG_EXE)
except Exception:
    FFMPEG_EXE = "ffmpeg"
    log.warning("Không tìm thấy imageio-ffmpeg, dùng 'ffmpeg' trong PATH")

# Spotify (tuỳ chọn) — CHỈ lấy metadata
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
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# Seed cho nhạc ngẫu nhiên
RANDOM_SEEDS = [
    "lofi hip hop", "vietnamese chill", "edm 2024", "pop hits 2024",
    "rap viet hay nhat", "acoustic viet", "phonk", "vinahouse",
    "kpop hits", "us uk trending", "nhac tre remix", "deep house",
]

# Nhận diện nền tảng
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
    if "open.spotify.com" in low:
        return q, "spotify", True
    if "youtube.com" in low or "youtu.be" in low:
        return q, "youtube", True
    if "soundcloud.com" in low:
        return q, "soundcloud", True
    m = _TAG_RE.search(q)
    if m:
        platform = PLATFORM_TAGS[m.group(1).lower()]
        clean = _TAG_RE.sub("", q).strip()
        return clean, platform, False
    return q, None, False


class Track:
    def __init__(self, url, title, duration, requester=None, webpage=None, views=None):
        self.url = url
        self.title = title
        self.duration = duration
        self.requester = requester
        self.webpage = webpage
        self.views = views


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
        await self.cog.bot.wait_until_ready()
        while True:
            self.next.clear()
            try:
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


# ============================================================
# 🎛️ PANEL ĐIỀU KHIỂN NHẠC (view bền vững sau restart)
# ============================================================
class MusicControls(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(emoji="⏯️", label="Phát/Dừng", style=discord.ButtonStyle.secondary, custom_id="music:pause")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Tạm dừng.", ephemeral=True)
        elif vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Tiếp tục.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Không có nhạc đang phát.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="Skip", style=discord.ButtonStyle.primary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message("⏭️ Đã skip.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Không có bài nào đang phát.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", label="Stop", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog.players.get(interaction.guild.id)
        vc = interaction.guild.voice_client
        if player:
            while not player.queue.empty():
                player.queue.get_nowait()
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await interaction.response.send_message("⏹️ Đã dừng & xóa hàng đợi.", ephemeral=True)

    @discord.ui.button(emoji="🔀", label="Random", style=discord.ButtonStyle.success, custom_id="music:random")
    async def random_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        target = await self.cog._target_channel(interaction.guild, interaction.user)
        if not target:
            return await interaction.response.send_message(
                "❌ Vào voice hoặc bật 24/7 trước.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        track = await self.cog._random_track()
        if not track:
            return await interaction.followup.send("❌ Không lấy được bài ngẫu nhiên.", ephemeral=True)
        vc = await self.cog._connect_to(interaction.guild, target)
        if not vc:
            return await interaction.followup.send(VOICE_ERR, ephemeral=True)
        track.requester = interaction.user
        player = self.cog._get_player(interaction.guild)
        await player.queue.put(track)
        await interaction.followup.send(f"🔀 Đã thêm ngẫu nhiên: **{track.title}**", ephemeral=True)

    @discord.ui.button(emoji="🔁", label="24/7", style=discord.ButtonStyle.secondary, custom_id="music:247")
    async def toggle_247(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        if await self.cog._is_247(gid):
            await self.cog.db.set_config(gid, "music_247", 0)
            await interaction.response.send_message("🔓 Đã TẮT 24/7.", ephemeral=True)
        else:
            target = await self.cog._target_channel(interaction.guild, interaction.user)
            if not target:
                return await interaction.response.send_message(
                    "❌ Vào voice trước.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            vc = await self.cog._connect_to(interaction.guild, target)
            if not vc:
                return await interaction.followup.send(VOICE_ERR, ephemeral=True)
            await self.cog.db.set_config(gid, "music_247", 1)
            await self.cog.db.set_config(gid, "music_247_channel", target.id)
            self.cog._get_player(interaction.guild)
            await interaction.followup.send(
                f"🔒 Đã BẬT 24/7 tại **{target.name}**.", ephemeral=True)


class Music(commands.Cog):
    """Cog nhạc đa nền tảng + treo voice 24/7 + panel."""

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

    async def _connect_to(self, guild: discord.Guild, channel):
        """Kết nối (hoặc di chuyển) bot tới voice channel. Trả None nếu lỗi/timeout."""
        vc = guild.voice_client
        try:
            if vc is None:
                return await channel.connect(self_deaf=True, timeout=VOICE_TIMEOUT, reconnect=False)
            if vc.channel != channel:
                await vc.move_to(channel)
            return vc
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("Voice connect TIMEOUT (host chặn UDP?) guild=%s channel=%s", guild.id, getattr(channel, "id", "?"))
            try:
                if guild.voice_client:
                    await guild.voice_client.disconnect(force=True)
            except Exception:
                pass
            return None
        except Exception:
            log.exception("Lỗi kết nối voice guild=%s", guild.id)
            return None

    async def _target_channel(self, guild: discord.Guild, user, explicit=None):
        """Chọn voice channel đích: explicit > user đang ở > kênh 24/7 đã lưu > bot đang ở."""
        if explicit:
            return explicit
        if user and getattr(user, "voice", None) and user.voice.channel:
            return user.voice.channel
        try:
            cid = await self.db.get_config(guild.id, "music_247_channel")
        except Exception:
            cid = None
        if cid:
            ch = guild.get_channel(cid)
            if ch:
                return ch
        if guild.voice_client and guild.voice_client.channel:
            return guild.voice_client.channel
        return None

    def _track_embed(self, track, title="🎵 Đã thêm vào hàng đợi"):
        mins, secs = divmod(track.duration or 0, 60)
        embed = discord.Embed(title=title, description=f"**{track.title}**", color=0x9b59b6)
        embed.add_field(name="Thời lượng", value=f"{mins}:{secs:02d}")
        if track.views:
            embed.add_field(name="Lượt xem", value=f"{track.views:,}")
        if track.webpage:
            embed.add_field(name="Nguồn", value=f"[Mở link]({track.webpage})", inline=False)
        return embed

    # ---- Resolver nhạc ----
    async def _extract(self, target):
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

    def _watch_url(self, entry):
        vid = entry.get("id") or entry.get("url")
        if vid and "http" not in str(vid):
            return "https://www.youtube.com/watch?v=" + str(vid)
        return vid

    async def _yt_best(self, q):
        """Search YouTube top 10 -> chọn bản NHIỀU VIEW NHẤT."""
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
        return await self._extract(self._watch_url(top))

    async def _random_track(self):
        """Chọn 1 seed ngẫu nhiên -> lấy 1 bài ngẫu nhiên trong top kết quả."""
        seed = random.choice(RANDOM_SEEDS)
        def _rank():
            try:
                info = ytdl_flat.extract_info(f"ytsearch15:{seed}", download=False)
            except Exception:
                return None
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                return None
            return random.choice(entries)
        top = await self.bot.loop.run_in_executor(None, _rank)
        if not top:
            return None
        return await self._extract(self._watch_url(top))

    async def _spotify_query(self, q, is_url):
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
        """Trả về (Track|None, err|None)."""
        clean, platform, is_url = detect_platform(query)
        if platform == "spotify":
            if not SPOTIFY_OK:
                return None, "spotify"
            name = await self._spotify_query(clean, is_url)
            if not name:
                return None, None
            return await self._yt_best(name), None
        if platform == "youtube":
            if is_url:
                return await self._extract(clean), None
            return await self._yt_best(clean), None
        if platform == "soundcloud":
            if is_url:
                return await self._extract(clean), None
            return await self._extract(f"scsearch1:{clean}"), None
        track = await self._yt_best(clean)
        if track is None:
            track = await self._extract(f"scsearch1:{clean}")
        return track, None

    async def _play_core(self, guild, channel, requester, query):
        """Resolve + connect + enqueue. Trả về (Track|None, err|None)."""
        track, err = await self._resolve(query)
        if err:
            return None, err
        if not track:
            return None, "notfound"
        vc = await self._connect_to(guild, channel)
        if not vc:
            return None, "voice"
        track.requester = requester
        player = self._get_player(guild)
        await player.queue.put(track)
        return track, None

    # ========================================================
    # 🔊 24/7 VOICE
    # ========================================================
    @app_commands.command(name="join", description="Bot vào voice channel của bạn")
    async def join(self, interaction: discord.Interaction):
        target = await self._target_channel(interaction.guild, interaction.user)
        if not target:
            return await interaction.response.send_message(
                "❌ Bạn phải vào một voice channel trước.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        vc = await self._connect_to(interaction.guild, target)
        if not vc:
            return await interaction.followup.send(VOICE_ERR, ephemeral=True)
        await interaction.followup.send(f"✅ Đã vào **{target.name}**.", ephemeral=True)

    @app_commands.command(name="leave", description="Bot rời voice channel")
    async def leave(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc:
            await self.db.set_config(interaction.guild.id, "music_247", 0)
            player = self.players.pop(interaction.guild.id, None)
            if player and player.task:
                player.task.cancel()
            await vc.disconnect(force=True)
            await interaction.response.send_message("👋 Đã rời voice.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Bot không ở trong voice.", ephemeral=True)

    @app_commands.command(name="247", description="Bật/tắt treo voice 24/7 (có thể chọn kênh, không cần vào voice)")
    @app_commands.describe(mode="on = bật, off = tắt", channel="Voice channel muốn treo (tùy chọn)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def stay247(self, interaction: discord.Interaction,
                      mode: app_commands.Choice[str],
                      channel: discord.VoiceChannel = None):
        if mode.value == "on":
            target = channel or await self._target_channel(interaction.guild, interaction.user)
            if not target:
                return await interaction.response.send_message(
                    "❌ Chọn 1 voice channel hoặc vào voice trước.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            vc = await self._connect_to(interaction.guild, target)
            if not vc:
                return await interaction.followup.send(VOICE_ERR, ephemeral=True)
            await self.db.set_config(interaction.guild.id, "music_247", 1)
            await self.db.set_config(interaction.guild.id, "music_247_channel", target.id)
            self._get_player(interaction.guild)
            await interaction.followup.send(
                f"🔒 Đã BẬT 24/7 tại **{target.name}**. Bot sẽ tự vào lại nếu bị ngắt.",
                ephemeral=True)
        else:
            await self.db.set_config(interaction.guild.id, "music_247", 0)
            await interaction.response.send_message("🔓 Đã TẮT chế độ 24/7.", ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Tự vào lại voice nếu 24/7 đang bật mà bot bị kick/disconnect."""
        if member.id != self.bot.user.id:
            return
        if before.channel and after.channel is None:
            guild = before.channel.guild
            if await self._is_247(guild.id):
                channel_id = await self.db.get_config(guild.id, "music_247_channel")
                channel = guild.get_channel(channel_id) if channel_id else before.channel
                if channel:
                    await asyncio.sleep(3)
                    vc = await self._connect_to(guild, channel)
                    if vc:
                        log.info("🔁 Tự vào lại voice 24/7: %s", guild.name)
                    else:
                        log.warning("Không thể vào lại voice 24/7 (UDP?): %s", guild.name)

    # ========================================================
    # 🎵 PHÁT NHẠC (slash)
    # ========================================================
    @app_commands.command(
        name="play",
        description="Phát nhạc (YouTube/SoundCloud/Spotify — tự mò nếu chỉ ghi tên)")
    @app_commands.describe(
        query="Tên bài hoặc link. Ghim nền tảng: 'tên bài -spotify' / '-yt' / '-sc'",
        channel="Voice channel muốn phát (tùy chọn, không cần bạn vào voice)")
    async def play(self, interaction: discord.Interaction, query: str,
                   channel: discord.VoiceChannel = None):
        if not YTDLP_OK:
            return await interaction.response.send_message(
                "❌ Thiếu thư viện `yt-dlp`. Kiểm tra requirements.txt.", ephemeral=True)
        target = await self._target_channel(interaction.guild, interaction.user, channel)
        if not target:
            return await interaction.response.send_message(
                "❌ Chọn 1 voice channel hoặc vào voice trước.", ephemeral=True)
        await interaction.response.defer()
        track, err = await self._play_core(interaction.guild, target, interaction.user, query)
        if err == "spotify":
            return await interaction.followup.send(
                "❌ Spotify chưa được cấu hình. Cần đặt `SPOTIFY_CLIENT_ID` và "
                "`SPOTIFY_CLIENT_SECRET`. Tạm dùng YouTube/SoundCloud hoặc dán link nhé.")
        if err == "voice":
            return await interaction.followup.send(VOICE_ERR)
        if err or not track:
            return await interaction.followup.send("❌ Không tìm thấy bài nhạc.")
        await interaction.followup.send(embed=self._track_embed(track), view=MusicControls(self))

    @app_commands.command(name="randomsong", description="Phát 1 bài nhạc ngẫu nhiên")
    @app_commands.describe(channel="Voice channel muốn phát (tùy chọn)")
    async def randomsong(self, interaction: discord.Interaction,
                         channel: discord.VoiceChannel = None):
        if not YTDLP_OK:
            return await interaction.response.send_message("❌ Thiếu yt-dlp.", ephemeral=True)
        target = await self._target_channel(interaction.guild, interaction.user, channel)
        if not target:
            return await interaction.response.send_message(
                "❌ Chọn 1 voice channel hoặc vào voice trước.", ephemeral=True)
        await interaction.response.defer()
        track = await self._random_track()
        if not track:
            return await interaction.followup.send("❌ Không lấy được bài ngẫu nhiên.")
        vc = await self._connect_to(interaction.guild, target)
        if not vc:
            return await interaction.followup.send(VOICE_ERR)
        track.requester = interaction.user
        player = self._get_player(interaction.guild)
        await player.queue.put(track)
        await interaction.followup.send(
            embed=self._track_embed(track, "🔀 Ngẫu nhiên"), view=MusicControls(self))

    @app_commands.command(name="panel", description="Hiện bảng điều khiển nhạc")
    async def panel(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild.id)
        desc = "Không có bài nào đang phát."
        if player and player.current:
            desc = f"🎧 Đang phát: **{player.current.title}**"
        embed = discord.Embed(title="🎛️ Bảng điều khiển nhạc", description=desc, color=0x9b59b6)
        await interaction.response.send_message(embed=embed, view=MusicControls(self))

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
            await interaction.response.send_message(
                embed=self._track_embed(player.current, "🎧 Đang phát"),
                view=MusicControls(self))
        else:
            await interaction.response.send_message("📭 Không có bài nào đang phát.", ephemeral=True)

    # ========================================================
    # 📝 PHÁT NHẠC (prefix '!')
    # ========================================================
    @commands.command(name="splay")
    async def splay(self, ctx: commands.Context, *, query: str = None):
        """!splay <tên bài / link> — phát nhạc ngay."""
        if not YTDLP_OK:
            return await ctx.send("❌ Thiếu thư viện yt-dlp.")
        if not query:
            return await ctx.send("ℹ️ Dùng: `!splay <tên bài hoặc link>`")
        target = await self._target_channel(ctx.guild, ctx.author)
        if not target:
            return await ctx.send("❌ Vào voice hoặc bật 24/7 trước đã.")
        msg = await ctx.send(f"🔎 Đang tìm: **{query}**...")
        track, err = await self._play_core(ctx.guild, target, ctx.author, query)
        if err == "spotify":
            return await msg.edit(content="❌ Spotify chưa cấu hình (thiếu CLIENT_ID/SECRET).")
        if err == "voice":
            return await msg.edit(content=VOICE_ERR)
        if err or not track:
            return await msg.edit(content="❌ Không tìm thấy bài nhạc.")
        await msg.edit(content=None, embed=self._track_embed(track), view=MusicControls(self))

    @commands.command(name="srandomsong")
    async def srandomsong(self, ctx: commands.Context):
        """!srandomsong — phát 1 bài ngẫu nhiên."""
        if not YTDLP_OK:
            return await ctx.send("❌ Thiếu thư viện yt-dlp.")
        target = await self._target_channel(ctx.guild, ctx.author)
        if not target:
            return await ctx.send("❌ Vào voice hoặc bật 24/7 trước đã.")
        msg = await ctx.send("🎲 Đang chọn bài ngẫu nhiên...")
        track = await self._random_track()
        if not track:
            return await msg.edit(content="❌ Không lấy được bài ngẫu nhiên.")
        vc = await self._connect_to(ctx.guild, target)
        if not vc:
            return await msg.edit(content=VOICE_ERR)
        track.requester = ctx.author
        player = self._get_player(ctx.guild)
        await player.queue.put(track)
        await msg.edit(content=None,
                       embed=self._track_embed(track, "🔀 Ngẫu nhiên"),
                       view=MusicControls(self))


async def setup(bot: commands.Bot):
    cog = Music(bot)
    await bot.add_cog(cog)
    try:
        bot.add_view(MusicControls(cog))
    except Exception:
        log.exception("Không đăng ký được MusicControls view")
