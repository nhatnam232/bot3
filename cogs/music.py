"""
cogs/music.py — Nhạc qua LAVALINK (wavelink) + treo 24/7 + Panel điều khiển.

VÌ SAO DÙNG LAVALINK:
  Host chặn UDP outbound -> bot KHÔNG tự vào voice được (Discord voice cần UDP).
  Lavalink chạy ở nơi có UDP; bot chỉ nói TCP/WebSocket tới Lavalink -> né chặn.
  Luồng:  Bot --TCP/WS--> Lavalink --UDP--> Discord voice

CẤU HÌNH (biến môi trường, có mặc định = node công cộng miễn phí):
  LAVALINK_URI       (mặc định https://lavalinkv4.serenetia.com:443)
  LAVALINK_PASSWORD  (mặc định https://dsc.gg/ajidevserver)

LỆNH:
  Slash: /join /leave /247 <on|off> [channel] /play <query> [channel]
         /randomsong [channel] /randomplaylist [count] [channel]
         /queue /volume <0-150> /loop <off|track|queue>
         /panel /skip /stop /nowplaying
  Prefix ('!'): !splay <query>   !srandomsong

GHI CHÚ /play:
  * Dán link YouTube/SoundCloud/Spotify -> Lavalink tự xử lý.
  * Ghim nền tảng bằng đuôi: 'tên bài -yt' / '-sc' / '-spotify'.
  * Chỉ ghi tên -> mặc định tìm trên YouTube.
"""

import os
import re
import random
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
import wavelink

log = logging.getLogger("bot.music")

# ============================================================
# ⚙️ CẤU HÌNH LAVALINK NODE
# ============================================================
LAVALINK_URI = os.getenv("LAVALINK_URI", "https://lavalinkv4.serenetia.com:443")
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "https://dsc.gg/ajidevserver")

# Thông báo dùng chung
NODE_ERR = (
    "❌ Chưa kết nối được máy chủ nhạc (Lavalink). Đợi vài giây rồi thử lại. "
    "Nếu mãi không được, node công cộng có thể đang sập — đổi node bằng biến "
    "môi trường LAVALINK_URI / LAVALINK_PASSWORD."
)
VOICE_ERR = "❌ Không vào được voice channel. Kiểm tra quyền của bot ở kênh đó."
NOTFOUND = "❌ Không tìm thấy bài nhạc nào."

# Seed cho nhạc ngẫu nhiên
RANDOM_SEEDS = [
    "lofi hip hop", "vietnamese chill", "edm 2024", "pop hits 2024",
    "rap viet hay nhat", "acoustic viet", "phonk", "vinahouse",
    "kpop hits", "us uk trending", "nhac tre remix", "deep house",
]

# Nhận diện nền tảng qua đuôi -yt / -sc / -spotify
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
    if low.startswith("http://") or low.startswith("https://"):
        return q, None, True
    m = _TAG_RE.search(q)
    if m:
        platform = PLATFORM_TAGS[m.group(1).lower()]
        clean = _TAG_RE.sub("", q).strip()
        return clean, platform, False
    return q, None, False


# ============================================================
# 🎛️ PANEL ĐIỀU KHIỂN NHẠC (view bền vững sau restart)
# ============================================================
class MusicControls(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    # ---- Hàng 1: điều khiển phát ----
    @discord.ui.button(emoji="⏯️", label="Phát/Dừng", style=discord.ButtonStyle.secondary, custom_id="music:pause", row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = interaction.guild.voice_client
        if player and (player.playing or player.paused):
            await player.pause(not player.paused)
            state = "⏸️ Tạm dừng." if player.paused else "▶️ Tiếp tục."
            await interaction.response.send_message(state, ephemeral=True)
        else:
            await interaction.response.send_message("❌ Không có nhạc đang phát.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="Skip", style=discord.ButtonStyle.primary, custom_id="music:skip", row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = interaction.guild.voice_client
        if player and player.playing:
            await player.skip(force=True)
            await interaction.response.send_message("⏭️ Đã skip.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Không có bài nào đang phát.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", label="Stop", style=discord.ButtonStyle.danger, custom_id="music:stop", row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = interaction.guild.voice_client
        if player:
            player.queue.clear()
            player.queue.mode = wavelink.QueueMode.normal
            player.autoplay = wavelink.AutoPlayMode.disabled
            if player.playing:
                await player.skip(force=True)
        await interaction.response.send_message("⏹️ Đã dừng & xóa hàng đợi.", ephemeral=True)

    @discord.ui.button(emoji="🔀", label="Random", style=discord.ButtonStyle.success, custom_id="music:random", row=0)
    async def random_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        target = await self.cog._target_channel(interaction.guild, interaction.user)
        if not target:
            return await interaction.response.send_message(
                "❌ Vào voice hoặc bật 24/7 trước.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        track, err = await self.cog._play_random(interaction.guild, target)
        if err:
            return await interaction.followup.send(self.cog._msg(err), ephemeral=True)
        await interaction.followup.send(f"🔀 Đã thêm ngẫu nhiên: **{track.title}**", ephemeral=True)

    @discord.ui.button(emoji="🔁", label="24/7", style=discord.ButtonStyle.secondary, custom_id="music:247", row=0)
    async def toggle_247(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        if await self.cog._is_247(gid):
            await self.cog.db.set_config(gid, "music_247", 0)
            await interaction.response.send_message("🔓 Đã TẮT 24/7.", ephemeral=True)
        else:
            target = await self.cog._target_channel(interaction.guild, interaction.user)
            if not target:
                return await interaction.response.send_message("❌ Vào voice trước.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            player = await self.cog._connect(interaction.guild, target)
            if not player:
                return await interaction.followup.send(self.cog._msg("voice"), ephemeral=True)
            await self.cog.db.set_config(gid, "music_247", 1)
            await self.cog.db.set_config(gid, "music_247_channel", target.id)
            await interaction.followup.send(f"🔒 Đã BẬT 24/7 tại **{target.name}**.", ephemeral=True)

    # ---- Hàng 2: âm lượng / loop / danh sách ----
    @discord.ui.button(emoji="🔉", label="Vol -", style=discord.ButtonStyle.secondary, custom_id="music:voldown", row=1)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("❌ Bot không ở trong voice.", ephemeral=True)
        new = max(0, player.volume - 10)
        await player.set_volume(new)
        await interaction.response.send_message(f"🔉 Âm lượng: **{new}%**", ephemeral=True)

    @discord.ui.button(emoji="🔊", label="Vol +", style=discord.ButtonStyle.secondary, custom_id="music:volup", row=1)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("❌ Bot không ở trong voice.", ephemeral=True)
        new = min(150, player.volume + 10)
        await player.set_volume(new)
        await interaction.response.send_message(f"🔊 Âm lượng: **{new}%**", ephemeral=True)

    @discord.ui.button(emoji="🔂", label="Loop", style=discord.ButtonStyle.secondary, custom_id="music:loop", row=1)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("❌ Bot không ở trong voice.", ephemeral=True)
        mode = player.queue.mode
        if mode == wavelink.QueueMode.normal:
            player.queue.mode = wavelink.QueueMode.loop
            txt = "🔂 Lặp lại 1 bài."
        elif mode == wavelink.QueueMode.loop:
            player.queue.mode = wavelink.QueueMode.loop_all
            txt = "🔁 Lặp cả hàng đợi."
        else:
            player.queue.mode = wavelink.QueueMode.normal
            txt = "➡️ Đã tắt lặp."
        await interaction.response.send_message(txt, ephemeral=True)

    @discord.ui.button(emoji="📜", label="Danh sách", style=discord.ButtonStyle.primary, custom_id="music:queue", row=1)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = interaction.guild.voice_client
        if not player or (not player.current and player.queue.is_empty):
            return await interaction.response.send_message("📭 Hàng đợi trống.", ephemeral=True)
        await interaction.response.send_message(embed=self.cog._queue_embed(player), ephemeral=True)


class Music(commands.Cog):
    """Cog nhạc qua Lavalink + treo voice 24/7 + panel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    # ---- Helpers ----
    def _node_ok(self) -> bool:
        try:
            return bool(wavelink.Pool.nodes)
        except Exception:
            return False

    def _msg(self, err: str) -> str:
        return {"node": NODE_ERR, "voice": VOICE_ERR, "notfound": NOTFOUND}.get(err, NOTFOUND)

    async def _is_247(self, guild_id: int) -> bool:
        try:
            return bool(await self.db.get_config(guild_id, "music_247"))
        except Exception:
            return False

    async def _target_channel(self, guild, user, explicit=None):
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

    async def _connect(self, guild, channel):
        """Kết nối/di chuyển Lavalink player tới voice channel. None nếu lỗi."""
        player: wavelink.Player = guild.voice_client
        try:
            if player is None:
                player = await channel.connect(cls=wavelink.Player)
            elif player.channel and player.channel.id != channel.id:
                await player.move_to(channel)
            player.autoplay = wavelink.AutoPlayMode.enabled
            return player
        except Exception:
            log.exception("Lỗi kết nối Lavalink player guild=%s", guild.id)
            return None

    async def _search(self, query: str):
        """Tìm nhạc qua Lavalink. Trả về list Playable / Playlist / None."""
        clean, platform, is_url = detect_platform(query)
        try:
            if is_url:
                return await wavelink.Playable.search(clean)
            if platform == "soundcloud":
                return await wavelink.Playable.search(clean, source="scsearch")
            if platform == "spotify":
                res = await wavelink.Playable.search(clean, source="spsearch")
                if res:
                    return res
                return await wavelink.Playable.search(clean, source="ytsearch")
            return await wavelink.Playable.search(clean, source="ytsearch")
        except Exception:
            log.exception("Lỗi search Lavalink")
            return None

    def _embed(self, track, title="🎵 Đã thêm vào hàng đợi"):
        desc = f"**{track.title}**"
        if getattr(track, "author", None):
            desc += f"\n`{track.author}`"
        embed = discord.Embed(title=title, description=desc, color=0x9b59b6)
        length = getattr(track, "length", 0) or 0
        if length:
            mins, secs = divmod(length // 1000, 60)
            embed.add_field(name="Thời lượng", value=f"{mins}:{secs:02d}")
        if getattr(track, "artwork", None):
            embed.set_thumbnail(url=track.artwork)
        if getattr(track, "uri", None):
            embed.add_field(name="Nguồn", value=f"[Mở link]({track.uri})", inline=False)
        return embed

    def _queue_embed(self, player):
        """Embed hiển thị hàng đợi hiện tại + trạng thái loop/âm lượng."""
        embed = discord.Embed(title="📜 Hàng đợi nhạc", color=0x9b59b6)
        cur = getattr(player, "current", None)
        if cur:
            embed.add_field(name="🎧 Đang phát", value=f"**{cur.title}**", inline=False)
        items = list(player.queue)
        if items:
            lines = [f"`{i}.` {t.title}" for i, t in enumerate(items[:10], 1)]
            more = len(items) - 10
            if more > 0:
                lines.append(f"... và {more} bài nữa")
            embed.add_field(name=f"⏭️ Tiếp theo ({len(items)} bài)",
                            value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="⏭️ Tiếp theo", value="_(trống)_", inline=False)
        mode_txt = {
            wavelink.QueueMode.normal: "Tắt",
            wavelink.QueueMode.loop: "🔂 Lặp 1 bài",
            wavelink.QueueMode.loop_all: "🔁 Lặp cả hàng đợi",
        }.get(player.queue.mode, "?")
        embed.add_field(name="Lặp", value=mode_txt)
        embed.add_field(name="Âm lượng", value=f"{player.volume}%")
        return embed

    async def _play_core(self, guild, channel, query):
        """Kết nối + tìm + phát/xếp hàng. Trả về (track|None, err|None)."""
        if not self._node_ok():
            return None, "node"
        player = await self._connect(guild, channel)
        if not player:
            return None, "voice"
        results = await self._search(query)
        if not results:
            return None, "notfound"
        if isinstance(results, wavelink.Playlist):
            await player.queue.put_wait(results)
            track = results.tracks[0] if results.tracks else None
        else:
            track = results[0]
            await player.queue.put_wait(track)
        if not player.playing:
            await player.play(player.queue.get(), volume=40)
        return track, None

    async def _play_random(self, guild, channel):
        """Phát 1 bài ngẫu nhiên. Trả về (track|None, err|None)."""
        if not self._node_ok():
            return None, "node"
        player = await self._connect(guild, channel)
        if not player:
            return None, "voice"
        seed = random.choice(RANDOM_SEEDS)
        results = await self._search(seed)
        if not results:
            return None, "notfound"
        pool = list(results.tracks) if isinstance(results, wavelink.Playlist) else list(results)
        pool = pool[:15]
        if not pool:
            return None, "notfound"
        track = random.choice(pool)
        await player.queue.put_wait(track)
        if not player.playing:
            await player.play(player.queue.get(), volume=40)
        return track, None

    async def _play_random_many(self, guild, channel, count=10):
        """Thêm nhiều bài ngẫu nhiên (playlist ngẫu nhiên). Trả về (added, first|None, err|None)."""
        if not self._node_ok():
            return 0, None, "node"
        player = await self._connect(guild, channel)
        if not player:
            return 0, None, "voice"
        collected = []
        seeds = random.sample(RANDOM_SEEDS, min(len(RANDOM_SEEDS), 4))
        for seed in seeds:
            res = await self._search(seed)
            if not res:
                continue
            pool = list(res.tracks) if isinstance(res, wavelink.Playlist) else list(res)
            collected.extend(pool)
            if len(collected) >= count:
                break
        if not collected:
            return 0, None, "notfound"
        random.shuffle(collected)
        chosen = collected[:count]
        added = await player.queue.put_wait(chosen)
        first = chosen[0]
        if not player.playing:
            await player.play(player.queue.get(), volume=40)
        return added, first, None

    # ========================================================
    # 📡 SỰ KIỆN WAVELINK
    # ========================================================
    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        log.info("✅ Lavalink Node đã kết nối: %s (resumed=%s)", payload.node.uri, payload.resumed)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        track = payload.track
        if track:
            log.info("Đang phát: %s", track.title)

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
        if not self._node_ok():
            return await interaction.followup.send(NODE_ERR, ephemeral=True)
        player = await self._connect(interaction.guild, target)
        if not player:
            return await interaction.followup.send(VOICE_ERR, ephemeral=True)
        await interaction.followup.send(f"✅ Đã vào **{target.name}**.", ephemeral=True)

    @app_commands.command(name="leave", description="Bot rời voice channel")
    async def leave(self, interaction: discord.Interaction):
        player: wavelink.Player = interaction.guild.voice_client
        if player:
            await self.db.set_config(interaction.guild.id, "music_247", 0)
            await player.disconnect()
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
            if not self._node_ok():
                return await interaction.followup.send(NODE_ERR, ephemeral=True)
            player = await self._connect(interaction.guild, target)
            if not player:
                return await interaction.followup.send(VOICE_ERR, ephemeral=True)
            await self.db.set_config(interaction.guild.id, "music_247", 1)
            await self.db.set_config(interaction.guild.id, "music_247_channel", target.id)
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
                    player = await self._connect(guild, channel)
                    if player:
                        log.info("🔁 Tự vào lại voice 24/7: %s", guild.name)
                    else:
                        log.warning("Không vào lại được voice 24/7: %s", guild.name)

    # ========================================================
    # 🎵 PHÁT NHẠC (slash)
    # ========================================================
    @app_commands.command(
        name="play",
        description="Phát nhạc (YouTube/SoundCloud/Spotify — tự tìm nếu chỉ ghi tên)")
    @app_commands.describe(
        query="Tên bài hoặc link. Ghim nền tảng: 'tên bài -spotify' / '-yt' / '-sc'",
        channel="Voice channel muốn phát (tùy chọn, không cần bạn vào voice)")
    async def play(self, interaction: discord.Interaction, query: str,
                   channel: discord.VoiceChannel = None):
        target = await self._target_channel(interaction.guild, interaction.user, channel)
        if not target:
            return await interaction.response.send_message(
                "❌ Chọn 1 voice channel hoặc vào voice trước.", ephemeral=True)
        await interaction.response.defer()
        track, err = await self._play_core(interaction.guild, target, query)
        if err:
            return await interaction.followup.send(self._msg(err))
        await interaction.followup.send(embed=self._embed(track), view=MusicControls(self))

    @app_commands.command(name="randomsong", description="Phát 1 bài nhạc ngẫu nhiên")
    @app_commands.describe(channel="Voice channel muốn phát (tùy chọn)")
    async def randomsong(self, interaction: discord.Interaction,
                         channel: discord.VoiceChannel = None):
        target = await self._target_channel(interaction.guild, interaction.user, channel)
        if not target:
            return await interaction.response.send_message(
                "❌ Chọn 1 voice channel hoặc vào voice trước.", ephemeral=True)
        await interaction.response.defer()
        track, err = await self._play_random(interaction.guild, target)
        if err:
            return await interaction.followup.send(self._msg(err))
        await interaction.followup.send(
            embed=self._embed(track, "🔀 Ngẫu nhiên"), view=MusicControls(self))

    @app_commands.command(name="randomplaylist",
                          description="Thêm nhiều bài ngẫu nhiên vào hàng đợi (playlist ngẫu nhiên)")
    @app_commands.describe(count="Số bài (mặc định 10, tối đa 25)",
                           channel="Voice channel muốn phát (tùy chọn)")
    async def randomplaylist(self, interaction: discord.Interaction,
                             count: app_commands.Range[int, 1, 25] = 10,
                             channel: discord.VoiceChannel = None):
        target = await self._target_channel(interaction.guild, interaction.user, channel)
        if not target:
            return await interaction.response.send_message(
                "❌ Chọn 1 voice channel hoặc vào voice trước.", ephemeral=True)
        await interaction.response.defer()
        added, first, err = await self._play_random_many(interaction.guild, target, count)
        if err:
            return await interaction.followup.send(self._msg(err))
        embed = discord.Embed(
            title="🎲 Playlist ngẫu nhiên",
            description=f"Đã thêm **{added}** bài vào hàng đợi.\nBắt đầu: **{first.title}**",
            color=0x9b59b6)
        await interaction.followup.send(embed=embed, view=MusicControls(self))

    @app_commands.command(name="queue", description="Xem hàng đợi nhạc hiện tại")
    async def queue_cmd(self, interaction: discord.Interaction):
        player: wavelink.Player = interaction.guild.voice_client
        if not player or (not player.current and player.queue.is_empty):
            return await interaction.response.send_message("📭 Hàng đợi trống.", ephemeral=True)
        await interaction.response.send_message(embed=self._queue_embed(player), view=MusicControls(self))

    @app_commands.command(name="volume", description="Chỉnh âm lượng nhạc (0-150)")
    @app_commands.describe(value="Mức âm lượng từ 0 đến 150 (%)")
    async def volume(self, interaction: discord.Interaction,
                     value: app_commands.Range[int, 0, 150]):
        player: wavelink.Player = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("❌ Bot không ở trong voice.", ephemeral=True)
        await player.set_volume(value)
        await interaction.response.send_message(f"🔊 Đã chỉnh âm lượng: **{value}%**", ephemeral=True)

    @app_commands.command(name="loop", description="Chế độ lặp nhạc")
    @app_commands.describe(mode="off = tắt, track = lặp 1 bài, queue = lặp cả hàng đợi")
    @app_commands.choices(mode=[
        app_commands.Choice(name="off", value="off"),
        app_commands.Choice(name="track", value="track"),
        app_commands.Choice(name="queue", value="queue"),
    ])
    async def loop_cmd(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        player: wavelink.Player = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("❌ Bot không ở trong voice.", ephemeral=True)
        mapping = {
            "off": wavelink.QueueMode.normal,
            "track": wavelink.QueueMode.loop,
            "queue": wavelink.QueueMode.loop_all,
        }
        player.queue.mode = mapping[mode.value]
        txt = {
            "off": "➡️ Đã tắt lặp.",
            "track": "🔂 Lặp lại 1 bài.",
            "queue": "🔁 Lặp cả hàng đợi.",
        }[mode.value]
        await interaction.response.send_message(txt, ephemeral=True)

    @app_commands.command(name="panel", description="Hiện bảng điều khiển nhạc")
    async def panel(self, interaction: discord.Interaction):
        player: wavelink.Player = interaction.guild.voice_client
        desc = "Không có bài nào đang phát."
        if player and player.current:
            desc = f"🎧 Đang phát: **{player.current.title}**"
        embed = discord.Embed(title="🎛️ Bảng điều khiển nhạc", description=desc, color=0x9b59b6)
        if player:
            mode_txt = {
                wavelink.QueueMode.normal: "Tắt",
                wavelink.QueueMode.loop: "🔂 Lặp 1 bài",
                wavelink.QueueMode.loop_all: "🔁 Lặp cả hàng đợi",
            }.get(player.queue.mode, "?")
            embed.add_field(name="Lặp", value=mode_txt)
            embed.add_field(name="Âm lượng", value=f"{player.volume}%")
        await interaction.response.send_message(embed=embed, view=MusicControls(self))

    @app_commands.command(name="skip", description="Bỏ qua bài đang phát")
    async def skip(self, interaction: discord.Interaction):
        player: wavelink.Player = interaction.guild.voice_client
        if player and player.playing:
            await player.skip(force=True)
            await interaction.response.send_message("⏭️ Đã skip.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Không có bài nào đang phát.", ephemeral=True)

    @app_commands.command(name="stop", description="Dừng nhạc & xóa hàng đợi")
    async def stop(self, interaction: discord.Interaction):
        player: wavelink.Player = interaction.guild.voice_client
        if player:
            player.queue.clear()
            player.queue.mode = wavelink.QueueMode.normal
            player.autoplay = wavelink.AutoPlayMode.disabled
            if player.playing:
                await player.skip(force=True)
        await interaction.response.send_message("⏹️ Đã dừng & xóa hàng đợi.", ephemeral=True)

    @app_commands.command(name="nowplaying", description="Xem bài đang phát")
    async def nowplaying(self, interaction: discord.Interaction):
        player: wavelink.Player = interaction.guild.voice_client
        if player and player.current:
            await interaction.response.send_message(
                embed=self._embed(player.current, "🎧 Đang phát"),
                view=MusicControls(self))
        else:
            await interaction.response.send_message("📭 Không có bài nào đang phát.", ephemeral=True)

    # ========================================================
    # 📝 PHÁT NHẠC (prefix '!')
    # ========================================================
    @commands.command(name="splay")
    async def splay(self, ctx: commands.Context, *, query: str = None):
        """!splay <tên bài / link> — phát nhạc ngay."""
        if not query:
            return await ctx.send("ℹ️ Dùng: `!splay <tên bài hoặc link>`")
        target = await self._target_channel(ctx.guild, ctx.author)
        if not target:
            return await ctx.send("❌ Vào voice hoặc bật 24/7 trước đã.")
        msg = await ctx.send(f"🔎 Đang tìm: **{query}**...")
        track, err = await self._play_core(ctx.guild, target, query)
        if err:
            return await msg.edit(content=self._msg(err))
        await msg.edit(content=None, embed=self._embed(track), view=MusicControls(self))

    @commands.command(name="srandomsong")
    async def srandomsong(self, ctx: commands.Context):
        """!srandomsong — phát 1 bài ngẫu nhiên."""
        target = await self._target_channel(ctx.guild, ctx.author)
        if not target:
            return await ctx.send("❌ Vào voice hoặc bật 24/7 trước đã.")
        msg = await ctx.send("🎲 Đang chọn bài ngẫu nhiên...")
        track, err = await self._play_random(ctx.guild, target)
        if err:
            return await msg.edit(content=self._msg(err))
        await msg.edit(content=None,
                       embed=self._embed(track, "🔀 Ngẫu nhiên"),
                       view=MusicControls(self))


async def setup(bot: commands.Bot):
    cog = Music(bot)
    await bot.add_cog(cog)
    # Đăng ký view panel bền vững (nút bấm vẫn chạy sau restart)
    try:
        bot.add_view(MusicControls(cog))
    except Exception:
        log.exception("Không đăng ký được MusicControls view")
    # Kết nối tới Lavalink node
    try:
        node = wavelink.Node(uri=LAVALINK_URI, password=LAVALINK_PASSWORD)
        await wavelink.Pool.connect(nodes=[node], client=bot, cache_capacity=100)
        log.info("🎶 Đang kết nối Lavalink: %s", LAVALINK_URI)
    except Exception:
        log.exception("Không kết nối được Lavalink pool")
