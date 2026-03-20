"""
╔══════════════════════════════════════════════════════════════╗
║            FLUXERBOT v2 — Native Fluxer API Edition          ║
║      Link Filter · Regex Guard · X Monitor · Timer           ║
╚══════════════════════════════════════════════════════════════╝
discord.py KULLANILMIYOR — Doğrudan api.fluxer.app bağlanır.
"""

import asyncio
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from xml.etree import ElementTree

import aiohttp
from dotenv import load_dotenv

from bot import FluxerGateway, FluxerHTTP
from database import Database
from embed import Color, Embed

load_dotenv()

# ─── Logging ──────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(name)s » %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("FluxerBot")

URL_RE = re.compile(
    r"(https?://[^\s]+|www\.[^\s]+|[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?)",
    re.IGNORECASE,
)
PLATFORM_RE = {
    "youtube": re.compile(r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/)[^\s]+", re.I),
    "kick": re.compile(r"https?://(?:www\.)?kick\.com/[^\s]+", re.I),
    "twitch": re.compile(r"https?://(?:www\.)?twitch\.tv/[^\s]+", re.I),
}
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.1d4.us",
]


# ─── Yardımcı Fonksiyonlar ────────────────────────────────────────────────────

def parse_duration(text: str) -> Optional[int]:
    m = re.match(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", text.strip(), re.I)
    if not m or not any(m.groups()):
        return None
    total = int(m.group(1) or 0)*86400 + int(m.group(2) or 0)*3600 + int(m.group(3) or 0)*60 + int(m.group(4) or 0)
    return total if total > 0 else None


def format_duration(s: int) -> str:
    parts = []
    for unit, label in [(86400,"g"),(3600,"s"),(60,"d"),(1,"sn")]:
        if s >= unit:
            parts.append(f"{s//unit}{label}")
            s %= unit
    return " ".join(parts) or "0sn"


def is_admin(member_data: dict) -> bool:
    """Kullanıcının ADMINISTRATOR yetkisi var mı kontrol eder (permission bit 3)."""
    perms = int(member_data.get("permissions", "0") or "0")
    return bool(perms & (1 << 3))


def mention(user_id: str) -> str:
    return f"<@{user_id}>"


def ch_mention(channel_id: str) -> str:
    return f"<#{channel_id}>"


# ─── Bot ──────────────────────────────────────────────────────────────────────

class FluxerBot:
    def __init__(self, token: str):
        self.token = token
        self.gw = FluxerGateway(token)
        self.http = self.gw.http
        self.db = Database()
        self._nitter_idx = 0
        self._http_session: Optional[aiohttp.ClientSession] = None

    async def _session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "FluxerBot/2.0 RSS"},
            )
        return self._http_session

    # ── Send helpers ──────────────────────────────────────────────────────────

    async def send(self, channel_id: str, embed: Embed = None, text: str = None, delete_after: float = None):
        e_dict = embed.to_dict() if embed else None
        await self.http.send_message(channel_id, content=text, embed=e_dict, delete_after=delete_after)

    async def send_log(self, guild_id: str, embed: Embed):
        ch = await self.db.get_log_channel(guild_id)
        if ch:
            await self.send(ch, embed=embed)

    # ── Background tasks ──────────────────────────────────────────────────────

    async def _timer_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                now = int(time.time())
                timers = await self.db.get_timers()
                for t in timers:
                    if t["next_run"] <= now:
                        e = Embed(description=t["message"], color=Color.BLUE).set_footer(f"⏰ {t['name']}").set_timestamp()
                        await self.send(t["channel_id"], embed=e)
                        await self.db.update_timer_next_run(t["id"], now + t["interval_seconds"])
            except Exception as ex:
                logger.error(f"Timer loop hatası: {ex}", exc_info=True)
            await asyncio.sleep(15)

    async def _x_monitor_loop(self):
        await asyncio.sleep(20)
        while True:
            try:
                await self._check_x_monitors()
            except Exception as ex:
                logger.error(f"X monitor hatası: {ex}", exc_info=True)
            await asyncio.sleep(300)

    async def _check_x_monitors(self):
        monitors = await self.db.get_x_monitors()
        session = await self._session()
        for m in monitors:
            try:
                posts = await self._fetch_rss(session, m["username"])
                if not posts:
                    continue
                last = m["last_post_id"]
                new_posts = []
                for p in posts:
                    if p["id"] == last:
                        break
                    new_posts.append(p)
                if not new_posts:
                    continue
                await self.db.update_last_post_id(m["id"], posts[0]["id"])
                if last is None:
                    continue
                for p in reversed(new_posts):
                    txt = f"{p['title']} {p['content']}"
                    for platform in m["platforms"]:
                        pat = PLATFORM_RE.get(platform)
                        if pat:
                            match = pat.search(txt)
                            if match:
                                link = match.group(0)
                                await self._send_x_notification(m["target_channel_id"], m["username"], p, platform, link)
                                await asyncio.sleep(1)
                await asyncio.sleep(2)
            except Exception as ex:
                logger.error(f"X monitor [{m['username']}]: {ex}")

    async def _fetch_rss(self, session: aiohttp.ClientSession, username: str) -> list:
        for _ in range(len(NITTER_INSTANCES)):
            base = NITTER_INSTANCES[self._nitter_idx % len(NITTER_INSTANCES)]
            self._nitter_idx += 1
            try:
                async with session.get(f"{base}/{username}/rss") as r:
                    if r.status != 200:
                        continue
                    xml = await r.text()
                    return self._parse_rss(xml)
            except Exception:
                continue
        return []

    @staticmethod
    def _parse_rss(xml_text: str) -> list:
        posts = []
        try:
            root = ElementTree.fromstring(xml_text)
            ch = root.find("channel")
            if ch is None:
                return posts
            for item in ch.findall("item"):
                guid = (item.findtext("guid") or "")
                m = re.search(r"/status/(\d+)", guid)
                pid = m.group(1) if m else guid
                posts.append({
                    "id": pid,
                    "title": item.findtext("title") or "",
                    "link": item.findtext("link") or "",
                    "content": item.findtext("description") or "",
                })
        except ElementTree.ParseError:
            pass
        return posts

    async def _send_x_notification(self, channel_id: str, username: str, post: dict, platform: str, link: str):
        colors = {"youtube": Color.RED if False else 0xFF0000, "kick": 0x53FC18, "twitch": 0x9146FF}
        icons = {"youtube": "▶️", "kick": "🟢", "twitch": "🟣"}
        names = {"youtube": "YouTube", "kick": "Kick", "twitch": "Twitch"}
        e = (
            Embed(
                title=f"{icons.get(platform,'🔗')} Yeni {names.get(platform,platform)} — @{username}",
                description=(post["title"] or "")[:400],
                color=colors.get(platform, Color.TWITTER),
                url=link,
            )
            .add_field("🔗 Link", link)
            .set_footer(f"X Monitor • @{username}")
            .set_timestamp()
        )
        if post["link"]:
            x_link = re.sub(r"https?://[^/]+/", "https://x.com/", post["link"])
            e.add_field("𝕏 Tweet", x_link)
        await self.send(channel_id, embed=e)

    # ── Message handler ───────────────────────────────────────────────────────

    async def handle_message(self, data: dict):
        author = data.get("author", {})
        if author.get("bot"):
            return

        content: str = data.get("content", "") or ""
        channel_id: str = data.get("channel_id", "")
        guild_id: str = data.get("guild_id", "")
        message_id: str = data.get("id", "")
        member: dict = data.get("member", {})
        author_id: str = author.get("id", "")

        if not guild_id:
            return

        admin = is_admin(member)
        prefix = await self.db.get_prefix(guild_id)

        # ── Moderation checks (admin değilse) ─────────────────────────────
        if not admin:
            deleted = await self._check_link_filter(guild_id, channel_id, message_id, author_id, member, content)
            if not deleted:
                await self._check_regex_filters(guild_id, channel_id, message_id, author_id, content)

        # ── Command parsing ───────────────────────────────────────────────
        if not content.startswith(prefix):
            return
        if not admin:
            e = Embed("🚫 Yetersiz Yetki", "Bu komutu yalnızca **Sunucu Yöneticileri** kullanabilir.", Color.RED)
            await self.send(channel_id, embed=e, delete_after=6)
            return

        parts = content[len(prefix):].strip().split()
        if not parts:
            return
        cmd = parts[0].lower()
        args = parts[1:]

        await self._route_command(cmd, args, data, guild_id, channel_id, author_id, prefix)

    # ── Moderation ────────────────────────────────────────────────────────────

    async def _check_link_filter(self, guild_id, channel_id, message_id, author_id, member, content) -> bool:
        cfg = await self.db.get_link_filter(guild_id)
        if not cfg["enabled"]:
            return False
        if channel_id in cfg["whitelist_channels"]:
            return False
        member_roles = [r for r in member.get("roles", [])]
        if any(r in cfg["whitelist_roles"] for r in member_roles):
            return False
        if URL_RE.search(content):
            await self.http.delete_message(channel_id, message_id)
            e = Embed(description=f"🔗 {mention(author_id)}, bu kanalda link paylaşmak yasaktır.", color=Color.RED)
            await self.send(channel_id, embed=e, delete_after=5)
            log_e = (
                Embed("🔗 Link Engellendi", color=Color.RED)
                .add_field("Kullanıcı", mention(author_id), True)
                .add_field("Kanal", ch_mention(channel_id), True)
                .add_field("Mesaj", content[:512], False)
                .set_timestamp()
            )
            await self.send_log(guild_id, log_e)
            return True
        return False

    async def _check_regex_filters(self, guild_id, channel_id, message_id, author_id, content):
        filters = await self.db.get_regex_filters(guild_id)
        for f in filters:
            try:
                if re.search(f["pattern"], content, re.IGNORECASE):
                    if f["action"] in ("delete", "warn"):
                        await self.http.delete_message(channel_id, message_id)
                    if f["action"] == "warn" and f["warn_message"]:
                        warn_txt = f["warn_message"].replace("{user}", mention(author_id))
                        await self.send(channel_id, embed=Embed(description=warn_txt, color=Color.YELLOW), delete_after=7)
                    log_e = (
                        Embed("🔎 Regex Filtresi Tetiklendi", color=Color.YELLOW)
                        .add_field("Filtre", f"`{f['name']}`", True)
                        .add_field("Kullanıcı", mention(author_id), True)
                        .add_field("Kanal", ch_mention(channel_id), True)
                        .add_field("Mesaj", content[:512])
                        .set_timestamp()
                    )
                    await self.send_log(guild_id, log_e)
                    break
            except re.error:
                continue

    # ── Command Router ────────────────────────────────────────────────────────

    async def _route_command(self, cmd, args, data, guild_id, channel_id, author_id, prefix):
        # Map: komut → handler
        routes = {
            "help": self._cmd_help,
            "yardım": self._cmd_help,
            "prefix": self._cmd_prefix,
            "setlog": self._cmd_setlog,
            "ping": self._cmd_ping,
            "botinfo": self._cmd_botinfo,
            "clear": self._cmd_clear,
            "temizle": self._cmd_clear,
            "linkfilter": self._cmd_linkfilter,
            "lf": self._cmd_linkfilter,
            "regex": self._cmd_regex,
            "xmonitor": self._cmd_xmonitor,
            "xm": self._cmd_xmonitor,
            "timer": self._cmd_timer,
        }
        handler = routes.get(cmd)
        if handler:
            try:
                await handler(args, guild_id, channel_id, author_id, prefix, data)
            except Exception as ex:
                logger.error(f"Komut hatası [{cmd}]: {ex}", exc_info=True)
                e = Embed("⚠️ Hata", f"`{ex}`", Color.RED)
                await self.send(channel_id, embed=e, delete_after=8)

    # ── Commands ──────────────────────────────────────────────────────────────

    async def _cmd_ping(self, args, guild_id, channel_id, *_):
        await self.send(channel_id, embed=Embed(description="🏓 Pong! Bot çalışıyor.", color=Color.GREEN), delete_after=5)

    async def _cmd_botinfo(self, args, guild_id, channel_id, *_):
        e = (
            Embed("🤖 FluxerBot v2", "Native Fluxer API botu", Color.BLUE)
            .add_field("🔧 Özellikler", "Link Filter • Regex Guard • X Monitor • Timer")
            .add_field("🌐 API", "api.fluxer.app (Native)")
            .set_timestamp()
        )
        await self.send(channel_id, embed=e)

    async def _cmd_prefix(self, args, guild_id, channel_id, *_):
        if not args:
            await self.send(channel_id, embed=Embed(description="⚠️ Kullanım: `prefix <yeni>`", color=Color.YELLOW), delete_after=5)
            return
        new_prefix = args[0][:5]
        await self.db.set_prefix(guild_id, new_prefix)
        await self.send(channel_id, embed=Embed(description=f"✅ Yeni prefix: **`{new_prefix}`**", color=Color.GREEN), delete_after=5)

    async def _cmd_setlog(self, args, guild_id, channel_id, *_, **__):
        # Kanal mention veya ID
        raw = args[0] if args else ""
        ch_id = re.sub(r"[<#>]", "", raw)
        if not ch_id.isdigit():
            await self.send(channel_id, embed=Embed(description="⚠️ Kullanım: `setlog #kanal` veya `setlog KANAL_ID`", color=Color.YELLOW), delete_after=5)
            return
        await self.db.set_log_channel(guild_id, ch_id)
        await self.send(channel_id, embed=Embed(description=f"✅ Log kanalı {ch_mention(ch_id)} olarak ayarlandı.", color=Color.GREEN), delete_after=5)

    async def _cmd_clear(self, args, guild_id, channel_id, *_):
        amount = int(args[0]) if args and args[0].isdigit() else 10
        amount = min(max(amount, 1), 100)
        # Fluxer bulk delete
        msgs = await self.http.request("GET", f"/channels/{channel_id}/messages?limit={amount+1}")
        if isinstance(msgs, list) and msgs:
            ids = [m["id"] for m in msgs]
            if len(ids) == 1:
                await self.http.delete_message(channel_id, ids[0])
            else:
                await self.http.request("POST", f"/channels/{channel_id}/messages/bulk-delete", json={"messages": ids})
        await self.send(channel_id, embed=Embed(description=f"🗑️ **{amount}** mesaj silindi.", color=Color.GREEN), delete_after=4)

    async def _cmd_help(self, args, guild_id, channel_id, author_id, prefix, *_):
        section = args[0].lower() if args else ""
        if section in ("linkfilter", "lf"):
            e = Embed("🔗 Link Filtresi — Komutlar", color=Color.BLUE)
            cmds = [
                (f"{prefix}linkfilter", "Mevcut ayarları gösterir"),
                (f"{prefix}linkfilter on", "Link filtresini aktifleştirir"),
                (f"{prefix}linkfilter off", "Link filtresini devre dışı bırakır"),
                (f"{prefix}linkfilter whitelist role ROL_ID", "Rolü beyaz listeye ekler"),
                (f"{prefix}linkfilter whitelist channel KANAL_ID", "Kanalı beyaz listeye ekler"),
                (f"{prefix}linkfilter whitelist remove ID", "Beyaz listeden kaldırır"),
            ]
            for c, d in cmds:
                e.add_field(f"`{c}`", d)
        elif section == "regex":
            e = Embed("🔎 Regex Filtresi — Komutlar", color=Color.BLUE)
            e.add_field(f"`{prefix}regex`", "Filtre listesini gösterir")
            e.add_field(f"`{prefix}regex add <isim> <desen> [eylem] [uyarı]`", "Filtre ekler (eylem: delete|warn)")
            e.add_field(f"`{prefix}regex remove <isim>`", "Filtre kaldırır")
            e.add_field(f"`{prefix}regex test <isim> <metin>`", "Filtreyi test eder")
            e.add_field("📌 Örnek", f"`{prefix}regex add küfür (kötü1|kötü2) warn {{user}} küfür yapamazsın!`")
        elif section in ("xmonitor", "xm", "x"):
            e = Embed("𝕏 X Monitor — Komutlar", color=Color.TWITTER)
            e.add_field(f"`{prefix}xmonitor`", "İzlenen hesapları listeler")
            e.add_field(f"`{prefix}xmonitor add <kullanıcı> <kanal_id> [platformlar]`", "Hesap ekler")
            e.add_field(f"`{prefix}xmonitor remove <kullanıcı>`", "Hesabı kaldırır")
            e.add_field(f"`{prefix}xmonitor force`", "Feed'leri manuel kontrol eder")
            e.add_field("Platformlar", "`youtube` `kick` `twitch` (boş bırakılırsa hepsi)")
        elif section == "timer":
            e = Embed("⏰ Timer — Komutlar", color=Color.BLUE)
            e.add_field(f"`{prefix}timer`", "Timer listesini gösterir")
            e.add_field(f"`{prefix}timer add <isim> <aralık> <kanal_id> <mesaj>`", "Timer ekler")
            e.add_field(f"`{prefix}timer remove <isim>`", "Timer siler")
            e.add_field(f"`{prefix}timer pause <isim>`", "Duraklatır")
            e.add_field(f"`{prefix}timer resume <isim>`", "Devam ettirir")
            e.add_field(f"`{prefix}timer now <isim>`", "Hemen gönderir")
            e.add_field("Zaman Formatı", "`30s` · `5m` · `2h` · `1d` · `1h30m`")
        else:
            e = (
                Embed("🤖 FluxerBot — Yardım Menüsü", f"Prefix: `{prefix}` · Sadece Yöneticiler kullanabilir", Color.BLUE)
                .add_field("🔗 Link Filtresi", f"`{prefix}help linkfilter`")
                .add_field("🔎 Regex Filtresi", f"`{prefix}help regex`")
                .add_field("𝕏 X Monitor", f"`{prefix}help xmonitor`")
                .add_field("⏰ Timer", f"`{prefix}help timer`")
                .add_field("⚙️ Genel", f"`{prefix}prefix` · `{prefix}setlog` · `{prefix}clear` · `{prefix}botinfo`")
            )
        await self.send(channel_id, embed=e)

    # ── Link Filter Commands ───────────────────────────────────────────────────

    async def _cmd_linkfilter(self, args, guild_id, channel_id, *_):
        sub = args[0].lower() if args else ""
        cfg = await self.db.get_link_filter(guild_id)

        if sub == "on":
            await self.db.set_link_filter_enabled(guild_id, True)
            await self.send(channel_id, embed=Embed(description="✅ Link filtresi **etkinleştirildi**.", color=Color.GREEN), delete_after=5)
        elif sub == "off":
            await self.db.set_link_filter_enabled(guild_id, False)
            await self.send(channel_id, embed=Embed(description="❌ Link filtresi **devre dışı bırakıldı**.", color=Color.RED), delete_after=5)
        elif sub == "whitelist":
            action = args[1].lower() if len(args) > 1 else ""
            raw_id = re.sub(r"[<#@&>]", "", args[2]) if len(args) > 2 else ""
            if action == "role" and raw_id:
                roles = cfg["whitelist_roles"]
                if raw_id not in roles:
                    roles.append(raw_id)
                    await self.db.update_link_whitelist(guild_id, roles, cfg["whitelist_channels"])
                await self.send(channel_id, embed=Embed(description=f"✅ Rol `{raw_id}` beyaz listeye eklendi.", color=Color.GREEN), delete_after=5)
            elif action == "channel" and raw_id:
                channels = cfg["whitelist_channels"]
                if raw_id not in channels:
                    channels.append(raw_id)
                    await self.db.update_link_whitelist(guild_id, cfg["whitelist_roles"], channels)
                await self.send(channel_id, embed=Embed(description=f"✅ Kanal {ch_mention(raw_id)} beyaz listeye eklendi.", color=Color.GREEN), delete_after=5)
            elif action == "remove" and raw_id:
                roles = [r for r in cfg["whitelist_roles"] if r != raw_id]
                channels = [c for c in cfg["whitelist_channels"] if c != raw_id]
                await self.db.update_link_whitelist(guild_id, roles, channels)
                await self.send(channel_id, embed=Embed(description=f"✅ `{raw_id}` beyaz listeden kaldırıldı.", color=Color.GREEN), delete_after=5)
            else:
                await self.send(channel_id, embed=Embed(description="⚠️ Kullanım: `linkfilter whitelist role/channel/remove <ID>`", color=Color.YELLOW), delete_after=6)
        else:
            e = (
                Embed("🔗 Link Filtresi Ayarları", color=Color.BLUE)
                .add_field("Durum", "✅ Aktif" if cfg["enabled"] else "❌ Pasif")
                .add_field("Beyaz Liste Roller", ", ".join(cfg["whitelist_roles"]) or "*Yok*")
                .add_field("Beyaz Liste Kanallar", ", ".join(ch_mention(c) for c in cfg["whitelist_channels"]) or "*Yok*")
            )
            await self.send(channel_id, embed=e)

    # ── Regex Commands ────────────────────────────────────────────────────────

    async def _cmd_regex(self, args, guild_id, channel_id, *_):
        sub = args[0].lower() if args else ""
        if sub == "add":
            # !regex add <isim> <desen> [eylem] [uyarı...]
            if len(args) < 3:
                await self.send(channel_id, embed=Embed(description="⚠️ Kullanım: `regex add <isim> <desen> [delete|warn] [uyarı mesajı]`", color=Color.YELLOW), delete_after=6)
                return
            name, pattern = args[1], args[2]
            action = args[3].lower() if len(args) > 3 and args[3].lower() in ("delete","warn") else "delete"
            warn_msg_start = 4 if len(args) > 3 and args[3].lower() in ("delete","warn") else 3
            warn_message = " ".join(args[warn_msg_start:]) or None
            try:
                re.compile(pattern)
            except re.error as err:
                await self.send(channel_id, embed=Embed(description=f"❌ Geçersiz regex: `{err}`", color=Color.RED), delete_after=6)
                return
            await self.db.add_regex_filter(guild_id, name, pattern, action, warn_message)
            e = (Embed("✅ Regex Filtresi Eklendi", color=Color.GREEN)
                 .add_field("İsim", f"`{name}`", True)
                 .add_field("Desen", f"`{pattern}`", True)
                 .add_field("Eylem", f"`{action}`", True))
            if warn_message:
                e.add_field("Uyarı", warn_message)
            await self.send(channel_id, embed=e)
        elif sub in ("remove", "sil", "del"):
            if len(args) < 2:
                await self.send(channel_id, embed=Embed(description="⚠️ Kullanım: `regex remove <isim>`", color=Color.YELLOW), delete_after=5)
                return
            ok = await self.db.remove_regex_filter(guild_id, args[1])
            color = Color.GREEN if ok else Color.RED
            txt = f"✅ `{args[1]}` kaldırıldı." if ok else f"❌ `{args[1]}` bulunamadı."
            await self.send(channel_id, embed=Embed(description=txt, color=color), delete_after=5)
        elif sub == "test":
            if len(args) < 3:
                await self.send(channel_id, embed=Embed(description="⚠️ Kullanım: `regex test <isim> <metin>`", color=Color.YELLOW), delete_after=5)
                return
            name = args[1]
            text = " ".join(args[2:])
            filters = await self.db.get_regex_filters(guild_id)
            flt = next((f for f in filters if f["name"] == name), None)
            if not flt:
                await self.send(channel_id, embed=Embed(description=f"❌ `{name}` bulunamadı.", color=Color.RED), delete_after=5)
                return
            m = re.search(flt["pattern"], text, re.IGNORECASE)
            result = f"✅ Eşleşme: `{m.group()}`" if m else "❌ Eşleşme yok."
            await self.send(channel_id, embed=Embed(f"🧪 Test — `{name}`", result, Color.GREEN if m else Color.RED))
        else:
            filters = await self.db.get_regex_filters(guild_id)
            if not filters:
                await self.send(channel_id, embed=Embed(description="📋 Henüz regex filtresi eklenmemiş.", color=Color.BLUE))
                return
            e = Embed("🔎 Regex Filtreleri", color=Color.BLUE)
            for f in filters:
                e.add_field(f"`{f['name']}`", f"Desen: `{f['pattern']}`\nEylem: `{f['action']}`" + (f"\nUyarı: {f['warn_message']}" if f["warn_message"] else ""))
            await self.send(channel_id, embed=e)

    # ── X Monitor Commands ────────────────────────────────────────────────────

    async def _cmd_xmonitor(self, args, guild_id, channel_id, *_):
        sub = args[0].lower() if args else ""
        if sub in ("add", "ekle"):
            # !xmonitor add <kullanıcı> <kanal_id> [platformlar...]
            if len(args) < 3:
                await self.send(channel_id, embed=Embed(description="⚠️ Kullanım: `xmonitor add <kullanıcı> <kanal_id> [youtube kick twitch]`", color=Color.YELLOW), delete_after=6)
                return
            username = args[1].strip("@").lower()
            ch_id = re.sub(r"[<#>]", "", args[2])
            valid = {"youtube", "kick", "twitch"}
            platforms = [p.lower() for p in args[3:] if p.lower() in valid] or list(valid)
            await self.db.add_x_monitor(guild_id, username, ch_id, platforms)
            e = (Embed("✅ X Monitor Eklendi", color=Color.GREEN)
                 .add_field("Kullanıcı", f"@{username}", True)
                 .add_field("Kanal", ch_mention(ch_id), True)
                 .add_field("Platformlar", ", ".join(platforms).title(), True)
                 .set_footer("Her 5 dakikada kontrol edilir."))
            await self.send(channel_id, embed=e)
        elif sub in ("remove", "sil", "kaldır"):
            if len(args) < 2:
                await self.send(channel_id, embed=Embed(description="⚠️ Kullanım: `xmonitor remove <kullanıcı>`", color=Color.YELLOW), delete_after=5)
                return
            ok = await self.db.remove_x_monitor(guild_id, args[1])
            txt = f"✅ `@{args[1].strip('@')}` kaldırıldı." if ok else f"❌ `@{args[1].strip('@')}` bulunamadı."
            await self.send(channel_id, embed=Embed(description=txt, color=Color.GREEN if ok else Color.RED), delete_after=5)
        elif sub == "force":
            msg = await self.http.send_message(channel_id, embed=Embed(description="🔄 X feed'leri kontrol ediliyor...", color=Color.BLUE).to_dict())
            await self._check_x_monitors()
            if msg and "id" in msg:
                await self.http.request("PATCH", f"/channels/{channel_id}/messages/{msg['id']}", json={"embeds": [Embed(description="✅ X feed kontrolü tamamlandı.", color=Color.GREEN).to_dict()]})
        else:
            monitors = await self.db.get_x_monitors(guild_id)
            if not monitors:
                await self.send(channel_id, embed=Embed(description="📋 İzlenen X hesabı yok.", color=Color.BLUE))
                return
            e = Embed("𝕏 İzlenen X Hesapları", color=Color.TWITTER)
            for m in monitors:
                e.add_field(f"@{m['username']}", f"📢 {ch_mention(m['target_channel_id'])}\n🎯 {', '.join(m['platforms']).title()}")
            await self.send(channel_id, embed=e)

    # ── Timer Commands ────────────────────────────────────────────────────────

    async def _cmd_timer(self, args, guild_id, channel_id, *_):
        sub = args[0].lower() if args else ""
        if sub in ("add", "ekle"):
            # !timer add <isim> <aralık> <kanal_id> <mesaj...>
            if len(args) < 5:
                await self.send(channel_id, embed=Embed(description="⚠️ Kullanım: `timer add <isim> <aralık> <kanal_id> <mesaj>`\nÖrnek: `timer add duyuru 6h 123456789 Merhaba!`", color=Color.YELLOW), delete_after=8)
                return
            name, interval_str = args[1], args[2]
            ch_id = re.sub(r"[<#>]", "", args[3])
            message = " ".join(args[4:])
            secs = parse_duration(interval_str)
            if not secs or secs < 30:
                await self.send(channel_id, embed=Embed(description="❌ Geçersiz zaman. Örnekler: `30s` `5m` `2h` `1d`\nMinimum: `30s`", color=Color.RED), delete_after=6)
                return
            next_run = int(time.time()) + secs
            await self.db.add_timer(guild_id, ch_id, name, message, secs, next_run)
            e = (Embed("✅ Timer Eklendi", color=Color.GREEN)
                 .add_field("İsim", f"`{name}`", True)
                 .add_field("Aralık", f"`{format_duration(secs)}`", True)
                 .add_field("Kanal", ch_mention(ch_id), True)
                 .add_field("Mesaj", message[:300]))
            await self.send(channel_id, embed=e)
        elif sub in ("remove", "sil", "kaldır"):
            if len(args) < 2:
                return
            ok = await self.db.remove_timer(guild_id, args[1])
            txt = f"✅ `{args[1]}` kaldırıldı." if ok else f"❌ `{args[1]}` bulunamadı."
            await self.send(channel_id, embed=Embed(description=txt, color=Color.GREEN if ok else Color.RED), delete_after=5)
        elif sub in ("pause", "durdur"):
            timers = await self.db.get_timers(guild_id)
            t = next((x for x in timers if x["name"] == args[1]), None) if len(args) > 1 else None
            if t:
                await self.db.set_timer_enabled(t["id"], False)
                await self.send(channel_id, embed=Embed(description=f"⏸️ `{args[1]}` duraklatıldı.", color=Color.YELLOW), delete_after=5)
        elif sub in ("resume", "devam"):
            timers = await self.db.get_timers(guild_id)
            t = next((x for x in timers if x["name"] == args[1]), None) if len(args) > 1 else None
            if t:
                await self.db.set_timer_enabled(t["id"], True)
                await self.db.update_timer_next_run(t["id"], int(time.time()) + t["interval_seconds"])
                await self.send(channel_id, embed=Embed(description=f"▶️ `{args[1]}` yeniden başlatıldı.", color=Color.GREEN), delete_after=5)
        elif sub == "now":
            timers = await self.db.get_timers(guild_id)
            t = next((x for x in timers if x["name"] == args[1]), None) if len(args) > 1 else None
            if t:
                e = Embed(description=t["message"], color=Color.BLUE).set_footer(f"⏰ {t['name']}").set_timestamp()
                await self.send(t["channel_id"], embed=e)
                await self.send(channel_id, embed=Embed(description=f"✅ `{args[1]}` mesajı {ch_mention(t['channel_id'])} kanalına gönderildi.", color=Color.GREEN), delete_after=4)
        else:
            timers = await self.db.get_timers(guild_id)
            if not timers:
                await self.send(channel_id, embed=Embed(description="⏰ Henüz timer eklenmemiş.", color=Color.BLUE))
                return
            now = int(time.time())
            e = Embed("⏰ Timer Listesi", color=Color.BLUE)
            for t in timers:
                remaining = max(0, t["next_run"] - now)
                status = "✅" if t["enabled"] else "⏸️"
                e.add_field(
                    f"{status} `{t['name']}`",
                    f"Kanal: {ch_mention(t['channel_id'])}\nAralık: `{format_duration(t['interval_seconds'])}`\nSonraki: `{format_duration(remaining)} sonra`",
                )
            await self.send(channel_id, embed=e)

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(self):
        await self.db.initialize()

        # Event handlers bağla
        self.gw.add_handler("MESSAGE_CREATE", self.handle_message)

        # Background tasks başlat
        asyncio.create_task(self._timer_loop())
        asyncio.create_task(self._x_monitor_loop())

        # Gateway'e bağlan (reconnect döngüsü içinde)
        await self.gw.connect()


# ─── Entry Point ──────────────────────────────────────────────────────────────

async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.critical("BOT_TOKEN bulunamadı! Railway Variables'a ekle.")
        sys.exit(1)

    bot = FluxerBot(token)
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Bot kapatılıyor...")
        await bot.gw.close()


if __name__ == "__main__":
    asyncio.run(main())
