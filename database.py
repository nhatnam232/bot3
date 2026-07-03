"""
database.py — Lớp truy cập SQLite (aiosqlite).
- Lưu config per-guild (mọi threshold đều chỉnh được qua slash command)
- Lưu warn, whitelist (user + bot + invite), backup metadata
- Có cache config trong RAM để event handler không phải query liên tục
"""

import time
import logging
from typing import Optional

import aiosqlite

from config import DB_PATH, DEFAULT_CONFIG

log = logging.getLogger("bot.database")

# ============================================================
# SQL tạo bảng
# ============================================================
SCHEMA = """
-- Config per guild: key/value, value lưu TEXT để linh hoạt (int/str)
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    value    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, key)
);

-- Warn tích lũy theo user + guild
CREATE TABLE IF NOT EXISTS warns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    moderator  INTEGER NOT NULL,
    reason     TEXT,
    created_at INTEGER NOT NULL,          -- unix timestamp
    expired    INTEGER NOT NULL DEFAULT 0 -- 1 = đã hết hạn
);
CREATE INDEX IF NOT EXISTS idx_warns_guild_user ON warns (guild_id, user_id);

-- Whitelist user: bypass toàn bộ module (chỉ Owner/Co-owner thêm được)
CREATE TABLE IF NOT EXISTS whitelist_users (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    added_by INTEGER NOT NULL,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Whitelist bot: bot được phép add vào server (anti-nuke không kick)
CREATE TABLE IF NOT EXISTS whitelist_bots (
    guild_id INTEGER NOT NULL,
    bot_id   INTEGER NOT NULL,
    added_by INTEGER NOT NULL,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, bot_id)
);

-- Whitelist invite: server được phép gửi link discord.gg
CREATE TABLE IF NOT EXISTS whitelist_invites (
    guild_id    INTEGER NOT NULL,
    invite_code TEXT    NOT NULL,
    added_by    INTEGER NOT NULL,
    added_at    INTEGER NOT NULL,
    PRIMARY KEY (guild_id, invite_code)
);

-- Từ ngữ blacklist per guild (ngoài file blacklist.txt global)
CREATE TABLE IF NOT EXISTS blacklist_words (
    guild_id INTEGER NOT NULL,
    word     TEXT    NOT NULL,
    PRIMARY KEY (guild_id, word)
);

-- Metadata backup (file JSON lưu trong data/backups/)
CREATE TABLE IF NOT EXISTS backups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    file_path  TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);

-- Lịch sử mute đang hiệu lực (để unmute đúng giờ kể cả khi bot restart)
CREATE TABLE IF NOT EXISTS active_mutes (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    unmute_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Auto respond per guild: trigger → reply (giống Mimu)
CREATE TABLE IF NOT EXISTS auto_responses (
    guild_id   INTEGER NOT NULL,
    trigger    TEXT    NOT NULL,            -- từ khóa (lowercase)
    reply      TEXT    NOT NULL,            -- nội dung trả lời
    exact      INTEGER NOT NULL DEFAULT 0,  -- 1 = khớp cả câu, 0 = chứa từ khóa
    created_by INTEGER NOT NULL,
    PRIMARY KEY (guild_id, trigger)
);

-- Panel pick role: lưu để re-attach view sau khi bot restart
CREATE TABLE IF NOT EXISTS role_panels (
    message_id INTEGER PRIMARY KEY,         -- tin nhắn chứa panel
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    role_ids   TEXT    NOT NULL             -- danh sách role id, cách nhau dấu phẩy
);
"""


class Database:
    """Wrapper aiosqlite — 1 connection dùng chung, có cache config."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None
        # Cache: {guild_id: {key: value}} — tránh query SQLite mỗi event
        self._config_cache: dict[int, dict[str, str]] = {}
        # Cache whitelist: {guild_id: set(user_id)}
        self._wl_user_cache: dict[int, set[int]] = {}
        self._wl_bot_cache: dict[int, set[int]] = {}

    # ========================================================
    # KHỞI TẠO / ĐÓNG
    # ========================================================
    async def init(self):
        """Mở connection + tạo bảng nếu chưa có."""
        try:
            self.conn = await aiosqlite.connect(self.db_path)
            await self.conn.executescript(SCHEMA)
            await self.conn.commit()
            log.info("Database initialized: %s", self.db_path)
        except Exception:
            log.exception("Failed to initialize database")
            raise

    async def close(self):
        """Đóng connection an toàn."""
        try:
            if self.conn:
                await self.conn.close()
        except Exception:
            log.exception("Error closing database")

    # ========================================================
    # ⚙️ CONFIG per guild
    # ========================================================
    async def _load_guild_config(self, guild_id: int) -> dict[str, str]:
        """Load toàn bộ config của guild vào cache."""
        cfg: dict[str, str] = {}
        try:
            async with self.conn.execute(
                "SELECT key, value FROM guild_config WHERE guild_id = ?", (guild_id,)
            ) as cur:
                async for key, value in cur:
                    cfg[key] = value
        except Exception:
            log.exception("Failed to load config for guild %s", guild_id)
        self._config_cache[guild_id] = cfg
        return cfg

    async def get_config(self, guild_id: int, key: str) -> int:
        """
        Lấy config dạng int (đa số threshold là số).
        Fallback về DEFAULT_CONFIG nếu guild chưa set.
        """
        cfg = self._config_cache.get(guild_id)
        if cfg is None:
            cfg = await self._load_guild_config(guild_id)
        raw = cfg.get(key)
        if raw is None:
            return int(DEFAULT_CONFIG.get(key, 0))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return int(DEFAULT_CONFIG.get(key, 0))

    async def set_config(self, guild_id: int, key: str, value) -> None:
        """Set config + update cache."""
        try:
            await self.conn.execute(
                "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                (guild_id, key, str(value)),
            )
            await self.conn.commit()
            self._config_cache.setdefault(guild_id, {})[key] = str(value)
        except Exception:
            log.exception("Failed to set config %s=%s for guild %s", key, value, guild_id)

    async def is_module_enabled(self, guild_id: int, module: str) -> bool:
        """Check toggle module (vd: 'antispam') có bật không."""
        return await self.get_config(guild_id, f"{module}_enabled") == 1

    # ========================================================
    # ⚠️ WARN SYSTEM
    # ========================================================
    async def add_warn(self, guild_id: int, user_id: int, moderator: int, reason: str) -> int:
        """Thêm warn, trả về số warn đang hiệu lực sau khi thêm."""
        now = int(time.time())
        try:
            await self.conn.execute(
                "INSERT INTO warns (guild_id, user_id, moderator, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, moderator, reason, now),
            )
            await self.conn.commit()
        except Exception:
            log.exception("Failed to add warn")
        return await self.count_warns(guild_id, user_id)

    async def count_warns(self, guild_id: int, user_id: int) -> int:
        """Đếm warn còn hiệu lực (chưa expire)."""
        expire_days = await self.get_config(guild_id, "warn_expire_days")
        cutoff = int(time.time()) - expire_days * 86400
        try:
            async with self.conn.execute(
                "SELECT COUNT(*) FROM warns "
                "WHERE guild_id = ? AND user_id = ? AND expired = 0 AND created_at > ?",
                (guild_id, user_id, cutoff),
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0
        except Exception:
            log.exception("Failed to count warns")
            return 0

    async def get_warns(self, guild_id: int, user_id: int) -> list:
        """Danh sách warn còn hiệu lực của user."""
        expire_days = await self.get_config(guild_id, "warn_expire_days")
        cutoff = int(time.time()) - expire_days * 86400
        try:
            async with self.conn.execute(
                "SELECT id, moderator, reason, created_at FROM warns "
                "WHERE guild_id = ? AND user_id = ? AND expired = 0 AND created_at > ? "
                "ORDER BY created_at DESC",
                (guild_id, user_id, cutoff),
            ) as cur:
                return await cur.fetchall()
        except Exception:
            log.exception("Failed to get warns")
            return []

    async def clear_warns(self, guild_id: int, user_id: int) -> None:
        """Xóa (đánh dấu expire) toàn bộ warn của user."""
        try:
            await self.conn.execute(
                "UPDATE warns SET expired = 1 WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await self.conn.commit()
        except Exception:
            log.exception("Failed to clear warns")

    async def expire_old_warns(self) -> None:
        """Task định kỳ: đánh dấu expire các warn quá hạn (mọi guild)."""
        try:
            # Mỗi guild có expire_days riêng → xử lý theo guild có warn
            async with self.conn.execute(
                "SELECT DISTINCT guild_id FROM warns WHERE expired = 0"
            ) as cur:
                guild_ids = [row[0] async for row in cur]
            for gid in guild_ids:
                expire_days = await self.get_config(gid, "warn_expire_days")
                cutoff = int(time.time()) - expire_days * 86400
                await self.conn.execute(
                    "UPDATE warns SET expired = 1 "
                    "WHERE guild_id = ? AND expired = 0 AND created_at <= ?",
                    (gid, cutoff),
                )
            await self.conn.commit()
        except Exception:
            log.exception("Failed to expire old warns")

    # ========================================================
    # 🔐 WHITELIST USERS
    # ========================================================
    async def _load_wl_users(self, guild_id: int) -> set[int]:
        wl: set[int] = set()
        try:
            async with self.conn.execute(
                "SELECT user_id FROM whitelist_users WHERE guild_id = ?", (guild_id,)
            ) as cur:
                async for (uid,) in cur:
                    wl.add(uid)
        except Exception:
            log.exception("Failed to load user whitelist")
        self._wl_user_cache[guild_id] = wl
        return wl

    async def is_whitelisted(self, guild_id: int, user_id: int) -> bool:
        """Check user có trong whitelist không (dùng cache)."""
        wl = self._wl_user_cache.get(guild_id)
        if wl is None:
            wl = await self._load_wl_users(guild_id)
        return user_id in wl

    async def add_whitelist_user(self, guild_id: int, user_id: int, added_by: int) -> None:
        try:
            await self.conn.execute(
                "INSERT OR IGNORE INTO whitelist_users (guild_id, user_id, added_by, added_at) "
                "VALUES (?, ?, ?, ?)",
                (guild_id, user_id, added_by, int(time.time())),
            )
            await self.conn.commit()
            self._wl_user_cache.setdefault(guild_id, set()).add(user_id)
        except Exception:
            log.exception("Failed to add whitelist user")

    async def remove_whitelist_user(self, guild_id: int, user_id: int) -> None:
        try:
            await self.conn.execute(
                "DELETE FROM whitelist_users WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await self.conn.commit()
            self._wl_user_cache.get(guild_id, set()).discard(user_id)
        except Exception:
            log.exception("Failed to remove whitelist user")

    async def list_whitelist_users(self, guild_id: int) -> list:
        try:
            async with self.conn.execute(
                "SELECT user_id, added_by, added_at FROM whitelist_users WHERE guild_id = ?",
                (guild_id,),
            ) as cur:
                return await cur.fetchall()
        except Exception:
            log.exception("Failed to list whitelist users")
            return []

    # ========================================================
    # 🤖 WHITELIST BOTS
    # ========================================================
    async def is_bot_whitelisted(self, guild_id: int, bot_id: int) -> bool:
        wl = self._wl_bot_cache.get(guild_id)
        if wl is None:
            wl = set()
            try:
                async with self.conn.execute(
                    "SELECT bot_id FROM whitelist_bots WHERE guild_id = ?", (guild_id,)
                ) as cur:
                    async for (bid,) in cur:
                        wl.add(bid)
            except Exception:
                log.exception("Failed to load bot whitelist")
            self._wl_bot_cache[guild_id] = wl
        return bot_id in wl

    async def add_whitelist_bot(self, guild_id: int, bot_id: int, added_by: int) -> None:
        try:
            await self.conn.execute(
                "INSERT OR IGNORE INTO whitelist_bots (guild_id, bot_id, added_by, added_at) "
                "VALUES (?, ?, ?, ?)",
                (guild_id, bot_id, added_by, int(time.time())),
            )
            await self.conn.commit()
            self._wl_bot_cache.setdefault(guild_id, set()).add(bot_id)
        except Exception:
            log.exception("Failed to add whitelist bot")

    async def remove_whitelist_bot(self, guild_id: int, bot_id: int) -> None:
        try:
            await self.conn.execute(
                "DELETE FROM whitelist_bots WHERE guild_id = ? AND bot_id = ?",
                (guild_id, bot_id),
            )
            await self.conn.commit()
            self._wl_bot_cache.get(guild_id, set()).discard(bot_id)
        except Exception:
            log.exception("Failed to remove whitelist bot")

    # ========================================================
    # 🔗 WHITELIST INVITES
    # ========================================================
    async def is_invite_whitelisted(self, guild_id: int, invite_code: str) -> bool:
        try:
            async with self.conn.execute(
                "SELECT 1 FROM whitelist_invites WHERE guild_id = ? AND invite_code = ?",
                (guild_id, invite_code),
            ) as cur:
                return await cur.fetchone() is not None
        except Exception:
            log.exception("Failed to check invite whitelist")
            return False

    async def add_whitelist_invite(self, guild_id: int, code: str, added_by: int) -> None:
        try:
            await self.conn.execute(
                "INSERT OR IGNORE INTO whitelist_invites (guild_id, invite_code, added_by, added_at) "
                "VALUES (?, ?, ?, ?)",
                (guild_id, code, added_by, int(time.time())),
            )
            await self.conn.commit()
        except Exception:
            log.exception("Failed to add whitelist invite")

    async def remove_whitelist_invite(self, guild_id: int, code: str) -> None:
        try:
            await self.conn.execute(
                "DELETE FROM whitelist_invites WHERE guild_id = ? AND invite_code = ?",
                (guild_id, code),
            )
            await self.conn.commit()
        except Exception:
            log.exception("Failed to remove whitelist invite")

    # ========================================================
    # 🈲 BLACKLIST WORDS (per guild)
    # ========================================================
    async def get_blacklist_words(self, guild_id: int) -> set[str]:
        words: set[str] = set()
        try:
            async with self.conn.execute(
                "SELECT word FROM blacklist_words WHERE guild_id = ?", (guild_id,)
            ) as cur:
                async for (w,) in cur:
                    words.add(w)
        except Exception:
            log.exception("Failed to get blacklist words")
        return words

    async def add_blacklist_word(self, guild_id: int, word: str) -> None:
        try:
            await self.conn.execute(
                "INSERT OR IGNORE INTO blacklist_words (guild_id, word) VALUES (?, ?)",
                (guild_id, word.lower().strip()),
            )
            await self.conn.commit()
        except Exception:
            log.exception("Failed to add blacklist word")

    async def remove_blacklist_word(self, guild_id: int, word: str) -> None:
        try:
            await self.conn.execute(
                "DELETE FROM blacklist_words WHERE guild_id = ? AND word = ?",
                (guild_id, word.lower().strip()),
            )
            await self.conn.commit()
        except Exception:
            log.exception("Failed to remove blacklist word")

    # ========================================================
    # 💾 BACKUP METADATA
    # ========================================================
    async def add_backup(self, guild_id: int, file_path: str) -> None:
        try:
            await self.conn.execute(
                "INSERT INTO backups (guild_id, file_path, created_at) VALUES (?, ?, ?)",
                (guild_id, file_path, int(time.time())),
            )
            await self.conn.commit()
        except Exception:
            log.exception("Failed to add backup record")

    async def get_latest_backup(self, guild_id: int) -> Optional[str]:
        """Trả về đường dẫn file backup mới nhất của guild."""
        try:
            async with self.conn.execute(
                "SELECT file_path FROM backups WHERE guild_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (guild_id,),
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else None
        except Exception:
            log.exception("Failed to get latest backup")
            return None

    # ========================================================
    # 🔇 ACTIVE MUTES (persist qua restart)
    # ========================================================
    async def add_mute(self, guild_id: int, user_id: int, unmute_at: int) -> None:
        try:
            await self.conn.execute(
                "INSERT INTO active_mutes (guild_id, user_id, unmute_at) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET unmute_at = excluded.unmute_at",
                (guild_id, user_id, unmute_at),
            )
            await self.conn.commit()
        except Exception:
            log.exception("Failed to add mute")

    async def remove_mute(self, guild_id: int, user_id: int) -> None:
        try:
            await self.conn.execute(
                "DELETE FROM active_mutes WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await self.conn.commit()
        except Exception:
            log.exception("Failed to remove mute")

    async def get_expired_mutes(self) -> list:
        """Danh sách (guild_id, user_id) đã đến giờ unmute."""
        try:
            async with self.conn.execute(
                "SELECT guild_id, user_id FROM active_mutes WHERE unmute_at <= ?",
                (int(time.time()),),
            ) as cur:
                return await cur.fetchall()
        except Exception:
            log.exception("Failed to get expired mutes")
            return []

    # ========================================================
    # ⚙️ CONFIG RESET — xóa config đã chỉnh, quay về mặc định
    # ========================================================
    async def delete_config(self, guild_id: int, key: str = None) -> None:
        """Xóa 1 key (hoặc TOÀN BỘ nếu key=None) của guild → quay về DEFAULT_CONFIG."""
        try:
            if key:
                await self.conn.execute(
                    "DELETE FROM guild_config WHERE guild_id = ? AND key = ?",
                    (guild_id, key))
                self._config_cache.get(guild_id, {}).pop(key, None)
            else:
                await self.conn.execute(
                    "DELETE FROM guild_config WHERE guild_id = ?", (guild_id,))
                self._config_cache.pop(guild_id, None)
            await self.conn.commit()
        except Exception:
            log.exception("Failed to delete config")

    # ========================================================
    # 💬 AUTO RESPONSES (per guild, giống Mimu)
    # ========================================================
    async def get_auto_responses(self, guild_id: int) -> list:
        """List (trigger, reply, exact) của guild."""
        try:
            async with self.conn.execute(
                "SELECT trigger, reply, exact FROM auto_responses WHERE guild_id = ?",
                (guild_id,),
            ) as cur:
                return await cur.fetchall()
        except Exception:
            log.exception("Failed to get auto responses")
            return []

    async def add_auto_response(
        self, guild_id: int, trigger: str, reply: str, exact: int, created_by: int,
    ) -> None:
        try:
            await self.conn.execute(
                "INSERT INTO auto_responses (guild_id, trigger, reply, exact, created_by) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, trigger) DO UPDATE SET "
                "reply = excluded.reply, exact = excluded.exact",
                (guild_id, trigger.lower().strip(), reply, exact, created_by))
            await self.conn.commit()
        except Exception:
            log.exception("Failed to add auto response")

    async def remove_auto_response(self, guild_id: int, trigger: str) -> bool:
        try:
            cur = await self.conn.execute(
                "DELETE FROM auto_responses WHERE guild_id = ? AND trigger = ?",
                (guild_id, trigger.lower().strip()))
            await self.conn.commit()
            return cur.rowcount > 0
        except Exception:
            log.exception("Failed to remove auto response")
            return False

    # ========================================================
    # 🎭 ROLE PANELS (pick role — persist qua restart)
    # ========================================================
    async def add_role_panel(
        self, message_id: int, guild_id: int, channel_id: int, role_ids: list,
    ) -> None:
        try:
            await self.conn.execute(
                "INSERT OR REPLACE INTO role_panels "
                "(message_id, guild_id, channel_id, role_ids) VALUES (?, ?, ?, ?)",
                (message_id, guild_id, channel_id,
                 ",".join(str(r) for r in role_ids)))
            await self.conn.commit()
        except Exception:
            log.exception("Failed to add role panel")

    async def get_all_role_panels(self) -> list:
        """List (message_id, guild_id, channel_id, role_ids_str) — re-attach view khi khởi động."""
        try:
            async with self.conn.execute(
                "SELECT message_id, guild_id, channel_id, role_ids FROM role_panels"
            ) as cur:
                return await cur.fetchall()
        except Exception:
            log.exception("Failed to get role panels")
            return []

    async def remove_role_panel(self, message_id: int) -> None:
        try:
            await self.conn.execute(
                "DELETE FROM role_panels WHERE message_id = ?", (message_id,))
            await self.conn.commit()
        except Exception:
            log.exception("Failed to remove role panel")


# Instance dùng chung toàn bot (import từ các cog)
db = Database()

# ✅ Done: database.py — Tiếp theo: main.py
