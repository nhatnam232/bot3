"""
cogs/music.py — 24/7 Voice + Phát nhạc SoundCloud.
- /join, /leave, /247 <on|off> — treo voice 24/7 (tự vào lại nếu bị kick/disconnect)
- /play <query> — phát nhạc SoundCloud (search hoặc link trực tiếp)
- /skip, /stop, /nowplaying — điều khiển hàng đợi
- Dùng yt-dlp (hỗ trợ soundcloud) + FFmpeg để stream audio
- Lưu cấu hình 24/7 per-guild vào SQLite qua db.get_config/set_config
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("bot.music")

# yt-dlp: import mềm — nếu thiếu sẽ báo hướng dẫn cài, không làm sập bot
try:
    import yt_dlp
    YTDLP_OK = True
except ImportError:
    YTDLP_OK = False

# ============================================================
# ⚙️ CẤU HÌNH yt-dlp + FFmpeg
# ============================================================
YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "scsearch",   # mặc định search trên SoundCloud
    "source_address": "0.0.0.0",
    "extract_flat": False,
}

FFMPEG_OPTS = {
    # reconnect để stream ổn định khi mạng chập chờn
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS) if YTDLP_OK else None


class Track:
    """Một bài nhạc trong hàng đợi."""
    def __init__(self, url: str, title: str, duration: int, requester):
        self.url = url            # URL stream trực tiếp
        self.title = title
        self.duration = duration
        self.requester = requester


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
        """Vòng lặp: lấy bài kế tiếp trong queue → phát."""
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
                discord.FFmpegPCMAudio(track.url, **FFMPEG_OPTS),
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
    """Cog nhạc SoundCloud + treo voice 24/7."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.players: dict = {}

    # ---- Helpers ----
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

    async def _search(self, query: str):
        """Search/parse nhạc từ SoundCloud bằng yt-dlp (chạy trong thread)."""
        def _extract():
            data = ytdl.extract_info(query, download=False)
            if "entries" in data:      # kết quả search → lấy bài đầu
                data = data["entries"][0]
            return data
        data = await self.bot.loop.run_in_executor(None, _extract)
        if not data:
            return None
        return Track(data["url"], data.get("title", "Unknown"),
                     data.get("duration", 0), None)

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
    # 🎵 PHÁT NHẠC SOUNDCLOUD
    # ========================================================
    @app_commands.command(name="play", description="Phát nhạc SoundCloud (tên bài hoặc link)")
    @app_commands.describe(query="Tên bài hát hoặc link SoundCloud")
    async def play(self, interaction: discord.Interaction, query: str):
        if not YTDLP_OK:
            return await interaction.response.send_message(
                "❌ Thiếu thư viện `yt-dlp`. Kiểm tra requirements.txt.", ephemeral=True)
        vc = await self._ensure_voice(interaction)
        if not vc:
            return
        await interaction.response.defer()
        track = await self._search(query)
        if not track:
            return await interaction.followup.send("❌ Không tìm thấy bài nhạc.")
        track.requester = interaction.user
        player = self._get_player(interaction.guild)
        await player.queue.put(track)
        mins, secs = divmod(track.duration or 0, 60)
        await interaction.followup.send(
            f"🎵 Đã thêm vào hàng đợi: **{track.title}** `[{mins}:{secs:02d}]`")

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
                f"🎧 Đang phát: **{player.current.title}**", ephemeral=True)
        else:
            await interaction.response.send_message("📭 Không có bài nào đang phát.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
