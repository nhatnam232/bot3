"""
utils/audit_cache.py — Cache audit log dùng chung cho mọi cog.

Vấn đề: anti_nuke + logger đều gọi guild.audit_logs() độc lập khi cùng
một sự kiện xảy ra → Discord trả 429 rate limit ngay lập tức.

Giải pháp: Mỗi (guild, action) chỉ được gọi API 1 lần trong TTL giây.
Các lần gọi tiếp theo trong cùng TTL đọc từ cache local.

Cách dùng:
    from utils.audit_cache import audit_cache

    entries = await audit_cache.get_entries(guild, discord.AuditLogAction.channel_delete)
    for entry in entries:
        ...
"""

import asyncio
import logging
import time
from typing import Optional

import discord

log = logging.getLogger("bot.audit_cache")

# TTL mặc định (giây) — audit log Discord delay ~1-2s nên 3s là đủ
_DEFAULT_TTL = 3.0

# Giới hạn entries fetch mỗi lần (tăng lên 10 để cả 2 cog dùng chung vẫn đủ)
_DEFAULT_LIMIT = 10


class AuditLogCache:
    """
    Cache audit log theo (guild_id, action).

    Thread-safe: dùng asyncio.Lock per key, tránh thundering herd
    (nhiều coroutine cùng miss cache rồi đồng loạt gọi API).
    """

    def __init__(self, ttl: float = _DEFAULT_TTL):
        self.ttl = ttl
        # {(guild_id, action): (fetch_time, [AuditLogEntry])}
        self._cache: dict[tuple, tuple[float, list]] = {}
        # Lock per key — tránh nhiều coroutine cùng fetch khi cache miss
        self._locks: dict[tuple, asyncio.Lock] = {}

    def _get_lock(self, key: tuple) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def get_entries(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        limit: int = _DEFAULT_LIMIT,
    ) -> list:
        """
        Trả danh sách AuditLogEntry. Gọi API tối đa 1 lần trong TTL giây.

        Trả [] nếu:
        - Bot không có quyền View Audit Log
        - Bị rate limit VÀ không có cache cũ nào để fallback
        """
        key = (guild.id, action)
        now = time.monotonic()

        async with self._get_lock(key):
            cached = self._cache.get(key)

            # Cache còn hạn → trả ngay, không gọi API
            if cached and (now - cached[0]) < self.ttl:
                return cached[1]

            # Cache hết hạn hoặc chưa có → gọi API
            try:
                entries = [
                    entry async for entry in guild.audit_logs(
                        limit=limit, action=action
                    )
                ]
                self._cache[key] = (now, entries)
                return entries

            except discord.Forbidden:
                log.warning(
                    "Missing View Audit Log permission in guild %s", guild.id
                )
                return []

            except discord.HTTPException as e:
                if e.status == 429:
                    # Rate limited → trả cache cũ nếu còn (dù expired)
                    if cached:
                        log.debug(
                            "Rate limited fetching audit log for guild %s action %s"
                            " — serving stale cache",
                            guild.id, action,
                        )
                        return cached[1]
                    log.warning(
                        "Rate limited fetching audit log for guild %s action %s"
                        " — no cache available",
                        guild.id, action,
                    )
                    return []
                log.exception(
                    "HTTP error fetching audit log for guild %s", guild.id
                )
                return []

            except Exception:
                log.exception(
                    "Unexpected error fetching audit log for guild %s", guild.id
                )
                if cached:
                    return cached[1]
                return []

    def invalidate(self, guild_id: int, action: discord.AuditLogAction | None = None):
        """
        Xóa cache thủ công (dùng sau khi bot tự thực hiện hành động).
        action=None → xóa toàn bộ cache của guild.
        """
        if action is None:
            keys_to_del = [k for k in self._cache if k[0] == guild_id]
        else:
            keys_to_del = [(guild_id, action)]
        for k in keys_to_del:
            self._cache.pop(k, None)


# Singleton — import và dùng trực tiếp trong mọi cog
audit_cache = AuditLogCache()
