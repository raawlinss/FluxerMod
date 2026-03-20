"""
Veritabanı katmanı — SQLite ile tüm ayarlar kalıcı olarak saklanır.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger("Database")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id        TEXT PRIMARY KEY,
    prefix          TEXT DEFAULT '!',
    log_channel_id  TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS link_filter (
    guild_id            TEXT PRIMARY KEY,
    enabled             INTEGER DEFAULT 0,
    whitelist_roles     TEXT DEFAULT '[]',
    whitelist_channels  TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS regex_filters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    pattern      TEXT NOT NULL,
    action       TEXT DEFAULT 'delete',
    warn_message TEXT DEFAULT NULL,
    UNIQUE(guild_id, name)
);

CREATE TABLE IF NOT EXISTS x_monitors (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          TEXT NOT NULL,
    username          TEXT NOT NULL,
    target_channel_id TEXT NOT NULL,
    platforms         TEXT DEFAULT '["youtube","kick","twitch"]',
    last_post_id      TEXT DEFAULT NULL,
    UNIQUE(guild_id, username)
);

CREATE TABLE IF NOT EXISTS timers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    name             TEXT NOT NULL,
    message          TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    next_run         INTEGER NOT NULL,
    enabled          INTEGER DEFAULT 1,
    UNIQUE(guild_id, name)
);
"""


class Database:
    def __init__(self, path: str = "fluxer_bot.db"):
        self.path = path

    async def initialize(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
        logger.info("Veritabanı başlatıldı ✓")

    # ── Guild Config ──────────────────────────────────────────────────────────

    async def get_prefix(self, guild_id: str) -> str:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT prefix FROM guild_config WHERE guild_id=?", (guild_id,)) as c:
                row = await c.fetchone()
                return row[0] if row else "!"

    async def set_prefix(self, guild_id: str, prefix: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO guild_config(guild_id,prefix) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET prefix=excluded.prefix",
                (guild_id, prefix),
            )
            await db.commit()

    async def get_log_channel(self, guild_id: str) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT log_channel_id FROM guild_config WHERE guild_id=?", (guild_id,)) as c:
                row = await c.fetchone()
                return row[0] if row else None

    async def set_log_channel(self, guild_id: str, channel_id: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO guild_config(guild_id,log_channel_id) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id",
                (guild_id, channel_id),
            )
            await db.commit()

    # ── Link Filter ───────────────────────────────────────────────────────────

    async def get_link_filter(self, guild_id: str) -> Dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT enabled,whitelist_roles,whitelist_channels FROM link_filter WHERE guild_id=?", (guild_id,)
            ) as c:
                row = await c.fetchone()
                if row:
                    return {"enabled": bool(row[0]), "whitelist_roles": json.loads(row[1]), "whitelist_channels": json.loads(row[2])}
                return {"enabled": False, "whitelist_roles": [], "whitelist_channels": []}

    async def set_link_filter_enabled(self, guild_id: str, enabled: bool):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO link_filter(guild_id,enabled) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled",
                (guild_id, int(enabled)),
            )
            await db.commit()

    async def update_link_whitelist(self, guild_id: str, roles: list, channels: list):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO link_filter(guild_id,whitelist_roles,whitelist_channels) VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET whitelist_roles=excluded.whitelist_roles,whitelist_channels=excluded.whitelist_channels",
                (guild_id, json.dumps(roles), json.dumps(channels)),
            )
            await db.commit()

    # ── Regex Filters ─────────────────────────────────────────────────────────

    async def get_regex_filters(self, guild_id: str) -> List[Dict]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT id,name,pattern,action,warn_message FROM regex_filters WHERE guild_id=?", (guild_id,)
            ) as c:
                rows = await c.fetchall()
                return [{"id": r[0], "name": r[1], "pattern": r[2], "action": r[3], "warn_message": r[4]} for r in rows]

    async def add_regex_filter(self, guild_id: str, name: str, pattern: str, action: str = "delete", warn_message: str = None):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO regex_filters(guild_id,name,pattern,action,warn_message) VALUES(?,?,?,?,?)",
                (guild_id, name, pattern, action, warn_message),
            )
            await db.commit()

    async def remove_regex_filter(self, guild_id: str, name: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            c = await db.execute("DELETE FROM regex_filters WHERE guild_id=? AND name=?", (guild_id, name))
            await db.commit()
            return c.rowcount > 0

    # ── X Monitors ────────────────────────────────────────────────────────────

    async def get_x_monitors(self, guild_id: str = None) -> List[Dict]:
        async with aiosqlite.connect(self.path) as db:
            if guild_id:
                q, p = "SELECT id,guild_id,username,target_channel_id,platforms,last_post_id FROM x_monitors WHERE guild_id=?", (guild_id,)
            else:
                q, p = "SELECT id,guild_id,username,target_channel_id,platforms,last_post_id FROM x_monitors", ()
            async with db.execute(q, p) as c:
                rows = await c.fetchall()
                return [{"id": r[0], "guild_id": r[1], "username": r[2], "target_channel_id": r[3], "platforms": json.loads(r[4]), "last_post_id": r[5]} for r in rows]

    async def add_x_monitor(self, guild_id: str, username: str, target_channel_id: str, platforms: list):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO x_monitors(guild_id,username,target_channel_id,platforms) VALUES(?,?,?,?)",
                (guild_id, username.lower().strip("@"), target_channel_id, json.dumps(platforms)),
            )
            await db.commit()

    async def remove_x_monitor(self, guild_id: str, username: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            c = await db.execute("DELETE FROM x_monitors WHERE guild_id=? AND username=?", (guild_id, username.lower().strip("@")))
            await db.commit()
            return c.rowcount > 0

    async def update_last_post_id(self, monitor_id: int, post_id: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE x_monitors SET last_post_id=? WHERE id=?", (post_id, monitor_id))
            await db.commit()

    # ── Timers ────────────────────────────────────────────────────────────────

    async def get_timers(self, guild_id: str = None) -> List[Dict]:
        async with aiosqlite.connect(self.path) as db:
            if guild_id:
                q, p = "SELECT id,guild_id,channel_id,name,message,interval_seconds,next_run,enabled FROM timers WHERE guild_id=?", (guild_id,)
            else:
                q, p = "SELECT id,guild_id,channel_id,name,message,interval_seconds,next_run,enabled FROM timers WHERE enabled=1", ()
            async with db.execute(q, p) as c:
                rows = await c.fetchall()
                return [{"id": r[0], "guild_id": r[1], "channel_id": r[2], "name": r[3], "message": r[4], "interval_seconds": r[5], "next_run": r[6], "enabled": bool(r[7])} for r in rows]

    async def add_timer(self, guild_id: str, channel_id: str, name: str, message: str, interval_seconds: int, next_run: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO timers(guild_id,channel_id,name,message,interval_seconds,next_run) VALUES(?,?,?,?,?,?)",
                (guild_id, channel_id, name, message, interval_seconds, next_run),
            )
            await db.commit()

    async def remove_timer(self, guild_id: str, name: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            c = await db.execute("DELETE FROM timers WHERE guild_id=? AND name=?", (guild_id, name))
            await db.commit()
            return c.rowcount > 0

    async def set_timer_enabled(self, timer_id: int, enabled: bool):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE timers SET enabled=? WHERE id=?", (int(enabled), timer_id))
            await db.commit()

    async def update_timer_next_run(self, timer_id: int, next_run: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE timers SET next_run=? WHERE id=?", (next_run, timer_id))
            await db.commit()
