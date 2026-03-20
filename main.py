"""
╔══════════════════════════════════════════════════════╗
║         FLUXERBOT v4 — fluxer.py Official Library    ║
║   Link Filter · Regex Guard · X Monitor · Timer      ║
╚══════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from xml.etree import ElementTree

import aiohttp
import fluxer
from dotenv import load_dotenv

from database import Database

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

# ─── Regex Patterns ───────────────────────────────────────────────────────────
URL_RE = re.compile(
    r"(https?://[^\s]+|www\.[^\s]+|\b[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?)",
    re.IGNORECASE,
)
PLATFORM_RE = {
    "youtube": re.compile(r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/)\S+", re.I),
    "kick":    re.compile(r"https?://(?:www\.)?kick\.com/\S+", re.I),
    "twitch":  re.compile(r"https?://(?:www\.)?twitch\.tv/\S+", re.I),
}
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.1d4.us",
]

# ─── Colors ───────────────────────────────────────────────────────────────────
RED    = 0xED4245
GREEN  = 0x57F287
YELLOW = 0xFEE75C
BLUE   = 0x5865F2
TWITTER = 0x1DA1F2


def parse_duration(text: str) -> Optional[int]:
    m = re.match(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", text.strip(), re.I)
    if not m or not any(m.groups()):
        return None
    total = (int(m.group(1) or 0)*86400 + int(m.group(2) or 0)*3600
             + int(m.group(3) or 0)*60 + int(m.group(4) or 0))
    return total if total > 0 else None


def fmt_dur(s: int) -> str:
    parts = []
    for unit, lbl in [(86400,"g"),(3600,"s"),(60,"d"),(1,"sn")]:
        if s >= unit:
            parts.append(f"{s//unit}{lbl}")
            s %= unit
    return " ".join(parts) or "0sn"


def embed(title=None, desc=None, color=BLUE, fields=None, footer=None) -> fluxer.Embed:
    f_list = []
    if fields:
        for item in fields:
            if len(item) == 3:
                f_list.append({"name": item[0], "value": item[1], "inline": item[2]})
            else:
                f_list.append({"name": item[0], "value": item[1], "inline": False})
    ft = {"text": footer} if footer else None
    return fluxer.Embed(title=title, description=desc, color=color, fields=f_list, footer=ft)


# ─── Bot Setup ────────────────────────────────────────────────────────────────
intents = (
    fluxer.Intents.GUILDS
    | fluxer.Intents.GUILD_MESSAGES
    | fluxer.Intents.GUILD_MEMBERS
    | fluxer.Intents.MESSAGE_CONTENT
    | fluxer.Intents.DIRECT_MESSAGES
)

bot = fluxer.Bot(command_prefix="!", intents=intents)
db = Database()
_nitter_idx = 0
_http_session: Optional[aiohttp.ClientSession] = None


async def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "FluxerBot/4.0"},
        )
    return _http_session


# ─── Permission Check ─────────────────────────────────────────────────────────
async def is_admin(msg: fluxer.Message) -> bool:
    if not msg.guild_id:
        return False
    try:
        guild = await bot.fetch_guild(str(msg.guild_id))
        member = await guild.fetch_member(msg.author.id)
        perms = member.roles  # list of Role objects
        # owner is always admin
        if str(guild.owner_id) == str(msg.author.id):
            return True
        for role in perms:
            if hasattr(role, 'permissions'):
                if role.permissions & fluxer.Permissions.ADMINISTRATOR:
                    return True
    except Exception:
        pass
    return False


async def send_log(guild_id: str, e: fluxer.Embed):
    ch_id = await db.get_log_channel(guild_id)
    if not ch_id:
        return
    try:
        ch = await bot.fetch_channel(ch_id)
        await ch.send(embed=e)
    except Exception:
        pass


# ─── Ready ────────────────────────────────────────────────────────────────────
@bot.on("READY")
async def on_ready(data):
    user = data.get("user", {})
    logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info(f"  Bot hazır: {user.get('username')}#{user.get('discriminator','0')}")
    logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    asyncio.create_task(_timer_loop())
    asyncio.create_task(_x_monitor_loop())


# ─── Message Create ───────────────────────────────────────────────────────────
@bot.on("MESSAGE_CREATE")
async def on_message(msg: fluxer.Message):
    if msg.author.bot:
        return
    if not msg.guild_id:
        return

    guild_id = str(msg.guild_id)
    channel_id = str(msg.channel_id)
    content = msg.content or ""

    admin = await is_admin(msg)

    # Moderasyon kontrolleri (admin değilse)
    if not admin:
        deleted = await _check_link_filter(msg, guild_id, channel_id, content)
        if not deleted:
            await _check_regex_filters(msg, guild_id, channel_id, content)

    # Komut kontrolü
    prefix = await db.get_prefix(guild_id)
    if not content.startswith(prefix):
        return

    if not admin:
        ch = await bot.fetch_channel(channel_id)
        await ch.send(embed=embed("🚫 Yetersiz Yetki", "Bu komutu yalnızca **Sunucu Yöneticileri** kullanabilir.", RED))
        return

    parts = content[len(prefix):].strip().split()
    if not parts:
        return

    cmd = parts[0].lower()
    args = parts[1:]

    await _route(cmd, args, msg, guild_id, channel_id, prefix)


# ─── Moderation ───────────────────────────────────────────────────────────────
async def _check_link_filter(msg: fluxer.Message, guild_id, channel_id, content) -> bool:
    cfg = await db.get_link_filter(guild_id)
    if not cfg["enabled"]:
        return False
    if channel_id in cfg["whitelist_channels"]:
        return False
    try:
        guild = await bot.fetch_guild(guild_id)
        member = await guild.fetch_member(msg.author.id)
        member_role_ids = [str(r.id) for r in member.roles]
        if any(r in cfg["whitelist_roles"] for r in member_role_ids):
            return False
    except Exception:
        pass
    if URL_RE.search(content):
        try:
            await msg.delete()
        except Exception:
            pass
        ch = await bot.fetch_channel(channel_id)
        await ch.send(embed=embed(desc=f"🔗 <@{msg.author.id}>, bu kanalda link paylaşmak yasaktır.", color=RED))
        await send_log(guild_id, embed(
            "🔗 Link Engellendi", color=RED,
            fields=[
                ("Kullanıcı", f"<@{msg.author.id}>", True),
                ("Kanal", f"<#{channel_id}>", True),
                ("Mesaj", content[:512], False),
            ]
        ))
        return True
    return False


async def _check_regex_filters(msg: fluxer.Message, guild_id, channel_id, content):
    filters = await db.get_regex_filters(guild_id)
    for f in filters:
        try:
            if re.search(f["pattern"], content, re.IGNORECASE):
                if f["action"] in ("delete", "warn"):
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                if f["action"] == "warn" and f["warn_message"]:
                    warn_txt = f["warn_message"].replace("{user}", f"<@{msg.author.id}>")
                    ch = await bot.fetch_channel(channel_id)
                    await ch.send(embed=embed(desc=warn_txt, color=YELLOW))
                await send_log(guild_id, embed(
                    "🔎 Regex Filtresi Tetiklendi", color=YELLOW,
                    fields=[
                        ("Filtre", f"`{f['name']}`", True),
                        ("Kullanıcı", f"<@{msg.author.id}>", True),
                        ("Kanal", f"<#{channel_id}>", True),
                        ("Mesaj", content[:512], False),
                    ]
                ))
                break
        except re.error:
            continue


# ─── Command Router ───────────────────────────────────────────────────────────
async def _route(cmd, args, msg, guild_id, channel_id, prefix):
    ch = await bot.fetch_channel(channel_id)
    handlers = {
        "ping": _cmd_ping, "botinfo": _cmd_botinfo,
        "prefix": _cmd_prefix, "setlog": _cmd_setlog,
        "clear": _cmd_clear, "temizle": _cmd_clear,
        "help": _cmd_help, "yardım": _cmd_help,
        "linkfilter": _cmd_linkfilter, "lf": _cmd_linkfilter,
        "regex": _cmd_regex,
        "xmonitor": _cmd_xmonitor, "xm": _cmd_xmonitor,
        "timer": _cmd_timer,
    }
    handler = handlers.get(cmd)
    if handler:
        try:
            await handler(args, msg, guild_id, ch, prefix)
        except Exception as ex:
            logger.error(f"Komut hatası [{cmd}]: {ex}", exc_info=True)
            await ch.send(embed=embed("⚠️ Hata", f"`{ex}`", RED))


# ─── General Commands ─────────────────────────────────────────────────────────
async def _cmd_ping(args, msg, guild_id, ch, prefix):
    await ch.send(embed=embed(desc="🏓 Pong! Bot çalışıyor.", color=GREEN))

async def _cmd_botinfo(args, msg, guild_id, ch, prefix):
    await ch.send(embed=embed(
        "🤖 FluxerBot v4", "Native fluxer.py kütüphanesi", BLUE,
        fields=[("🔧 Özellikler", "Link Filter • Regex Guard • X Monitor • Timer", False)]
    ))

async def _cmd_prefix(args, msg, guild_id, ch, prefix):
    if not args:
        await ch.send(embed=embed(desc="⚠️ Kullanım: `prefix <yeni>`", color=YELLOW))
        return
    new = args[0][:5]
    await db.set_prefix(guild_id, new)
    await ch.send(embed=embed(desc=f"✅ Yeni prefix: **`{new}`**", color=GREEN))

async def _cmd_setlog(args, msg, guild_id, ch, prefix):
    raw = args[0] if args else ""
    ch_id = re.sub(r"[<#>]", "", raw)
    if not ch_id.isdigit():
        await ch.send(embed=embed(desc="⚠️ Kullanım: `setlog #kanal` veya `setlog KANAL_ID`", color=YELLOW))
        return
    await db.set_log_channel(guild_id, ch_id)
    await ch.send(embed=embed(desc=f"✅ Log kanalı <#{ch_id}> olarak ayarlandı.", color=GREEN))

async def _cmd_clear(args, msg, guild_id, ch, prefix):
    amount = int(args[0]) if args and args[0].isdigit() else 10
    amount = min(max(amount, 1), 100)
    msgs = await ch.fetch_messages(limit=amount + 1)
    if msgs:
        ids = [m.id for m in msgs]
        try:
            await ch.delete_messages(ids)
        except Exception:
            for mid in ids:
                try:
                    await bot.delete_message(ch.id, mid)
                except Exception:
                    pass
    await ch.send(embed=embed(desc=f"🗑️ **{amount}** mesaj silindi.", color=GREEN))

async def _cmd_help(args, msg, guild_id, ch, prefix):
    section = args[0].lower() if args else ""
    if section in ("linkfilter", "lf"):
        e = embed("🔗 Link Filtresi", color=BLUE, fields=[
            (f"`{prefix}linkfilter`", "Ayarları gösterir", False),
            (f"`{prefix}linkfilter on/off`", "Aktifleştirir / devre dışı bırakır", False),
            (f"`{prefix}linkfilter whitelist role ROL_ID`", "Rolü beyaz listeye ekler", False),
            (f"`{prefix}linkfilter whitelist channel KANAL_ID`", "Kanalı beyaz listeye ekler", False),
            (f"`{prefix}linkfilter whitelist remove ID`", "Beyaz listeden kaldırır", False),
        ])
    elif section == "regex":
        e = embed("🔎 Regex Filtresi", color=BLUE, fields=[
            (f"`{prefix}regex`", "Filtre listesi", False),
            (f"`{prefix}regex add <isim> <desen> [delete|warn] [uyarı]`", "Filtre ekler", False),
            (f"`{prefix}regex remove <isim>`", "Filtre siler", False),
            (f"`{prefix}regex test <isim> <metin>`", "Test eder", False),
            ("📌 Örnek", f"`{prefix}regex add reklam discord\\.gg delete`\n`{prefix}regex add küfür (kelime1|kelime2) warn {{user}} uyarı!`", False),
        ])
    elif section in ("xmonitor", "xm"):
        e = embed("𝕏 X Monitor", color=TWITTER, fields=[
            (f"`{prefix}xmonitor`", "İzlenen hesaplar", False),
            (f"`{prefix}xmonitor add <kullanıcı> <kanal_id> [youtube kick twitch]`", "Hesap ekler", False),
            (f"`{prefix}xmonitor remove <kullanıcı>`", "Kaldırır", False),
            (f"`{prefix}xmonitor force`", "Manuel kontrol", False),
        ])
    elif section == "timer":
        e = embed("⏰ Timer", color=BLUE, fields=[
            (f"`{prefix}timer`", "Timer listesi", False),
            (f"`{prefix}timer add <isim> <aralık> <kanal_id> <mesaj>`", "Timer ekler", False),
            (f"`{prefix}timer remove/pause/resume/now <isim>`", "Yönetim", False),
            ("Zaman Formatı", "`30s` · `5m` · `2h` · `1d` · `1h30m`", False),
            ("📌 Örnek", f"`{prefix}timer add duyuru 6h 123456789 Mesaj buraya`", False),
        ])
    else:
        e = embed(
            "🤖 FluxerBot — Yardım",
            f"Prefix: `{prefix}` · Tüm komutlar sadece Yöneticilere açık",
            BLUE,
            fields=[
                ("🔗 Link Filtresi", f"`{prefix}help linkfilter`", False),
                ("🔎 Regex Filtresi", f"`{prefix}help regex`", False),
                ("𝕏 X Monitor", f"`{prefix}help xmonitor`", False),
                ("⏰ Timer", f"`{prefix}help timer`", False),
                ("⚙️ Genel", f"`{prefix}prefix` · `{prefix}setlog` · `{prefix}clear` · `{prefix}botinfo`", False),
            ]
        )
    await ch.send(embed=e)


# ─── Link Filter Commands ─────────────────────────────────────────────────────
async def _cmd_linkfilter(args, msg, guild_id, ch, prefix):
    sub = args[0].lower() if args else ""
    cfg = await db.get_link_filter(guild_id)

    if sub == "on":
        await db.set_link_filter_enabled(guild_id, True)
        await ch.send(embed=embed(desc="✅ Link filtresi **etkinleştirildi**.", color=GREEN))
    elif sub == "off":
        await db.set_link_filter_enabled(guild_id, False)
        await ch.send(embed=embed(desc="❌ Link filtresi **devre dışı bırakıldı**.", color=RED))
    elif sub == "whitelist" and len(args) >= 3:
        action = args[1].lower()
        raw_id = re.sub(r"[<#@&>]", "", args[2])
        if action == "role":
            roles = cfg["whitelist_roles"]
            if raw_id not in roles:
                roles.append(raw_id)
                await db.update_link_whitelist(guild_id, roles, cfg["whitelist_channels"])
            await ch.send(embed=embed(desc=f"✅ Rol `{raw_id}` beyaz listeye eklendi.", color=GREEN))
        elif action == "channel":
            channels = cfg["whitelist_channels"]
            if raw_id not in channels:
                channels.append(raw_id)
                await db.update_link_whitelist(guild_id, cfg["whitelist_roles"], channels)
            await ch.send(embed=embed(desc=f"✅ Kanal <#{raw_id}> beyaz listeye eklendi.", color=GREEN))
        elif action == "remove":
            roles = [r for r in cfg["whitelist_roles"] if r != raw_id]
            channels = [c for c in cfg["whitelist_channels"] if c != raw_id]
            await db.update_link_whitelist(guild_id, roles, channels)
            await ch.send(embed=embed(desc=f"✅ `{raw_id}` beyaz listeden kaldırıldı.", color=GREEN))
    else:
        await ch.send(embed=embed("🔗 Link Filtresi Ayarları", color=BLUE, fields=[
            ("Durum", "✅ Aktif" if cfg["enabled"] else "❌ Pasif", False),
            ("Beyaz Liste Roller", ", ".join(cfg["whitelist_roles"]) or "*Yok*", False),
            ("Beyaz Liste Kanallar", ", ".join(f"<#{c}>" for c in cfg["whitelist_channels"]) or "*Yok*", False),
        ]))


# ─── Regex Commands ───────────────────────────────────────────────────────────
async def _cmd_regex(args, msg, guild_id, ch, prefix):
    sub = args[0].lower() if args else ""
    if sub == "add":
        if len(args) < 3:
            await ch.send(embed=embed(desc="⚠️ Kullanım: `regex add <isim> <desen> [delete|warn] [uyarı]`", color=YELLOW))
            return
        name, pattern = args[1], args[2]
        action = args[3].lower() if len(args) > 3 and args[3].lower() in ("delete","warn") else "delete"
        start = 4 if len(args) > 3 and args[3].lower() in ("delete","warn") else 3
        warn_msg = " ".join(args[start:]) or None
        try:
            re.compile(pattern)
        except re.error as err:
            await ch.send(embed=embed(desc=f"❌ Geçersiz regex: `{err}`", color=RED))
            return
        await db.add_regex_filter(guild_id, name, pattern, action, warn_msg)
        await ch.send(embed=embed("✅ Regex Filtresi Eklendi", color=GREEN, fields=[
            ("İsim", f"`{name}`", True), ("Desen", f"`{pattern}`", True), ("Eylem", f"`{action}`", True),
        ]))
    elif sub in ("remove", "sil", "del"):
        if len(args) < 2:
            return
        ok = await db.remove_regex_filter(guild_id, args[1])
        await ch.send(embed=embed(desc=f"{'✅' if ok else '❌'} `{args[1]}` {'kaldırıldı' if ok else 'bulunamadı'}.", color=GREEN if ok else RED))
    elif sub == "test":
        if len(args) < 3:
            return
        text = " ".join(args[2:])
        filters = await db.get_regex_filters(guild_id)
        flt = next((f for f in filters if f["name"] == args[1]), None)
        if not flt:
            await ch.send(embed=embed(desc=f"❌ `{args[1]}` bulunamadı.", color=RED))
            return
        m = re.search(flt["pattern"], text, re.IGNORECASE)
        await ch.send(embed=embed(f"🧪 Test — `{args[1]}`", f"✅ Eşleşme: `{m.group()}`" if m else "❌ Eşleşme yok.", GREEN if m else RED))
    else:
        filters = await db.get_regex_filters(guild_id)
        if not filters:
            await ch.send(embed=embed(desc="📋 Henüz regex filtresi yok.", color=BLUE))
            return
        fields = [(f"`{f['name']}`", f"Desen: `{f['pattern']}`\nEylem: `{f['action']}`" + (f"\nUyarı: {f['warn_message']}" if f['warn_message'] else ""), False) for f in filters]
        await ch.send(embed=embed("🔎 Regex Filtreleri", color=BLUE, fields=fields))


# ─── X Monitor Commands ───────────────────────────────────────────────────────
async def _cmd_xmonitor(args, msg, guild_id, ch, prefix):
    sub = args[0].lower() if args else ""
    if sub in ("add", "ekle"):
        if len(args) < 3:
            await ch.send(embed=embed(desc="⚠️ Kullanım: `xmonitor add <kullanıcı> <kanal_id> [youtube kick twitch]`", color=YELLOW))
            return
        username = args[1].strip("@").lower()
        ch_id = re.sub(r"[<#>]", "", args[2])
        valid = {"youtube", "kick", "twitch"}
        platforms = [p.lower() for p in args[3:] if p.lower() in valid] or list(valid)
        await db.add_x_monitor(guild_id, username, ch_id, platforms)
        await ch.send(embed=embed("✅ X Monitor Eklendi", color=GREEN, fields=[
            ("Kullanıcı", f"@{username}", True),
            ("Kanal", f"<#{ch_id}>", True),
            ("Platformlar", ", ".join(platforms).title(), True),
        ], footer="Her 5 dakikada bir kontrol edilir."))
    elif sub in ("remove", "sil"):
        if len(args) < 2:
            return
        ok = await db.remove_x_monitor(guild_id, args[1])
        await ch.send(embed=embed(desc=f"{'✅' if ok else '❌'} @{args[1].strip('@')} {'kaldırıldı' if ok else 'bulunamadı'}.", color=GREEN if ok else RED))
    elif sub == "force":
        tmp = await ch.send(embed=embed(desc="🔄 X feed'leri kontrol ediliyor...", color=BLUE))
        await _check_x_monitors()
        await tmp.edit(embed=embed(desc="✅ X feed kontrolü tamamlandı.", color=GREEN))
    else:
        monitors = await db.get_x_monitors(guild_id)
        if not monitors:
            await ch.send(embed=embed(desc="📋 İzlenen X hesabı yok.", color=BLUE))
            return
        fields = [(f"@{m['username']}", f"📢 <#{m['target_channel_id']}>\n🎯 {', '.join(m['platforms']).title()}", False) for m in monitors]
        await ch.send(embed=embed("𝕏 İzlenen X Hesapları", color=TWITTER, fields=fields))


# ─── Timer Commands ───────────────────────────────────────────────────────────
async def _cmd_timer(args, msg, guild_id, ch, prefix):
    sub = args[0].lower() if args else ""
    if sub in ("add", "ekle"):
        if len(args) < 5:
            await ch.send(embed=embed(desc="⚠️ Kullanım: `timer add <isim> <aralık> <kanal_id> <mesaj>`\nÖrnek: `timer add duyuru 6h 123456789 Merhaba!`", color=YELLOW))
            return
        name, interval_str = args[1], args[2]
        ch_id = re.sub(r"[<#>]", "", args[3])
        message = " ".join(args[4:])
        secs = parse_duration(interval_str)
        if not secs or secs < 30:
            await ch.send(embed=embed(desc="❌ Geçersiz zaman. Örnekler: `30s` `5m` `2h` `1d`\nMinimum: `30s`", color=RED))
            return
        await db.add_timer(guild_id, ch_id, name, message, secs, int(time.time()) + secs)
        await ch.send(embed=embed("✅ Timer Eklendi", color=GREEN, fields=[
            ("İsim", f"`{name}`", True), ("Aralık", f"`{fmt_dur(secs)}`", True), ("Kanal", f"<#{ch_id}>", True),
            ("Mesaj", message[:300], False),
        ]))
    elif sub in ("remove", "sil"):
        if len(args) < 2:
            return
        ok = await db.remove_timer(guild_id, args[1])
        await ch.send(embed=embed(desc=f"{'✅' if ok else '❌'} `{args[1]}` {'kaldırıldı' if ok else 'bulunamadı'}.", color=GREEN if ok else RED))
    elif sub in ("pause", "durdur"):
        timers = await db.get_timers(guild_id)
        t = next((x for x in timers if x["name"] == (args[1] if len(args) > 1 else "")), None)
        if t:
            await db.set_timer_enabled(t["id"], False)
            await ch.send(embed=embed(desc=f"⏸️ `{args[1]}` duraklatıldı.", color=YELLOW))
    elif sub in ("resume", "devam"):
        timers = await db.get_timers(guild_id)
        t = next((x for x in timers if x["name"] == (args[1] if len(args) > 1 else "")), None)
        if t:
            await db.set_timer_enabled(t["id"], True)
            await db.update_timer_next_run(t["id"], int(time.time()) + t["interval_seconds"])
            await ch.send(embed=embed(desc=f"▶️ `{args[1]}` yeniden başlatıldı.", color=GREEN))
    elif sub == "now":
        timers = await db.get_timers(guild_id)
        t = next((x for x in timers if x["name"] == (args[1] if len(args) > 1 else "")), None)
        if t:
            tch = await bot.fetch_channel(t["channel_id"])
            await tch.send(embed=embed(desc=t["message"], color=BLUE, footer=f"⏰ {t['name']}"))
            await ch.send(embed=embed(desc=f"✅ `{args[1]}` mesajı <#{t['channel_id']}> kanalına gönderildi.", color=GREEN))
    else:
        timers = await db.get_timers(guild_id)
        if not timers:
            await ch.send(embed=embed(desc="⏰ Henüz timer yok.", color=BLUE))
            return
        now = int(time.time())
        fields = [
            (f"{'✅' if t['enabled'] else '⏸️'} `{t['name']}`",
             f"Kanal: <#{t['channel_id']}>\nAralık: `{fmt_dur(t['interval_seconds'])}`\nSonraki: `{fmt_dur(max(0,t['next_run']-now))} sonra`",
             False) for t in timers
        ]
        await ch.send(embed=embed("⏰ Timer Listesi", color=BLUE, fields=fields))


# ─── Background Tasks ─────────────────────────────────────────────────────────
async def _timer_loop():
    await asyncio.sleep(15)
    while True:
        try:
            now = int(time.time())
            for t in await db.get_timers():
                if t["enabled"] and t["next_run"] <= now:
                    try:
                        tch = await bot.fetch_channel(t["channel_id"])
                        await tch.send(embed=embed(desc=t["message"], color=BLUE, footer=f"⏰ {t['name']}"))
                        await db.update_timer_next_run(t["id"], now + t["interval_seconds"])
                        logger.info(f"[Timer:{t['name']}] gönderildi")
                    except Exception as ex:
                        logger.error(f"Timer '{t['name']}': {ex}")
        except Exception as ex:
            logger.error(f"Timer döngüsü: {ex}")
        await asyncio.sleep(15)


async def _x_monitor_loop():
    await asyncio.sleep(30)
    while True:
        try:
            await _check_x_monitors()
        except Exception as ex:
            logger.error(f"X monitor döngüsü: {ex}")
        await asyncio.sleep(300)


async def _check_x_monitors():
    global _nitter_idx
    session = await get_session()
    for m in await db.get_x_monitors():
        try:
            posts = await _fetch_rss(session, m["username"])
            if not posts:
                continue
            last = m["last_post_id"]
            new_posts = [p for p in posts if p["id"] != last]
            if not new_posts:
                continue
            await db.update_last_post_id(m["id"], posts[0]["id"])
            if last is None:
                continue
            for p in reversed(new_posts):
                txt = f"{p['title']} {p['content']}"
                for platform in m["platforms"]:
                    pat = PLATFORM_RE.get(platform)
                    if pat and (match := pat.search(txt)):
                        await _send_x_notif(m["target_channel_id"], m["username"], p, platform, match.group(0))
                        await asyncio.sleep(1)
            await asyncio.sleep(2)
        except Exception as ex:
            logger.error(f"X monitor [{m['username']}]: {ex}")


async def _fetch_rss(session, username) -> list:
    global _nitter_idx
    for _ in range(len(NITTER_INSTANCES)):
        base = NITTER_INSTANCES[_nitter_idx % len(NITTER_INSTANCES)]
        _nitter_idx += 1
        try:
            async with session.get(f"{base}/{username}/rss") as r:
                if r.status == 200:
                    return _parse_rss(await r.text())
        except Exception:
            continue
    return []


def _parse_rss(xml_text) -> list:
    posts = []
    try:
        root = ElementTree.fromstring(xml_text)
        ch = root.find("channel")
        if ch is None:
            return posts
        for item in ch.findall("item"):
            guid = item.findtext("guid") or ""
            m = re.search(r"/status/(\d+)", guid)
            posts.append({
                "id": m.group(1) if m else guid,
                "title": item.findtext("title") or "",
                "link": item.findtext("link") or "",
                "content": item.findtext("description") or "",
            })
    except ElementTree.ParseError:
        pass
    return posts


async def _send_x_notif(channel_id, username, post, platform, link):
    colors = {"youtube": 0xFF0000, "kick": 0x53FC18, "twitch": 0x9146FF}
    icons = {"youtube": "▶️", "kick": "🟢", "twitch": "🟣"}
    names = {"youtube": "YouTube", "kick": "Kick", "twitch": "Twitch"}
    fields = [("🔗 Link", link, False)]
    if post["link"]:
        x_link = re.sub(r"https?://[^/]+/", "https://x.com/", post["link"])
        fields.append(("𝕏 Tweet", x_link, False))
    try:
        tch = await bot.fetch_channel(channel_id)
        await tch.send(embed=embed(
            f"{icons.get(platform,'🔗')} Yeni {names.get(platform,platform)} — @{username}",
            (post["title"] or "")[:400],
            colors.get(platform, TWITTER),
            fields=fields,
            footer=f"X Monitor • @{username}",
        ))
    except Exception as ex:
        logger.error(f"X notif gönderim hatası: {ex}")


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.critical("BOT_TOKEN bulunamadı! Railway Variables'a ekle.")
        sys.exit(1)
    await db.initialize()
    logger.info("Fluxer'a bağlanılıyor...")
    await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
