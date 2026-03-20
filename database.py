import json, logging
from typing import Any, Dict, List, Optional
import aiosqlite

logger = logging.getLogger("Database")

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS guild_config (guild_id TEXT PRIMARY KEY, prefix TEXT DEFAULT '!', log_channel_id TEXT DEFAULT NULL);
CREATE TABLE IF NOT EXISTS link_filter (guild_id TEXT PRIMARY KEY, enabled INTEGER DEFAULT 0, whitelist_roles TEXT DEFAULT '[]', whitelist_channels TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS regex_filters (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, name TEXT NOT NULL, pattern TEXT NOT NULL, action TEXT DEFAULT 'delete', warn_message TEXT DEFAULT NULL, UNIQUE(guild_id, name));
CREATE TABLE IF NOT EXISTS x_monitors (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, username TEXT NOT NULL, target_channel_id TEXT NOT NULL, platforms TEXT DEFAULT '["youtube","kick","twitch"]', last_post_id TEXT DEFAULT NULL, UNIQUE(guild_id, username));
CREATE TABLE IF NOT EXISTS timers (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, name TEXT NOT NULL, message TEXT NOT NULL, interval_seconds INTEGER NOT NULL, next_run INTEGER NOT NULL, enabled INTEGER DEFAULT 1, UNIQUE(guild_id, name));
"""

class Database:
    def __init__(self, path="fluxer_bot.db"):
        self.path = path

    async def initialize(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
        logger.info("Veritabanı başlatıldı ✓")

    async def get_prefix(self, g): 
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT prefix FROM guild_config WHERE guild_id=?", (g,)) as c:
                r = await c.fetchone(); return r[0] if r else "!"

    async def set_prefix(self, g, p):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO guild_config(guild_id,prefix) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET prefix=excluded.prefix", (g,p)); await db.commit()

    async def get_log_channel(self, g):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT log_channel_id FROM guild_config WHERE guild_id=?", (g,)) as c:
                r = await c.fetchone(); return r[0] if r else None

    async def set_log_channel(self, g, c):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO guild_config(guild_id,log_channel_id) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id", (g,c)); await db.commit()

    async def get_link_filter(self, g):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT enabled,whitelist_roles,whitelist_channels FROM link_filter WHERE guild_id=?", (g,)) as c:
                r = await c.fetchone()
                return {"enabled":bool(r[0]),"whitelist_roles":json.loads(r[1]),"whitelist_channels":json.loads(r[2])} if r else {"enabled":False,"whitelist_roles":[],"whitelist_channels":[]}

    async def set_link_filter_enabled(self, g, e):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO link_filter(guild_id,enabled) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled", (g,int(e))); await db.commit()

    async def update_link_whitelist(self, g, roles, channels):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO link_filter(guild_id,whitelist_roles,whitelist_channels) VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET whitelist_roles=excluded.whitelist_roles,whitelist_channels=excluded.whitelist_channels", (g,json.dumps(roles),json.dumps(channels))); await db.commit()

    async def get_regex_filters(self, g):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT id,name,pattern,action,warn_message FROM regex_filters WHERE guild_id=?", (g,)) as c:
                return [{"id":r[0],"name":r[1],"pattern":r[2],"action":r[3],"warn_message":r[4]} for r in await c.fetchall()]

    async def add_regex_filter(self, g, name, pattern, action="delete", warn_message=None):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR REPLACE INTO regex_filters(guild_id,name,pattern,action,warn_message) VALUES(?,?,?,?,?)", (g,name,pattern,action,warn_message)); await db.commit()

    async def remove_regex_filter(self, g, name):
        async with aiosqlite.connect(self.path) as db:
            c = await db.execute("DELETE FROM regex_filters WHERE guild_id=? AND name=?", (g,name)); await db.commit(); return c.rowcount > 0

    async def get_x_monitors(self, g=None):
        async with aiosqlite.connect(self.path) as db:
            q = "SELECT id,guild_id,username,target_channel_id,platforms,last_post_id FROM x_monitors" + (" WHERE guild_id=?" if g else "")
            async with db.execute(q, (g,) if g else ()) as c:
                return [{"id":r[0],"guild_id":r[1],"username":r[2],"target_channel_id":r[3],"platforms":json.loads(r[4]),"last_post_id":r[5]} for r in await c.fetchall()]

    async def add_x_monitor(self, g, username, ch, platforms):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR REPLACE INTO x_monitors(guild_id,username,target_channel_id,platforms) VALUES(?,?,?,?)", (g,username.lower().strip("@"),ch,json.dumps(platforms))); await db.commit()

    async def remove_x_monitor(self, g, username):
        async with aiosqlite.connect(self.path) as db:
            c = await db.execute("DELETE FROM x_monitors WHERE guild_id=? AND username=?", (g,username.lower().strip("@"))); await db.commit(); return c.rowcount > 0

    async def update_last_post_id(self, id, post_id):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE x_monitors SET last_post_id=? WHERE id=?", (post_id,id)); await db.commit()

    async def get_timers(self, g=None):
        async with aiosqlite.connect(self.path) as db:
            if g: q,p = "SELECT id,guild_id,channel_id,name,message,interval_seconds,next_run,enabled FROM timers WHERE guild_id=?",(g,)
            else: q,p = "SELECT id,guild_id,channel_id,name,message,interval_seconds,next_run,enabled FROM timers WHERE enabled=1",()
            async with db.execute(q,p) as c:
                return [{"id":r[0],"guild_id":r[1],"channel_id":r[2],"name":r[3],"message":r[4],"interval_seconds":r[5],"next_run":r[6],"enabled":bool(r[7])} for r in await c.fetchall()]

    async def add_timer(self, g, ch, name, message, secs, next_run):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR REPLACE INTO timers(guild_id,channel_id,name,message,interval_seconds,next_run) VALUES(?,?,?,?,?,?)", (g,ch,name,message,secs,next_run)); await db.commit()

    async def remove_timer(self, g, name):
        async with aiosqlite.connect(self.path) as db:
            c = await db.execute("DELETE FROM timers WHERE guild_id=? AND name=?", (g,name)); await db.commit(); return c.rowcount > 0

    async def set_timer_enabled(self, id, e):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE timers SET enabled=? WHERE id=?", (int(e),id)); await db.commit()

    async def update_timer_next_run(self, id, next_run):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE timers SET next_run=? WHERE id=?", (next_run,id)); await db.commit()
