"""
Telegram Master Multi-Forwarder (Multi-Account, Channel-Aware, Ultra-Fast Edition)
=================================================================================
Fixes & Features:
1. Multi-Account & In-Bot Login:
   - Connect ANY Telegram account directly inside @autforwbdbot via 1-tap Phone + Inline Numpad OTP
   - Safe inline OTP keypad avoids Telegram's plain-text OTP invalidation
   - Pre-loads default owner account from SESSION_STRING automatically
2. Channel & Group Command Support:
   - Commands (/start, /menu, /scan, /setsource, /settarget, /id) work inside Channels, Groups & DM
   - Auto-discovers any channel/group where @autforwbdbot is added or commanded
   - Automatically matches the commanding channel/user to the corresponding connected account
3. Instant & Reliable Group Scanner:
   - Scans BOTH main chat list and Archived folder (archived=False & archived=True)
   - Merges Userbot dialogs + Bot-discovered chats, filtering out deactivated/left/forbidden chats
   - Guarantees fresh visual update + toast feedback on every "নতুন গ্রুপ স্ক্যান" click
4. High-Speed Forwarding Engine:
   - Downloads protected media ONCE and cloud-forwards to remaining targets (up to 20x faster)
   - Preserves video attributes (resolution, duration, streaming) & thumbnails
   - Handles direct Bot uploads & cross-client fallbacks seamlessly
   - Marks anti-duplicate fingerprints ONLY after successful upload
   - Safe HTML formatting (eliminates Markdown crashes on special chars in group names)
"""

import os
import re
import io
import sys
import html
import json
import math
import time
import random
import hashlib
import struct
import shutil
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

try:
    import qrcode
except ImportError:
    qrcode = None

from telethon import TelegramClient, events, Button, utils, functions, types, helpers, errors as tl_errors
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.errors.rpcerrorlist import (
    MessageNotModifiedError,
    ChatForwardsRestrictedError,
    ChannelPrivateError,
    ChatWriteForbiddenError,
    PeerIdInvalidError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    UserAlreadyParticipantError,
)

from config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    OWNER_ID,
    BASE_DIR,
    LOG_FORMAT,
)
from utils import format_size, is_video_message, is_sticker_message, clean_chat_input, parse_telegram_link

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("MultiForwarder")

TEMP_DIR = BASE_DIR / "temp_media"
TEMP_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = BASE_DIR / "settings.json"
STATE_FILE = BASE_DIR / "bot_state.json"

DEFAULT_OWNER_ID = OWNER_ID or 8721309931
BST_TZ = timezone(timedelta(hours=6))

# ==================== CLEANUP & HELPERS ====================

def cleanup_temp_dir():
    """Remove leftover temporary files on startup"""
    try:
        for p in TEMP_DIR.iterdir():
            if p.is_file():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
    except Exception as e:
        logger.warning(f"Temp cleanup warning: {e}")

def h(text) -> str:
    """Escape text safely for Telegram HTML parse mode"""
    return html.escape(str(text or ""))

def now_bst_str() -> str:
    return datetime.now(BST_TZ).strftime("%I:%M:%S %p")

def to_canonical_id(chat_id) -> int:
    """Normalize any chat ID format (-100xxxx, -xxxx, xxxx) to plain positive int"""
    if not chat_id:
        return 0
    s = str(chat_id).strip()
    if s.startswith("-100"):
        return int(s[4:])
    if s.startswith("-"):
        return int(s[1:])
    return int(s)

def normalize_peer_id(entity, fallback_id: int = 0) -> int:
    """Get exact Telethon peer ID (-100xxx for channels, -xxx for groups)"""
    if entity is not None:
        try:
            return utils.get_peer_id(entity)
        except Exception:
            eid = getattr(entity, "id", 0)
            if isinstance(entity, (types.Channel, types.ChannelForbidden)):
                return utils.get_peer_id(types.PeerChannel(to_canonical_id(eid)))
            if isinstance(entity, (types.Chat, types.ChatForbidden)):
                return utils.get_peer_id(types.PeerChat(to_canonical_id(eid)))
            return int(eid or fallback_id)
    return int(fallback_id)

def sanitize_chat_id_value(cid: int) -> int:
    """Replace known migrated legacy group ID with its supergroup ID"""
    if int(cid) == -5522665152:
        return -1004402901248
    return int(cid)

# ==================== STATE MANAGEMENT ====================

def make_default_profile(owner_chat_id: int, account_id: int | None = None, seed_defaults: bool = False) -> dict:
    if seed_defaults:
        return {
            "active_account_id": account_id,
            "target_ids": [-1004402901248],
            "target_titles": {"-1004402901248": "🫦🫦🫦"},
            "source_ids": [-1004495534028],
            "source_titles": {"-1004495534028": "🍑🍑🍑🍑"},
            "caption_mode": "original",
            "media_filter": "video_only",
            "auto_forward_enabled": True,
            "duplicate_protection": True,
            "notify_on_forward": True,
            "seen_fingerprints": [],
            "forwarded_count": 13,
            "notify_chat_id": owner_chat_id,
        }
    return {
        "active_account_id": account_id,
        "target_ids": [],
        "target_titles": {},
        "source_ids": [],
        "source_titles": {},
        "caption_mode": "original",
        "media_filter": "video_only",
        "auto_forward_enabled": True,
        "duplicate_protection": True,
        "notify_on_forward": True,
        "seen_fingerprints": [],
        "forwarded_count": 0,
        "notify_chat_id": owner_chat_id,
    }

def load_state() -> dict:
    state = {"accounts": {}, "profiles": {}, "bot_known_chats": {}}

    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    state["accounts"] = loaded.get("accounts", {})
                    state["profiles"] = loaded.get("profiles", {})
                    state["bot_known_chats"] = loaded.get("bot_known_chats", {})
        except Exception as e:
            logger.error(f"Error loading bot_state.json: {e}")

    # One-time migration from legacy settings.json if owner profile not present
    owner_key = str(DEFAULT_OWNER_ID)
    if owner_key not in state["profiles"]:
        prof = make_default_profile(DEFAULT_OWNER_ID, account_id=DEFAULT_OWNER_ID, seed_defaults=True)
        if SETTINGS_FILE.exists():
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    old = json.load(f)
                    for k in ("caption_mode", "media_filter", "auto_forward_enabled", "duplicate_protection", "seen_fingerprints", "forwarded_count"):
                        if k in old:
                            prof[k] = old[k]
                    if old.get("target_ids"):
                        prof["target_ids"] = [sanitize_chat_id_value(x) for x in old["target_ids"]]
                        prof["target_titles"] = old.get("target_titles", prof["target_titles"])
                    if old.get("source_ids"):
                        prof["source_ids"] = [sanitize_chat_id_value(x) for x in old["source_ids"]]
                        prof["source_titles"] = old.get("source_titles", prof["source_titles"])
            except Exception as e:
                logger.warning(f"Legacy settings migration warning: {e}")
        state["profiles"][owner_key] = prof

    # Sanitize all loaded profiles
    for pk, prof in state["profiles"].items():
        prof.setdefault("active_account_id", None)
        prof.setdefault("target_ids", [])
        prof.setdefault("target_titles", {})
        prof.setdefault("source_ids", [])
        prof.setdefault("source_titles", {})
        prof.setdefault("caption_mode", "original")
        prof.setdefault("media_filter", "video_only")
        prof.setdefault("auto_forward_enabled", True)
        prof.setdefault("duplicate_protection", True)
        prof.setdefault("notify_on_forward", True)
        prof.setdefault("seen_fingerprints", [])
        prof.setdefault("forwarded_count", 0)
        prof.setdefault("notify_chat_id", int(pk) if pk.lstrip("-").isdigit() else DEFAULT_OWNER_ID)

        clean_tgts = []
        for tid in prof["target_ids"]:
            stid = sanitize_chat_id_value(tid)
            if stid not in clean_tgts:
                clean_tgts.append(stid)
        prof["target_ids"] = clean_tgts

    return state

def save_state():
    """Atomic state persistence prevents corruption on crash"""
    try:
        tmp_path = STATE_FILE.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(app_state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except Exception as e:
        logger.error(f"Error saving state: {e}")

app_state = load_state()

def get_profile(prof_key: str) -> dict:
    prof_key = str(prof_key)
    if prof_key not in app_state["profiles"]:
        chat_num = int(prof_key) if prof_key.lstrip("-").isdigit() else DEFAULT_OWNER_ID
        acc_id = chat_num if str(chat_num) in app_state["accounts"] else None
        app_state["profiles"][prof_key] = make_default_profile(chat_num, account_id=acc_id, seed_defaults=False)
        save_state()
    return app_state["profiles"][prof_key]

def record_bot_known_chat(chat, chat_id: int | None = None) -> bool:
    """Persist any group/channel where @autforwbdbot was added or commanded. Returns True if changed."""
    if chat is None and not chat_id:
        return False
    if isinstance(chat, types.User):
        return False
    peer_id = normalize_peer_id(chat, chat_id or 0)
    if not peer_id or peer_id > 0:
        return False
    if getattr(chat, "deactivated", False) or getattr(chat, "left", False):
        return False
    if type(chat).__name__.endswith("Forbidden"):
        return False

    title = (getattr(chat, "title", None) or getattr(chat, "username", None) or str(peer_id)).strip()[:64]
    username = getattr(chat, "username", None)
    ctype = "channel" if isinstance(chat, types.Channel) and not getattr(chat, "megagroup", False) else "group"
    existing = app_state["bot_known_chats"].get(str(peer_id))
    if existing and existing.get("title") == title and existing.get("type") == ctype:
        return False

    app_state["bot_known_chats"][str(peer_id)] = {
        "id": peer_id,
        "title": title,
        "username": username,
        "type": ctype,
        "updated_at": now_bst_str(),
    }
    return True

# ==================== CLIENTS INITIALIZATION ====================

SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
if not SESSION_STRING:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
    SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

BOT_SESSION_STRING = os.getenv(
    "BOT_SESSION_STRING",
    "1BVtsOHEBu4SgSeGUhFYOYVSIjlZB2m_DrLe39cVmVULZcfRMcyoNjWq6BnomwbtrDY8crG4_kFj9nmE3zgOMlESW5zJoop1d6SppNKYR4Odjnf6xVGGM1086cPuVNeSvYPW0yT6h2tpBkv0uNsYJtwlp1qC8ob-0mKruMwX6y2vY_LPHSop8wXzuvP58VYpbnYjZqcyi2AKzuwrEIb926-ravhUQ_BsB34cLjWoz7xt4wkaRLlhmb2YsaMTVSsxpqZQ2QnsJ4jfCAxelHXXx7Wme3d_dZnNAsXGAlEplRHYII2VCRenyVU1XydwTI7S0CxWuIPVPU6hKGZLdDr8UgNYg8dEP9to="
).strip()

bot_client = TelegramClient(StringSession(BOT_SESSION_STRING), API_ID, API_HASH)
bot_client.parse_mode = "html"

# Active connected Userbot clients: account_uid (int) -> TelegramClient
user_clients: dict[int, TelegramClient] = {}
# Per-account caches: account_uid (int) -> {"list": [...], "last_fetched": float, "last_scan": str, "lock": asyncio.Lock}
dialog_caches: dict[int, dict] = {}
# Per-account entity cache: (client_key, peer_or_canonical_id) -> entity
entity_caches: dict[tuple, object] = {}
# Bot-only chat cache timestamp
bot_scan_meta = {"last_scan": now_bst_str()}
# Pending in-bot login states: sender_id (int) -> dict
pending_logins: dict[int, dict] = {}
# Pending & active Link Hunter scans
pending_link_hunts: dict[int, dict] = {}
active_link_hunts: dict[str, dict] = {}
# Per-profile active bulk sync tracking
active_syncs: dict[str, dict] = {}
# Deduplicate dual delivery when both Userbot & Bot see the same source post
recent_dispatched: dict[tuple[str, int, int], float] = {}
# Cache canonical IDs of source chats that forbid direct cloud forwarding (avoids wasted RPC per video)
restricted_source_chats: set[int] = set()
# Per-account FloodWait unlock epoch timestamp & adaptive batch spacing delay
account_flood_until: dict[int, float] = {}
account_pace_delay: dict[int, float] = {}

async def wait_out_flood(
    acc_id: int | None,
    seconds: int,
    *,
    reply_chat_id: int | None = None,
    status_msg_id: int | None = None,
    prof_key: str = "",
    copied: int = 0,
    should_stop_fn=None,
):
    """
    Waits out Telegram FloodWait safely in cancelable 2s steps, bumps adaptive pacing,
    and updates the Telegram UI live with exact Bangladesh unlock clock & switch-account button.
    """
    now_ts = time.time()
    target_ts = now_ts + max(1, seconds) + 2
    if acc_id:
        account_flood_until[acc_id] = max(account_flood_until.get(acc_id, 0.0), target_ts)
        cur_pace = account_pace_delay.get(acc_id, 1.5)
        account_pace_delay[acc_id] = min(max(cur_pace * 1.5, 2.8), 6.0)

    last_ui = 0.0
    while True:
        if should_stop_fn and should_stop_fn():
            return
        rem = int(target_ts - time.time())
        if rem <= 0:
            break

        now_m = time.monotonic()
        if reply_chat_id and status_msg_id and (now_m - last_ui >= (45.0 if rem > 90 else 15.0)):
            last_ui = now_m
            unlock_dt = datetime.fromtimestamp(target_ts, tz=BST_TZ).strftime("%I:%M %p")
            mins, secs = divmod(rem, 60)
            dur_txt = f"{mins} মিনিট {secs} সেকেন্ড" if mins else f"{secs} সেকেন্ড"
            btns = [[Button.inline("🛑 সিঙ্ক বন্ধ করুন (Stop)", f"stop:{prof_key}".encode())]]
            if prof_key:
                btns.append([Button.inline("👤 অন্য অ্যাকাউন্ট দিয়ে চালাতে চাপুন", f"acc:{prof_key}".encode())])
            try:
                await bot_client.edit_message(
                    reply_chat_id,
                    status_msg_id,
                    f"⏳ <b>টেলিগ্রাম স্প্যাম-গার্ড বিরতি (অটো চলবে)</b>\n\n"
                    f"📥 একটানা দ্রুত ফরোয়ার্ডের কারণে টেলিগ্রাম সার্ভার এই অ্যাকাউন্টটিকে সাময়িক বিরতি দিয়েছে।\n"
                    f"🕒 <b>বাকি সময়:</b> <code>{dur_txt}</code> (অটো চালু হবে: <b>{unlock_dt}</b>)\n"
                    f"✅ এখন পর্যন্ত ফরোয়ার্ড হয়েছে: <code>{copied}</code> টি ভিডিও\n\n"
                    f"💡 <i>বট বন্ধ হয়নি—সময় শেষ হলেই ঠিক এখান থেকেই আবার শুরু হবে! চাইলে নিচ থেকে অন্য অ্যাকাউন্ট ব্যবহার করতে পারেন।</i>",
                    buttons=btns,
                )
            except MessageNotModifiedError:
                pass
            except Exception:
                pass

        await asyncio.sleep(min(2.0, max(0.2, target_ts - time.time())))

def get_active_sync(prof_key: str) -> dict:
    prof_key = str(prof_key)
    if prof_key not in active_syncs:
        active_syncs[prof_key] = {
            "running": False,
            "should_stop": False,
            "copied": 0,
            "scanned": 0,
            "task": None,
            "chat_id": None,
            "msg_id": None,
        }
    return active_syncs[prof_key]

def trigger_sync_stop(prof_key: str, sender_id: int | None = None, chat_id: int | None = None) -> tuple[bool, str]:
    """Immediately signals and cancels an active bulk sync task for prof_key or matching user/chat."""
    candidates = [str(prof_key)]
    if sender_id is not None:
        candidates.append(str(sender_id))
    if chat_id is not None:
        candidates.append(str(chat_id))
    for k, st in list(active_syncs.items()):
        if st.get("running") and k not in candidates:
            candidates.append(k)

    for k in candidates:
        st = active_syncs.get(k)
        if not st or not st.get("running"):
            continue
        if k != str(prof_key) and sender_id is not None:
            prof = app_state.get("profiles", {}).get(k, {})
            if str(prof.get("active_account_id")) != str(sender_id) and str(st.get("chat_id")) != str(chat_id):
                continue
        st["should_stop"] = True
        task = st.get("task")
        if task and not task.done():
            task.cancel()
        logger.info(f"🛑 Stop triggered immediately for profile [{k}] (by {sender_id})")
        return True, k
    return False, str(prof_key)

# ==================== MEDIA FILTER & FINGERPRINT ====================

def is_eligible_media(message, profile: dict) -> bool:
    if not message or not getattr(message, "media", None):
        return False
    # Always skip Telegram stickers (.webm / .tgs / .webp / custom emoji) in ALL modes
    if is_sticker_message(message):
        return False
    m_filter = profile.get("media_filter", "video_only")
    if m_filter == "all_media":
        return True
    # Filter reaction GIFs / tiny silent animated loops (<500 KB) when video_only
    if getattr(message, "gif", None):
        fsize = getattr(message.file, "size", 0) if message.file else 0
        if fsize and fsize < 500_000:
            return False
    return is_video_message(message)

def get_media_fingerprint(message) -> str:
    if not message or not message.media:
        return ""
    if getattr(message, "document", None):
        doc = message.document
        for attr in getattr(doc, "attributes", []):
            if hasattr(attr, "duration") and hasattr(attr, "w"):
                return f"vid_{doc.size}_{attr.duration}_{attr.w}_{attr.h}"
        return f"doc_{doc.id}_{doc.size}"
    if getattr(message, "video", None):
        v = message.video
        return f"v_{v.id}_{v.size}"
    if getattr(message, "photo", None):
        p = message.photo
        return f"ph_{p.id}"
    if getattr(message, "file", None):
        return f"f_{message.file.size}_{getattr(message.file, 'name', '')}"
    return ""

def safe_caption_and_entities(message, caption_mode: str):
    """Extract caption within Telegram's 1024-char limit preserving formatting entities safely"""
    if caption_mode != "original":
        return None, None
    raw = (getattr(message, "message", None) or "").strip()
    if not raw:
        return None, None
    entities = getattr(message, "entities", None)
    if len(raw) <= 1020:
        return raw, entities
    truncated = raw[:1017] + "..."
    valid_entities = None
    if entities:
        valid_entities = [e for e in entities if (e.offset + e.length) <= 1017]
    return truncated, valid_entities or None

def extract_mp4_metadata(filepath: Path) -> tuple[int, int, float]:
    """Zero-RAM seek-based MP4 mvhd/tkhd parser returning exact (width, height, duration) including rotation."""
    try:
        file_size = filepath.stat().st_size
        duration = 0.0
        tracks = []

        def walk(f, start: int, end: int):
            nonlocal duration
            pos = start
            while pos + 8 <= end:
                f.seek(pos)
                hdr_bytes = f.read(8)
                if len(hdr_bytes) < 8:
                    break
                size, btype = struct.unpack(">I4s", hdr_bytes)
                hdr = 8
                if size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    size = struct.unpack(">Q", ext)[0]
                    hdr = 16
                elif size == 0:
                    size = end - pos
                if size < hdr or pos + size > end:
                    break
                body_start, body_end = pos + hdr, pos + size
                if btype in (b"moov", b"trak"):
                    walk(f, body_start, body_end)
                elif btype == b"mvhd" and body_end - body_start >= 20:
                    f.seek(body_start)
                    buf = f.read(min(36, body_end - body_start))
                    ver = buf[0]
                    if ver == 0 and len(buf) >= 20:
                        ts, dur = struct.unpack(">II", buf[12:20])
                        if ts:
                            duration = max(duration, dur / ts)
                    elif ver == 1 and len(buf) >= 32:
                        ts, dur = struct.unpack(">IQ", buf[20:32])
                        if ts:
                            duration = max(duration, dur / ts)
                elif btype == b"tkhd" and body_end - body_start >= 84:
                    f.seek(body_start)
                    buf = f.read(min(100, body_end - body_start))
                    ver = buf[0]
                    off = 88 if ver == 1 else 76
                    mat_off = 44 if ver == 1 else 32
                    if len(buf) >= off + 8:
                        a, b, _, c, d = struct.unpack(">iiiii", buf[mat_off:mat_off + 20])
                        w_raw, h_raw = struct.unpack(">II", buf[off:off + 8])
                        w = int(round(w_raw / 65536.0))
                        h = int(round(h_raw / 65536.0))
                        if w > 1 and h > 1:
                            angle = abs(round(math.degrees(math.atan2(b / 65536.0, a / 65536.0))))
                            if angle in (90, 270):
                                w, h = h, w
                            tracks.append((w, h))
                pos += size

        with open(filepath, "rb") as f:
            walk(f, 0, file_size)
        if tracks:
            return tracks[0][0], tracks[0][1], duration
    except Exception as e:
        logger.debug(f"MP4 box parse fallback: {e}")

    # Optional hachoir fallback for MKV / WebM / AVI
    try:
        from hachoir.parser import createParser
        from hachoir.metadata import extractMetadata
        parser = createParser(str(filepath))
        if parser:
            with parser:
                meta = extractMetadata(parser)
                if meta:
                    w = meta.get("width") if meta.has("width") else 0
                    h = meta.get("height") if meta.has("height") else 0
                    dur = meta.get("duration").total_seconds() if meta.has("duration") else 0.0
                    if w > 1 and h > 1:
                        return int(w), int(h), float(dur)
    except Exception:
        pass
    return 0, 0, 0.0

def build_video_attributes(message, downloaded_path: Path | None = None):
    """
    Constructs exact non-zoomed video attributes preserving original aspect ratio and duration.
    Prevents Telethon's 1x1 square zoom bug completely.
    """
    orig_w, orig_h, orig_dur = 0, 0, 0.0
    round_msg = False
    nosound = False
    orig_name = None

    if getattr(message, "document", None) and getattr(message.document, "attributes", None):
        for a in message.document.attributes:
            if isinstance(a, types.DocumentAttributeVideo):
                orig_w = getattr(a, "w", 0) or 0
                orig_h = getattr(a, "h", 0) or 0
                orig_dur = float(getattr(a, "duration", 0) or 0)
                round_msg = bool(getattr(a, "round_message", False))
                nosound = bool(getattr(a, "nosound", False))
            elif isinstance(a, types.DocumentAttributeFilename):
                orig_name = getattr(a, "file_name", None)

    final_w, final_h, final_dur = orig_w, orig_h, orig_dur
    # If source metadata was invalid (e.g. 1x1 or missing) and we have the file, measure real native dimensions!
    if (final_w <= 16 or final_h <= 16 or final_dur <= 0) and downloaded_path and downloaded_path.exists():
        rw, rh, rdur = extract_mp4_metadata(downloaded_path)
        if rw > 16 and rh > 16 and (final_w <= 16 or final_h <= 16):
            final_w, final_h = rw, rh
        if rdur > 0 and final_dur <= 0:
            final_dur = rdur

    attrs = []
    if final_w > 16 and final_h > 16:
        attrs.append(types.DocumentAttributeVideo(
            duration=final_dur,
            w=final_w,
            h=final_h,
            round_message=round_msg,
            supports_streaming=True,
            nosound=nosound,
        ))
    if orig_name:
        attrs.append(types.DocumentAttributeFilename(file_name=orig_name))
    elif downloaded_path:
        attrs.append(types.DocumentAttributeFilename(file_name=downloaded_path.name))
    return attrs or None, (final_w, final_h)

# ==================== ENTITY & DIALOG RESOLUTION ====================

async def get_entity_safe(client: TelegramClient, chat_id: int, client_key: str = "default"):
    if not chat_id or client is None:
        return None
    ck = (client_key, chat_id)
    if ck in entity_caches:
        return entity_caches[ck]

    try:
        entity = await client.get_entity(chat_id)
        if getattr(entity, "migrated_to", None):
            entity = await client.get_entity(entity.migrated_to)
        if getattr(entity, "deactivated", False) or type(entity).__name__.endswith("Forbidden"):
            return None
        pid = normalize_peer_id(entity, chat_id)
        entity_caches[(client_key, chat_id)] = entity
        entity_caches[(client_key, pid)] = entity
        entity_caches[(client_key, to_canonical_id(pid))] = entity
        return entity
    except Exception as e:
        logger.debug(f"Entity resolution failed on {client_key} for {chat_id}: {e}")
        return None

async def scan_account_dialogs(account_id: int | None, force_refresh: bool = False) -> tuple[list[dict], str]:
    """
    Scans ALL dialogs (Main + Archived) of account_id AND merges bot_known_chats.
    Returns (items_list, scan_time_str). Each item: {"id": int, "title": str, "source": "userbot"|"bot", "type": str}.
    """
    merged: dict[int, dict] = {}
    scan_time = now_bst_str()

    if account_id and account_id in user_clients:
        cache = dialog_caches.setdefault(
            account_id,
            {"list": [], "last_fetched": 0.0, "last_scan": scan_time, "lock": asyncio.Lock()},
        )
        now = time.monotonic()

        if force_refresh or not cache["list"] or (now - cache["last_fetched"] >= 900):
            async with cache["lock"]:
                now2 = time.monotonic()
                # Coalesce rapid double-taps (< 3s apart) without redundant MTProto RPCs
                if (cache["list"] and (now2 - cache["last_fetched"] < 3.0)) or (
                    not force_refresh and cache["list"] and (now2 - cache["last_fetched"] < 900)
                ):
                    pass
                else:
                    client = user_clients[account_id]
                    logger.info(f"🔄 Scanning dialogs (main + archived) for account {account_id}...")
                    scanned_items = []
                    seen_canon = set()

                    for is_arch in (False, True):
                        try:
                            async for d in client.iter_dialogs(limit=None, archived=is_arch):
                                ent = d.entity
                                if getattr(ent, "deactivated", False) or getattr(ent, "left", False):
                                    continue
                                if type(ent).__name__.endswith("Forbidden"):
                                    continue
                                if d.is_channel or d.is_group:
                                    pid = normalize_peer_id(ent, d.id)
                                    canon = to_canonical_id(pid)
                                    if canon in seen_canon:
                                        continue
                                    seen_canon.add(canon)
                                    title = (d.name or getattr(ent, "title", None) or str(pid)).strip()
                                    ctype = "channel" if d.is_channel and not d.is_group else "group"
                                    scanned_items.append({
                                        "id": pid,
                                        "title": title,
                                        "source": "arch" if is_arch else "userbot",
                                        "type": ctype,
                                    })
                                    ck = f"u_{account_id}"
                                    entity_caches[(ck, pid)] = ent
                                    entity_caches[(ck, canon)] = ent
                        except Exception as e:
                            logger.warning(f"Dialog scan warning (archived={is_arch}) for {account_id}: {e}")

                    cache["list"] = scanned_items
                    cache["last_fetched"] = time.monotonic()
                    cache["last_scan"] = now_bst_str()
                    logger.info(f"⚡ Cached {len(scanned_items)} dialogs for account {account_id}.")

        for item in cache["list"]:
            merged[to_canonical_id(item["id"])] = item
        scan_time = cache["last_scan"]
    else:
        if force_refresh:
            bot_scan_meta["last_scan"] = now_bst_str()
        scan_time = bot_scan_meta["last_scan"]

    # Always merge bot-discovered chats so channels/groups where bot was added or commanded appear immediately
    for pid_str, binfo in app_state.get("bot_known_chats", {}).items():
        try:
            pid = int(binfo["id"])
            canon = to_canonical_id(pid)
            if canon not in merged:
                merged[canon] = {
                    "id": pid,
                    "title": binfo.get("title", str(pid)),
                    "source": "bot",
                    "type": binfo.get("type", "group"),
                }
        except Exception:
            pass

    return list(merged.values()), scan_time

async def find_account_for_chat(chat_id: int, sender_id: int | None = None) -> int | None:
    """Resolve which connected Userbot account matches the sender or the channel/group (preferring creator/admin)."""
    if sender_id and sender_id in user_clients:
        return sender_id
    canon = to_canonical_id(chat_id)
    fallback_member_uid = None
    for uid, uc in user_clients.items():
        try:
            ent = await uc.get_entity(chat_id)
            if ent and not getattr(ent, "left", False) and not getattr(ent, "deactivated", False):
                if getattr(ent, "creator", False) or getattr(ent, "admin_rights", None) is not None:
                    return uid
                if fallback_member_uid is None:
                    fallback_member_uid = uid
                continue
        except Exception:
            pass
        cache = dialog_caches.get(uid)
        if fallback_member_uid is None and cache and any(to_canonical_id(item["id"]) == canon for item in cache.get("list", [])):
            fallback_member_uid = uid
    return fallback_member_uid

async def resolve_profile_key_from_chat(event) -> tuple[str, int | None]:
    """
    Determines the profile key and matching account ID for an incoming command in DM, Group, or Channel.
    """
    sender_id = getattr(event, "sender_id", None)
    chat_id = event.chat_id

    if event.is_private:
        uid = sender_id or chat_id
        pk = str(uid)
        prof = get_profile(pk)
        # Ensure connected userbot links automatically
        if uid in user_clients and not prof.get("active_account_id"):
            prof["active_account_id"] = uid
            save_state()
        return pk, prof.get("active_account_id")

    # Inside Group or Channel: register the chat into bot_known_chats
    try:
        chat = await event.get_chat()
        record_bot_known_chat(chat, chat_id)
    except Exception:
        pass

    matched_uid = await find_account_for_chat(chat_id, sender_id if (sender_id and sender_id > 0) else None)
    if matched_uid:
        pk = str(matched_uid)
        prof = get_profile(pk)
        if not prof.get("active_account_id"):
            prof["active_account_id"] = matched_uid
            save_state()
        return pk, prof.get("active_account_id")

    # Channel/Group standalone bot profile if no Userbot is connected for it yet
    pk = str(chat_id)
    prof = get_profile(pk)
    save_state()
    return pk, prof.get("active_account_id")

# ==================== SMART HYBRID VIDEO FORWARDING ENGINE ====================

FAST_CHUNK_SIZE = 512 * 1024  # 512 KB (maximum Telegram MTProto chunk size)
TRANSFER_CONCURRENCY = 4      # Balanced 4x512KB=2MB pipeline per file (prevents DC4 timeouts)
BULK_BATCH_SIZE = 15          # Safe batch size per ForwardMessagesRequest to prevent 1h flood bans

def _record_fingerprint_and_count(profile: dict, fp: str, count: int = 1, defer_save: bool = False):
    """Records seen fingerprint and bumps forwarded count safely after confirmed send."""
    if profile.get("duplicate_protection", True) and fp:
        seen_list = profile.get("seen_fingerprints", [])
        if fp not in seen_list:
            seen_list.append(fp)
            if len(seen_list) > 25000:
                seen_list = seen_list[-20000:]
            profile["seen_fingerprints"] = seen_list
    profile["forwarded_count"] = profile.get("forwarded_count", 0) + count
    if not defer_save:
        save_state()

def _cleanup_payload(payload: dict | None):
    if not payload:
        return
    for key in ("downloaded_path", "thumb_path"):
        p = payload.get(key)
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

async def fast_download_video(
    client: TelegramClient,
    message,
    dest_path: Path,
    concurrency: int = TRANSFER_CONCURRENCY,
    source_peer=None,
) -> Path | None:
    """
    Pipelined 512 KB chunk downloader directly streaming to disk (<4 MB RAM).
    Automatically handles cross-DC borrowing, file_reference refresh & resilient retries.
    """
    doc = getattr(message, "document", None)
    file_size = getattr(doc, "size", 0) if doc else 0
    if not doc or file_size < FAST_CHUNK_SIZE:
        dl = await client.download_media(message, file=str(dest_path))
        return Path(dl) if dl and os.path.exists(dl) else None

    loc = types.InputDocumentFileLocation(doc.id, doc.access_hash, doc.file_reference, "")
    state = {"dc_id": doc.dc_id, "sender": None, "exported": False}
    state_lock = asyncio.Lock()
    sem = asyncio.Semaphore(concurrency)
    offsets = list(range(0, file_size, FAST_CHUNK_SIZE))
    fh = None
    ref_target = source_peer or getattr(message, "chat_id", None) or getattr(message, "peer_id", None)

    async def ensure_sender(force_reborrow: bool = False):
        async with state_lock:
            if force_reborrow and state["exported"] and state["sender"]:
                try:
                    await client._return_exported_sender(state["sender"])
                except Exception:
                    pass
                state["sender"] = None
                state["exported"] = False
            if state["dc_id"] and state["dc_id"] != client.session.dc_id:
                if state["sender"] is None or (hasattr(state["sender"], "is_connected") and not state["sender"].is_connected()):
                    try:
                        state["sender"] = await client._borrow_exported_sender(state["dc_id"])
                        state["exported"] = True
                    except tl_errors.DcIdInvalidError:
                        state["sender"] = client._sender
                        state["exported"] = False
            else:
                state["sender"] = client._sender
                state["exported"] = False

    try:
        await ensure_sender()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(dest_path, "wb+")
        fh.truncate(file_size)

        async def fetch_chunk(off: int):
            async with sem:
                for attempt in range(4):
                    try:
                        if state["sender"] is None or (hasattr(state["sender"], "is_connected") and not state["sender"].is_connected()):
                            await ensure_sender(force_reborrow=True)
                        req = functions.upload.GetFileRequest(loc, offset=off, limit=FAST_CHUNK_SIZE)
                        res = await client._call(state["sender"], req)
                        if isinstance(res, types.upload.FileCdnRedirect):
                            raise RuntimeError("CDN redirect")
                        data = res.bytes
                        fh.seek(off)
                        fh.write(data)
                        return
                    except (tl_errors.FileReferenceExpiredError, tl_errors.FilerefUpgradeNeededError):
                        if not ref_target:
                            raise
                        async with state_lock:
                            fresh = await client.get_messages(ref_target, ids=message.id)
                            if fresh and getattr(fresh, "document", None):
                                loc.file_reference = fresh.document.file_reference
                            else:
                                raise
                    except tl_errors.FileMigrateError as fm:
                        async with state_lock:
                            if state["dc_id"] != fm.new_dc:
                                if state["exported"] and state["sender"]:
                                    try:
                                        await client._return_exported_sender(state["sender"])
                                    except Exception:
                                        pass
                                state["dc_id"] = fm.new_dc
                                state["sender"] = await client._borrow_exported_sender(fm.new_dc)
                                state["exported"] = True
                    except FloodWaitError as fl:
                        wait_s = min(fl.seconds + 1, 60)
                        logger.warning(f"Download FloodWait ({fl.seconds}s) -> sleeping {wait_s}s")
                        await asyncio.sleep(wait_s)
                    except (asyncio.TimeoutError, tl_errors.TimedOutError, ConnectionError, OSError, ValueError) as net_err:
                        if attempt == 3:
                            raise
                        await asyncio.sleep(1.0 + attempt)
                        if state["exported"]:
                            await ensure_sender(force_reborrow=True)

        await asyncio.gather(*(fetch_chunk(off) for off in offsets))
        fh.flush()
        fh.close()
        fh = None
        if dest_path.exists() and dest_path.stat().st_size == file_size:
            return dest_path
        raise RuntimeError(f"Incomplete size {dest_path.stat().st_size if dest_path.exists() else 0} != {file_size}")
    except Exception as e:
        logger.warning(f"fast_download_video fallback (msg={message.id}, dc={getattr(doc, 'dc_id', None)}): {e}")
        try:
            if fh is not None:
                fh.close()
                fh = None
            dest_path.unlink(missing_ok=True)
        except Exception:
            pass
        cur_msg = message
        for dl_try in range(2):
            try:
                dl = await client.download_media(cur_msg, file=str(dest_path))
                if dl and os.path.exists(dl):
                    return Path(dl)
            except (tl_errors.FileReferenceExpiredError, tl_errors.FilerefUpgradeNeededError):
                if ref_target:
                    fresh = await client.get_messages(ref_target, ids=message.id)
                    if fresh:
                        cur_msg = fresh
                        continue
            except Exception as fb_err:
                logger.warning(f"Fallback download retry {dl_try + 1} failed for msg {message.id}: {fb_err}")
                if dl_try == 0:
                    await asyncio.sleep(3)
                    if ref_target:
                        try:
                            cur_msg = await client.get_messages(ref_target, ids=message.id) or cur_msg
                        except Exception:
                            pass
        return None
    finally:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        if state["exported"] and state["sender"]:
            try:
                await client._return_exported_sender(state["sender"])
            except Exception:
                pass

async def fast_upload_file(client: TelegramClient, filepath: Path, file_name: str | None = None, concurrency: int = TRANSFER_CONCURRENCY):
    """
    Pipelined 512 KB chunk uploader (~6x-9x faster than client.upload_file, <5 MB RAM).
    Returns InputFile / InputFileBig directly usable with send_file.
    """
    file_size = filepath.stat().st_size
    fname = file_name or filepath.name
    if file_size < FAST_CHUNK_SIZE:
        return str(filepath)

    part_count = (file_size + FAST_CHUNK_SIZE - 1) // FAST_CHUNK_SIZE
    is_big = file_size > 10 * 1024 * 1024
    file_id = helpers.generate_random_long()

    md5_hex = ""
    if not is_big:
        md5_h = hashlib.md5()
        with open(filepath, "rb") as mf:
            while True:
                blk = mf.read(FAST_CHUNK_SIZE)
                if not blk:
                    break
                md5_h.update(blk)
        md5_hex = md5_h.hexdigest()

    sem = asyncio.Semaphore(concurrency)
    fh = open(filepath, "rb")
    try:
        async def upload_part(idx: int):
            async with sem:
                fh.seek(idx * FAST_CHUNK_SIZE)
                chunk = fh.read(FAST_CHUNK_SIZE)
                req = (
                    functions.upload.SaveBigFilePartRequest(file_id, idx, part_count, chunk)
                    if is_big
                    else functions.upload.SaveFilePartRequest(file_id, idx, chunk)
                )
                for attempt in range(4):
                    try:
                        ok = await client(req)
                        if ok:
                            return
                        raise RuntimeError(f"Telegram rejected part {idx}/{part_count}")
                    except FloodWaitError as fl:
                        wait_s = fl.seconds + 1
                        logger.warning(f"Upload FloodWait ({fl.seconds}s) -> waiting {wait_s}s cleanly...")
                        await wait_out_flood(None, wait_s)
                    except (asyncio.TimeoutError, tl_errors.TimedOutError, ConnectionError):
                        if attempt == 3:
                            raise
                        await asyncio.sleep(0.5)

        await asyncio.gather(*(upload_part(i) for i in range(part_count)))
    finally:
        fh.close()

    if is_big:
        return types.InputFileBig(file_id, part_count, fname)
    return types.InputFile(file_id, part_count, fname, md5_hex)

async def upload_file_with_retry(
    upload_client: TelegramClient,
    target_entity,
    file_obj,
    caption,
    entities,
    attributes=None,
    thumb=None,
    mime_type=None,
    flood_cb=None,
):
    """Send file with pipelined 512KB upload for local files & full FloodWait retry loop."""
    send_file_obj = file_obj
    if isinstance(file_obj, (str, Path)):
        fp = Path(file_obj)
        if fp.exists() and fp.stat().st_size >= FAST_CHUNK_SIZE and (attributes or not utils.is_image(str(fp))):
            send_file_obj = await fast_upload_file(upload_client, fp, fp.name)

    for attempt in range(4):
        try:
            kwargs = {
                "entity": target_entity,
                "file": send_file_obj,
                "caption": caption,
                "formatting_entities": entities,
                "supports_streaming": True,
            }
            if attributes:
                kwargs["attributes"] = attributes
            if thumb and os.path.exists(thumb):
                kwargs["thumb"] = thumb
            if mime_type:
                kwargs["mime_type"] = mime_type
            return await upload_client.send_file(**kwargs)
        except FloodWaitError as flood:
            logger.warning(f"SendMedia FloodWait ({flood.seconds}s): pausing before retry...")
            if flood_cb:
                await flood_cb(flood.seconds)
            else:
                await wait_out_flood(None, flood.seconds)
            if attempt == 3:
                raise
    return None

async def prepare_video_payload(source_client: TelegramClient, message, src_canon: int, source_peer=None) -> dict | None:
    """Downloads video (512KB pipelined) + optional thumb concurrently and extracts native frame attributes."""
    ext = (getattr(message.file, "ext", None) or ".mp4") if message.file else ".mp4"
    uid_tag = helpers.generate_random_long() & 0xFFFF
    temp_video_path = TEMP_DIR / f"fwd_{src_canon}_{message.id}_{uid_tag}{ext}"
    temp_thumb_path = TEMP_DIR / f"thumb_{src_canon}_{message.id}_{uid_tag}.jpg"

    _, (src_w, src_h) = build_video_attributes(message, None)
    is_vid = is_video_message(message)
    source_has_broken_frame = bool(is_vid and getattr(message, "document", None) and (src_w <= 16 or src_h <= 16))
    has_thumb = bool(not source_has_broken_frame and getattr(message, "document", None) and getattr(message.document, "thumbs", None))

    async def dl_thumb():
        if not has_thumb:
            return None
        try:
            th = await source_client.download_media(message, file=str(temp_thumb_path), thumb=-1)
            return th if th and os.path.exists(th) else None
        except Exception:
            return None

    dl_res, thumb_res = await asyncio.gather(
        fast_download_video(source_client, message, temp_video_path, source_peer=source_peer),
        dl_thumb(),
    )
    if not dl_res or not Path(dl_res).exists():
        try:
            if thumb_res and os.path.exists(thumb_res):
                os.remove(thumb_res)
        except Exception:
            pass
        return None

    video_attrs, (real_w, real_h) = build_video_attributes(message, Path(dl_res))
    if is_vid:
        logger.info(f"📐 Verified original video frame: {real_w}x{real_h}")
    doc = getattr(message, "document", None)
    mime_type = getattr(doc, "mime_type", None) or (getattr(message.file, "mime_type", None) if message.file else None)
    return {
        "downloaded_path": str(dl_res),
        "thumb_path": thumb_res,
        "video_attrs": video_attrs,
        "mime_type": mime_type,
    }

async def distribute_video(
    source_client: TelegramClient,
    message,
    source_chat_id: int,
    source_title: str,
    prof_key: str,
    send_notification: bool = True,
    preloaded: dict | None = None,
    target_ids_override: list[int] | None = None,
    defer_save: bool = False,
    flood_cb=None,
) -> list[str]:
    """
    Ultra-fast hybrid distributor:
    - Caches restricted source chats to eliminate repeated failed cloud probes
    - Uses preloaded payload from background prefetcher when available
    - Uploads once to Target #1 (512KB pipelined) and fans out cloud media concurrently to all remaining targets
    """
    profile = get_profile(prof_key)
    if not message or not is_eligible_media(message, profile):
        _cleanup_payload(preloaded)
        return []

    fp = get_media_fingerprint(message)
    if profile.get("duplicate_protection", True) and fp and target_ids_override is None:
        if fp in set(profile.get("seen_fingerprints", [])):
            logger.info(f"🛡️ [{prof_key}] Duplicate detected ({fp}). Skipping!")
            _cleanup_payload(preloaded)
            return []

    src_canon = to_canonical_id(source_chat_id)
    raw_targets = target_ids_override if target_ids_override is not None else profile.get("target_ids", [])
    target_ids = [tid for tid in raw_targets if to_canonical_id(tid) != src_canon]

    if not target_ids:
        logger.warning(f"[{prof_key}] No valid target groups configured (or all match source)!")
        _cleanup_payload(preloaded)
        return []

    caption_text, caption_entities = safe_caption_and_entities(
        message, profile.get("caption_mode", "original")
    )

    acc_id = profile.get("active_account_id")
    primary_client = user_clients.get(acc_id) if (acc_id and acc_id in user_clients) else source_client
    file_size = getattr(message.file, "size", 0) if message.file else 0

    _, (src_w, src_h) = build_video_attributes(message, None)
    is_vid = is_video_message(message)
    source_has_broken_frame = bool(is_vid and getattr(message, "document", None) and (src_w <= 16 or src_h <= 16))

    payload = preloaded
    dl_lock = asyncio.Lock()
    reusable_cloud_media = None
    reusable_cloud_client = None
    successful_targets: list[str] = []

    def get_candidates(tgt_id: int) -> list[tuple[TelegramClient, str]]:
        cands: list[tuple[TelegramClient, str]] = []
        if primary_client is not None:
            cands.append((primary_client, f"u_{acc_id or 'prim'}"))
        if bot_client not in [c for c, _ in cands] and (file_size <= 50_000_000 or not file_size):
            cands.append((bot_client, "bot"))
        return cands

    async def ensure_payload():
        nonlocal payload
        async with dl_lock:
            if payload is None:
                logger.info(f"📥 Fast downloading ({format_size(file_size)})...")
                payload = await prepare_video_payload(source_client, message, src_canon, source_peer=source_chat_id)
        return payload

    async def handle_flood(secs: int, client_used):
        cid = acc_id if (client_used is primary_client) else None
        if flood_cb:
            await flood_cb(secs)
        else:
            await wait_out_flood(cid, secs)

    async def send_to_single_target(tgt_id: int, allow_cloud_probe: bool = True) -> str | None:
        nonlocal reusable_cloud_media, reusable_cloud_client
        target_title = profile.get("target_titles", {}).get(str(tgt_id), str(tgt_id))

        for u_client, u_key in get_candidates(tgt_id):
            tgt_ent = await get_entity_safe(u_client, tgt_id, u_key)
            if not tgt_ent:
                continue
            target_title = getattr(tgt_ent, "title", None) or target_title

            # Fast path A: Reuse already-uploaded media from Target #1
            if reusable_cloud_media is not None and reusable_cloud_client is u_client:
                try:
                    sent_msg = await upload_file_with_retry(
                        u_client, tgt_ent, reusable_cloud_media,
                        caption_text, caption_entities, flood_cb=flood_cb
                    )
                    if sent_msg:
                        return target_title
                except Exception as e:
                    logger.debug(f"Cloud media reuse failed on {target_title}: {e}")

            # Fast path B: Direct cloud forward / copy ONLY if source isn't restricted & video dims intact
            if (
                allow_cloud_probe
                and payload is None
                and u_client is source_client
                and not source_has_broken_frame
                and src_canon not in restricted_source_chats
            ):
                # B1: Native ForwardMessagesRequest(drop_author=True) — fastest instant server copy
                b1_perm_skip = False
                for _ in range(3):
                    try:
                        src_ent = await get_entity_safe(u_client, source_chat_id, u_key)
                        if src_ent is None:
                            break
                        drop_caps = profile.get("caption_mode", "original") != "original"
                        res_upd = await u_client(functions.messages.ForwardMessagesRequest(
                            from_peer=src_ent,
                            id=[message.id],
                            to_peer=tgt_ent,
                            random_id=[helpers.generate_random_long()],
                            drop_author=True,
                            drop_media_captions=drop_caps,
                        ))
                        if res_upd:
                            for upd in getattr(res_upd, "updates", []):
                                m_obj = getattr(upd, "message", None)
                                if m_obj and getattr(m_obj, "media", None):
                                    reusable_cloud_media = m_obj.media
                                    reusable_cloud_client = u_client
                                    break
                            return target_title
                        break
                    except ChatForwardsRestrictedError as e:
                        restricted_source_chats.add(src_canon)
                        logger.info(f"Source restricted ({type(e).__name__}) -> switching to turbo transfer")
                        break
                    except (ChatWriteForbiddenError, ChannelPrivateError, PeerIdInvalidError):
                        b1_perm_skip = True
                        break
                    except FloodWaitError as fl:
                        await handle_flood(fl.seconds, u_client)
                        continue
                    except Exception as fwd_e:
                        err_s = str(fwd_e).lower()
                        if "protected chat" in err_s or "forwards" in err_s or "chat_forwards_restricted" in err_s:
                            restricted_source_chats.add(src_canon)
                            logger.info("Protected chat cached -> turbo transfer")
                        break
                if b1_perm_skip:
                    continue

                # B2: Direct SendMedia cloud copy if B1 didn't handle & source still unrestricted
                if src_canon not in restricted_source_chats:
                    try:
                        sent_msg = await upload_file_with_retry(
                            u_client, tgt_ent, message.media,
                            caption_text, caption_entities, flood_cb=flood_cb
                        )
                        if sent_msg:
                            reusable_cloud_media = sent_msg.media
                            reusable_cloud_client = u_client
                            return target_title
                    except ChatForwardsRestrictedError as e:
                        restricted_source_chats.add(src_canon)
                        logger.info(f"Source restricted ({type(e).__name__}) -> switching to turbo transfer")
                    except (ChatWriteForbiddenError, ChannelPrivateError, PeerIdInvalidError):
                        continue
                    except Exception as e:
                        err_s = str(e).lower()
                        if "protected chat" in err_s or "forwards" in err_s or "chat_forwards_restricted" in err_s:
                            restricted_source_chats.add(src_canon)
                            logger.info("Protected chat cached -> turbo transfer")
                        elif "file reference" in err_s:
                            pass
                        elif "peer" in err_s or "write" in err_s or "admin" in err_s:
                            continue
                        else:
                            logger.warning(f"Direct forward error on {u_key}: {e}")

            # Fallback path C: Turbo 512KB download/upload preserving native video frame
            p = await ensure_payload()
            if not p or not p.get("downloaded_path") or not os.path.exists(p["downloaded_path"]):
                return None

            try:
                sent_msg = await upload_file_with_retry(
                    u_client,
                    tgt_ent,
                    p["downloaded_path"],
                    caption_text,
                    caption_entities,
                    attributes=p.get("video_attrs"),
                    thumb=p.get("thumb_path"),
                    mime_type=p.get("mime_type"),
                    flood_cb=flood_cb,
                )
                if sent_msg:
                    if reusable_cloud_media is None:
                        reusable_cloud_media = sent_msg.media
                        reusable_cloud_client = u_client
                    return target_title
            except (ChatWriteForbiddenError, ChannelPrivateError, PeerIdInvalidError) as perm_err:
                logger.warning(f"{u_key} cannot write to {target_title} ({type(perm_err).__name__}), trying next client...")
                continue
            except Exception as up_err:
                logger.error(f"Fallback upload error to {target_title} on {u_key}: {up_err}")
        return None

    try:
        # Step 1: Send to first target to prime reusable_cloud_media cleanly
        first_res = await send_to_single_target(target_ids[0], allow_cloud_probe=True)
        if first_res:
            successful_targets.append(first_res)

        # Step 2: Fan out to remaining targets concurrently!
        if len(target_ids) > 1:
            tgt_sem = asyncio.Semaphore(5)

            async def _fan(tid):
                async with tgt_sem:
                    return await send_to_single_target(tid, allow_cloud_probe=(reusable_cloud_media is None))

            rem_results = await asyncio.gather(*(_fan(tid) for tid in target_ids[1:]))
            for r in rem_results:
                if r:
                    successful_targets.append(r)
    finally:
        _cleanup_payload(payload)

    if successful_targets:
        _record_fingerprint_and_count(profile, fp, count=1, defer_save=defer_save)

        if send_notification and profile.get("notify_on_forward", True):
            notify_id = profile.get("notify_chat_id")
            if notify_id:
                tgt_str = ", ".join(h(t) for t in successful_targets)
                try:
                    await bot_client.send_message(
                        notify_id,
                        f"🎬 <b>নতুন ভিডিও সফলভাবে ফরোয়ার্ড হয়েছে!</b>\n\n"
                        f"📥 <b>সোর্স:</b> {h(source_title)}\n"
                        f"📤 <b>টার্গেটসমূহ ({len(successful_targets)}টি):</b> {tgt_str}\n"
                        f"📁 <b>সাইজ:</b> {h(format_size(file_size))}\n"
                        f"📊 <b>সর্বমোট ফরোয়ার্ড:</b> <code>{profile.get('forwarded_count', 0)}</code> টি"
                    )
                except Exception as e:
                    logger.warning(f"Notification skipped: {e}")

    return successful_targets

# ==================== ASYNC VIDEO QUEUE WORKER ====================

video_queue: asyncio.Queue = asyncio.Queue()

async def video_forward_worker():
    logger.info("🎬 Video forward queue worker online.")
    while True:
        try:
            item = await video_queue.get()
            if item is None:
                break
            src_client, msg, chat_id, s_title, prof_key = item
            try:
                await distribute_video(src_client, msg, chat_id, s_title, prof_key, send_notification=True)
            finally:
                video_queue.task_done()
        except Exception as e:
            logger.error(f"Worker error in video forwarding: {e}")
            await asyncio.sleep(1)

def enqueue_incoming_video(src_client: TelegramClient, message, chat_id: int, source_title: str, prof_key: str):
    canon = to_canonical_id(chat_id)
    dedup_key = (prof_key, canon, message.id)
    now = time.monotonic()

    if len(recent_dispatched) > 500:
        cutoff = now - 120
        for k in [k for k, t in recent_dispatched.items() if t < cutoff]:
            recent_dispatched.pop(k, None)

    if dedup_key in recent_dispatched and (now - recent_dispatched[dedup_key] < 60):
        return
    recent_dispatched[dedup_key] = now
    video_queue.put_nowait((src_client, message, chat_id, source_title, prof_key))

# ==================== USERBOT EVENT REGISTRATION ====================

def attach_userbot_listeners(client: TelegramClient, account_uid: int):
    @client.on(events.NewMessage)
    async def on_account_new_message(event):
        if not event.chat_id:
            return
        canon_chat_id = to_canonical_id(event.chat_id)

        for prof_key, profile in list(app_state["profiles"].items()):
            if profile.get("active_account_id") != account_uid:
                continue
            if not profile.get("auto_forward_enabled", True):
                continue

            active_canons = {to_canonical_id(sid) for sid in profile.get("source_ids", [])}
            if canon_chat_id in active_canons and is_eligible_media(event.message, profile):
                s_titles = profile.get("source_titles", {})
                source_title = s_titles.get(str(event.chat_id), s_titles.get(str(canon_chat_id), "Source Group"))
                file_size = getattr(event.message.file, "size", 0) if event.message.file else 0
                logger.info(f"🚨 [Acc:{account_uid}] New media in [{source_title}] ({format_size(file_size)}) -> Queue")
                enqueue_incoming_video(client, event.message, event.chat_id, source_title, prof_key)

async def run_client_forever(client: TelegramClient, label: str):
    """Keeps Telethon client running 24/7 with automatic reconnect backoff"""
    backoff = 5
    while True:
        try:
            if not client.is_connected():
                await client.connect()
            await client.run_until_disconnected()
            logger.warning(f"⚠️ {label} disconnected cleanly. Reconnecting in {backoff}s...")
        except asyncio.CancelledError:
            break
        except (tl_errors.AuthKeyDuplicatedError, tl_errors.AuthKeyUnregisteredError) as fatal_auth:
            logger.error(f"❌ {label} session invalidated ({type(fatal_auth).__name__}). Reconnect via /login QR.")
            for k, v in list(user_clients.items()):
                if v is client:
                    user_clients.pop(k, None)
            break
        except Exception as e:
            if "authkeyduplicated" in str(e).lower():
                logger.error(f"❌ {label} session duplicated/invalidated. Reconnect via /login QR.")
                for k, v in list(user_clients.items()):
                    if v is client:
                        user_clients.pop(k, None)
                break
            logger.error(f"⚠️ {label} connection error ({e}). Reconnecting in {backoff}s...")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)

async def start_user_account(session_str: str, phone: str = "") -> tuple[int, TelegramClient]:
    """Boots a Userbot TelegramClient from StringSession and registers it live"""
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        await client.start()
        me = await client.get_me()
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise
    uid = me.id
    full_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or str(uid)

    app_state["accounts"][str(uid)] = {
        "uid": uid,
        "session_string": client.session.save(),
        "name": full_name,
        "username": getattr(me, "username", None),
        "phone": getattr(me, "phone", None) or phone,
    }
    save_state()

    user_clients[uid] = client
    attach_userbot_listeners(client, uid)

    asyncio.create_task(scan_account_dialogs(uid, force_refresh=True))
    logger.info(f"✅ Userbot Online: {full_name} (ID: {uid})")
    return uid, client

# ==================== TURBO BULK SYNC WORKER ====================

async def resilient_iter_messages(
    client: TelegramClient,
    entity,
    *,
    filter_obj=None,
    server_total: int = 0,
    should_stop_fn=None,
    label: str = "",
):
    """
    Iterates ALL matching messages safely across 10,000-100,000+ message channels:
    - Retries transient FloodWait / Timeout / Connection / Server errors resuming from exact offset_id
    - Recovers via reverse=False if Telegram reverse=True stops early on cleared-history holes
    """
    seen_msg_ids: set[int] = set()
    last_id = 0
    retries = 0

    while retries < 8:
        if should_stop_fn and should_stop_fn():
            return
        try:
            it = client.iter_messages(
                entity,
                limit=None,
                offset_id=last_id,
                filter=filter_obj,
                reverse=True,
                wait_time=0.25,
            )
            async for msg in it:
                if should_stop_fn and should_stop_fn():
                    return
                if not msg or msg.id in seen_msg_ids:
                    continue
                seen_msg_ids.add(msg.id)
                if msg.id > last_id:
                    last_id = msg.id
                yield msg
            break
        except FloodWaitError as fl:
            wait_s = min(fl.seconds + 2, 120)
            logger.warning(f"History scan FloodWait ({fl.seconds}s) on {label} at id={last_id}: sleeping {wait_s}s...")
            await asyncio.sleep(wait_s)
            retries += 1
        except (
            asyncio.TimeoutError,
            tl_errors.TimedOutError,
            tl_errors.ServerError,
            tl_errors.RpcCallFailError,
            ConnectionError,
            OSError,
            ValueError,
        ) as net_e:
            retries += 1
            wait_s = min(4 * retries, 30)
            logger.warning(f"History scan interrupted on {label} at id={last_id} ({net_e}); retry {retries}/8 in {wait_s}s...")
            await asyncio.sleep(wait_s)

    if (not should_stop_fn or not should_stop_fn()) and server_total >= 40 and len(seen_msg_ids) < int(server_total * 0.85):
        logger.info(f"🔄 [{label}] Reverse scan reached {len(seen_msg_ids)}/{server_total}; recovering rest via descending pass")
        desc_offset = 0
        retries = 0
        while retries < 8:
            if should_stop_fn and should_stop_fn():
                return
            try:
                it_desc = client.iter_messages(
                    entity,
                    limit=None,
                    offset_id=desc_offset,
                    filter=filter_obj,
                    reverse=False,
                    wait_time=0.25,
                )
                async for msg in it_desc:
                    if should_stop_fn and should_stop_fn():
                        return
                    if not msg:
                        continue
                    if desc_offset == 0 or msg.id < desc_offset:
                        desc_offset = msg.id
                    if msg.id in seen_msg_ids:
                        continue
                    seen_msg_ids.add(msg.id)
                    yield msg
                break
            except FloodWaitError as fl:
                wait_s = min(fl.seconds + 2, 120)
                await asyncio.sleep(wait_s)
                retries += 1
            except (
                asyncio.TimeoutError,
                tl_errors.TimedOutError,
                tl_errors.ServerError,
                tl_errors.RpcCallFailError,
                ConnectionError,
                OSError,
                ValueError,
            ):
                retries += 1
                await asyncio.sleep(min(4 * retries, 30))

async def _batch_forward_unrestricted(
    src_client: TelegramClient,
    s_entity,
    source_id: int,
    src_canon: int,
    s_title: str,
    batch: list,
    profile: dict,
    prof_key: str,
    target_ids: list[int],
    flood_cb=None,
    should_stop_fn=None,
) -> tuple[int, list]:
    """
    Forwards up to 15 unrestricted videos per RPC via ForwardMessagesRequest(drop_author=True).
    - Respects account_flood_until & adaptive pacing to prevent hourly 3300s lockouts
    - Waits out FloodWait cleanly once & retries the batch (NEVER splits FloodWait into 15-20x loops!)
    - Only splits non-flood item errors per-message before falling back to download+upload
    """
    valid_targets = [tid for tid in target_ids if to_canonical_id(tid) != src_canon]
    if not valid_targets or not batch:
        return 0, []

    acc_id = profile.get("active_account_id")
    drop_caps = profile.get("caption_mode", "original") != "original"
    msg_ids = [m.id for m in batch]

    user_ok_targets = 0
    bot_only_targets: list[int] = []
    fallback_msgs_by_id: dict[int, object] = {}

    async def handle_wait(secs: int):
        if flood_cb:
            await flood_cb(secs)
        else:
            await wait_out_flood(acc_id, secs, should_stop_fn=should_stop_fn)

    async def send_forward_ids(tgt_ent, tid: int, id_list: list[int]) -> tuple[bool, bool, bool]:
        """Returns (success, was_restricted, was_flood_aborted)."""
        for attempt in range(4):
            if should_stop_fn and should_stop_fn():
                return False, False, True
            rem_lock = int(account_flood_until.get(acc_id, 0.0) - time.time()) if acc_id else 0
            if rem_lock > 2:
                await handle_wait(rem_lock)
                if should_stop_fn and should_stop_fn():
                    return False, False, True

            t0 = time.monotonic()
            try:
                await src_client(functions.messages.ForwardMessagesRequest(
                    from_peer=s_entity,
                    id=id_list,
                    to_peer=tgt_ent,
                    random_id=[helpers.generate_random_long() for _ in id_list],
                    drop_author=True,
                    drop_media_captions=drop_caps,
                ))
                elapsed = time.monotonic() - t0
                # Detect hidden Telethon internal flood sleep (>8s) and slow pacing proactively!
                if acc_id and elapsed > 8.0:
                    cur_p = account_pace_delay.get(acc_id, 1.5)
                    new_p = min(max(cur_p * 1.4, 2.8), 6.0)
                    account_pace_delay[acc_id] = new_p
                    logger.info(f"⚠️ Internal Telegram flood pause detected ({elapsed:.1f}s); raising batch pace to {new_p:.1f}s")
                    await asyncio.sleep(2.5)
                return True, False, False
            except ChatForwardsRestrictedError:
                restricted_source_chats.add(src_canon)
                logger.info(f"Batch forward restricted on {s_title} -> switching to Turbo transfer")
                return False, True, False
            except FloodWaitError as fl:
                logger.warning(f"Batch Forward FloodWait ({fl.seconds}s) on [{acc_id}] -> handling cleanly")
                await handle_wait(fl.seconds)
                if should_stop_fn and should_stop_fn():
                    return False, False, True
                continue
            except (ChatWriteForbiddenError, ChannelPrivateError, PeerIdInvalidError):
                bot_only_targets.append(tid)
                return False, False, False
            except Exception as e:
                err_s = str(e).lower()
                if "protected" in err_s or "chat_forwards_restricted" in err_s or ("forward" in err_s and "restrict" in err_s):
                    restricted_source_chats.add(src_canon)
                    return False, True, False
                logger.warning(f"Batch RPC error on {tid} (count={len(id_list)}): {e}")
                return False, False, False
        return False, False, True

    for tid in valid_targets:
        tgt_ent = await get_entity_safe(src_client, tid, f"u_{acc_id}")
        if not tgt_ent:
            bot_only_targets.append(tid)
            continue

        ok, is_restr, hit_flood = await send_forward_ids(tgt_ent, tid, msg_ids)
        if is_restr:
            return 0, batch
        if hit_flood:
            return 0, []
        if ok:
            user_ok_targets += 1
            continue

        # Only split into individual calls for non-flood item errors (never FloodWaitError)
        if tid not in bot_only_targets and len(batch) > 1:
            indiv_ok = 0
            for m in batch:
                m_ok, m_restr, m_flood = await send_forward_ids(tgt_ent, tid, [m.id])
                if m_restr:
                    return 0, batch
                if m_flood:
                    break
                if m_ok:
                    indiv_ok += 1
                else:
                    fallback_msgs_by_id[m.id] = m
            if indiv_ok > 0:
                user_ok_targets += 1
        elif tid not in bot_only_targets:
            fallback_msgs_by_id[batch[0].id] = batch[0]

    # Adaptive spacing prevents triggering Telegram's 1-hour 3300s ban after 2,000 videos
    if user_ok_targets > 0:
        pace_delay = account_pace_delay.get(acc_id, 1.4) if acc_id else 1.0
        await asyncio.sleep(pace_delay)

    succeeded_msgs = [m for m in batch if m.id not in fallback_msgs_by_id]
    rej_msgs = list(fallback_msgs_by_id.values())

    if user_ok_targets > 0:
        for m in succeeded_msgs:
            _record_fingerprint_and_count(profile, get_media_fingerprint(m), count=1, defer_save=True)

    bot_delivered = 0
    if bot_only_targets:
        for m in succeeded_msgs:
            res = await distribute_video(
                src_client, m, source_id, s_title, prof_key,
                send_notification=False, target_ids_override=bot_only_targets, defer_save=True, flood_cb=flood_cb
            )
            if res:
                bot_delivered += 1

    if user_ok_targets > 0:
        return len(succeeded_msgs), rej_msgs
    if bot_delivered > 0:
        return bot_delivered, rej_msgs
    return 0, batch

async def run_multi_bulk_sync(prof_key: str, limit_count: int | None, reply_chat_id: int, status_msg_id: int):
    sync_state = get_active_sync(prof_key)
    sync_state["running"] = True
    sync_state["should_stop"] = False
    sync_state["copied"] = 0
    sync_state["scanned"] = 0
    sync_state["chat_id"] = reply_chat_id
    sync_state["msg_id"] = status_msg_id
    sync_state["task"] = asyncio.current_task()

    profile = get_profile(prof_key)
    source_ids = list(profile.get("source_ids", []))
    target_ids = list(profile.get("target_ids", []))
    acc_id = profile.get("active_account_id")
    src_client = user_clients.get(acc_id) if (acc_id and acc_id in user_clients) else None

    if not src_client:
        await bot_client.edit_message(
            reply_chat_id, status_msg_id,
            "⚠️ পুরোনো হিস্টোরি সিঙ্ক করতে হলে আপনার অ্যাকাউন্টটি কানেক্ট থাকতে হবে!\n"
            "নিচের বাটন থেকে অ্যাকাউন্ট কানেক্ট করুন:",
            buttons=[[Button.inline("📲 অ্যাকাউন্ট কানেক্ট করুন", f"login:{prof_key}".encode())], [Button.inline("🔙 মূল মেন্যু", f"home:{prof_key}".encode())]]
        )
        sync_state["running"] = False
        sync_state["task"] = None
        return

    if not source_ids or not target_ids:
        await bot_client.edit_message(
            reply_chat_id, status_msg_id,
            "⚠️ সোর্স এবং টার্গেট উভয় গ্রুপ সিলেক্ট থাকতে হবে!",
            buttons=[[Button.inline("🔙 মূল মেন্যু", f"home:{prof_key}".encode())]]
        )
        sync_state["running"] = False
        sync_state["task"] = None
        return

    copied = 0
    scanned = 0
    skipped_dup = 0
    skipped_other = 0
    failed = 0
    cur_expected = 0
    last_edit = 0.0
    unsaved_ops = 0
    last_cooldown_at = 0
    state_lock = asyncio.Lock()

    async def sync_flood_cb(secs: int):
        await wait_out_flood(
            acc_id,
            secs,
            reply_chat_id=reply_chat_id,
            status_msg_id=status_msg_id,
            prof_key=prof_key,
            copied=copied,
            should_stop_fn=lambda: bool(sync_state["should_stop"]),
        )

    async def maybe_update_progress(current_title: str, mode_label: str, force: bool = False):
        nonlocal last_edit
        if sync_state["should_stop"]:
            return
        now = time.monotonic()
        if not force and (now - last_edit < 4.0):
            return
        last_edit = now
        exp_txt = f" (মোট: ~<code>{cur_expected}</code>)" if cur_expected > 0 else ""
        try:
            await bot_client.edit_message(
                reply_chat_id,
                status_msg_id,
                f"⚡ <b>মাল্টি-সিঙ্ক চলছে ({mode_label})...</b>\n\n"
                f"📥 সোর্স: <code>{len(source_ids)}</code> টি | 📤 টার্গেট: <code>{len(target_ids)}</code> টি\n"
                f"🎬 বর্তমান চ্যাট: <b>{h(current_title)}</b>{exp_txt}\n"
                f"🔎 স্ক্যান: <code>{scanned}</code> | ✅ ফরোয়ার্ড: <code>{copied}</code> টি\n"
                f"♻️ ডুপ্লিকেট বাদ: <code>{skipped_dup}</code> | 🚫 স্টিকার/অন্যান্য বাদ: <code>{skipped_other}</code> | ⚠️ ব্যর্থ: <code>{failed}</code>\n\n"
                f"🚀 <i>টার্বো পাইপলাইন সক্রিয়...</i>",
                buttons=[[Button.inline("🛑 সিঙ্ক থামান (Stop)", f"stop:{prof_key}".encode())]]
            )
        except MessageNotModifiedError:
            pass
        except Exception:
            pass

    async def maybe_cooldown_pause(current_title: str):
        nonlocal last_cooldown_at
        if copied - last_cooldown_at >= 400 and not sync_state["should_stop"]:
            last_cooldown_at = copied
            await maybe_update_progress(current_title, "রেট-সেফ বিরতি (১০ সে.)", force=True)
            for _ in range(5):
                if sync_state["should_stop"]:
                    return
                await asyncio.sleep(2.0)

    was_cancelled = False
    try:
        # Immediately surface remaining FloodWait cooldown if account is already rate-limited!
        if acc_id:
            pre_rem = int(account_flood_until.get(acc_id, 0.0) - time.time())
            if pre_rem > 3:
                await sync_flood_cb(pre_rem)

        for sid in source_ids:
            if sync_state["should_stop"] or (limit_count and copied >= limit_count):
                break

            try:
                s_entity = await get_entity_safe(src_client, sid, f"u_{acc_id}")
                if not s_entity:
                    continue

                src_canon = to_canonical_id(sid)
                s_title = profile.get("source_titles", {}).get(str(sid), getattr(s_entity, "title", str(sid)))
                if getattr(s_entity, "noforwards", False):
                    restricted_source_chats.add(src_canon)

                # Probe BOTH Video gallery index AND Document files so no video-as-file is ever skipped!
                scan_passes: list[tuple[object | None, int, str]] = []
                if profile.get("media_filter", "video_only") == "video_only":
                    vid_tot = 0
                    doc_tot = 0
                    try:
                        vid_probe = await src_client.get_messages(s_entity, limit=1, filter=types.InputMessagesFilterVideo)
                        vid_tot = getattr(vid_probe, "total", 0) or len(vid_probe)
                    except Exception:
                        pass
                    try:
                        doc_probe = await src_client.get_messages(s_entity, limit=1, filter=types.InputMessagesFilterDocument)
                        doc_tot = getattr(doc_probe, "total", 0) or len(doc_probe)
                    except Exception:
                        pass
                    if vid_tot > 0:
                        scan_passes.append((types.InputMessagesFilterVideo, vid_tot, "VideoIndex"))
                    if doc_tot > 0:
                        scan_passes.append((types.InputMessagesFilterDocument, doc_tot, "DocIndex"))
                    if not scan_passes:
                        scan_passes.append((None, 0, "FullHistory"))
                else:
                    scan_passes.append((None, 0, "AllMedia"))

                dup_on = profile.get("duplicate_protection", True)
                seen_set = set(profile.get("seen_fingerprints", [])) if dup_on else set()
                seen_source_msg_ids: set[int] = set()

                for pass_filter, pass_total, pass_name in scan_passes:
                    if sync_state["should_stop"] or (limit_count and copied >= limit_count):
                        break

                    cur_expected = pass_total
                    logger.info(f"📂 [{prof_key}] Scanning '{s_title}' ({sid}) | pass={pass_name} | expected={pass_total or 'all'}")

                    fallback_seed_msgs = []
                    raw_iter = resilient_iter_messages(
                        src_client,
                        s_entity,
                        filter_obj=pass_filter,
                        server_total=pass_total,
                        should_stop_fn=lambda: bool(sync_state["should_stop"] or (limit_count and copied >= limit_count)),
                        label=f"{s_title}:{pass_name}",
                    )

                    async def dedup_msg_iter():
                        async for m in raw_iter:
                            if m.id in seen_source_msg_ids:
                                continue
                            seen_source_msg_ids.add(m.id)
                            yield m

                    msg_iter = dedup_msg_iter()

                    # Stage A (Unrestricted sources): Safe Paced Batch Direct Forward via ForwardMessagesRequest
                    if src_canon not in restricted_source_chats:
                        batch = []
                        async for msg in msg_iter:
                            if sync_state["should_stop"] or (limit_count and copied >= limit_count):
                                break
                            scanned += 1
                            sync_state["scanned"] = scanned

                            if not is_eligible_media(msg, profile):
                                skipped_other += 1
                                if scanned % 80 == 0:
                                    await maybe_update_progress(s_title, "ব্যাচ স্ক্যান")
                                continue

                            fp = get_media_fingerprint(msg)
                            if dup_on and fp:
                                if fp in seen_set:
                                    skipped_dup += 1
                                    if skipped_dup % 100 == 0:
                                        await maybe_update_progress(s_title, "ব্যাচ স্ক্যান")
                                    continue
                                seen_set.add(fp)

                            _, (sw, sh) = build_video_attributes(msg, None)
                            is_broken = is_video_message(msg) and getattr(msg, "document", None) and (sw <= 16 or sh <= 16)
                            if is_broken:
                                res = await distribute_video(
                                    src_client, msg, sid, s_title, prof_key,
                                    send_notification=False, defer_save=True, flood_cb=sync_flood_cb
                                )
                                if res:
                                    copied += 1
                                    unsaved_ops += 1
                                    sync_state["copied"] = copied
                                else:
                                    failed += 1
                            else:
                                batch.append(msg)

                            max_batch = min(BULK_BATCH_SIZE, limit_count - copied) if limit_count else BULK_BATCH_SIZE
                            if len(batch) >= max(1, max_batch):
                                ok_n, rej = await _batch_forward_unrestricted(
                                    src_client, s_entity, sid, src_canon, s_title, batch, profile, prof_key, target_ids,
                                    flood_cb=sync_flood_cb, should_stop_fn=lambda: bool(sync_state["should_stop"])
                                )
                                batch = []
                                if ok_n:
                                    copied += ok_n
                                    unsaved_ops += ok_n
                                    sync_state["copied"] = copied
                                if rej:
                                    fallback_seed_msgs.extend(rej)
                                if unsaved_ops >= 15:
                                    save_state()
                                    unsaved_ops = 0
                                await maybe_update_progress(s_title, "ডিরেক্ট ফরোয়ার্ড")
                                await maybe_cooldown_pause(s_title)
                                if src_canon in restricted_source_chats:
                                    break

                            if limit_count and copied >= limit_count:
                                break

                        if batch and not sync_state["should_stop"] and (not limit_count or copied < limit_count):
                            ok_n, rej = await _batch_forward_unrestricted(
                                src_client, s_entity, sid, src_canon, s_title, batch, profile, prof_key, target_ids,
                                flood_cb=sync_flood_cb, should_stop_fn=lambda: bool(sync_state["should_stop"])
                            )
                            if ok_n:
                                copied += ok_n
                                unsaved_ops += ok_n
                                sync_state["copied"] = copied
                            if rej:
                                fallback_seed_msgs.extend(rej)

                    drain_remaining_iter = (src_canon in restricted_source_chats)
                    if not drain_remaining_iter and not fallback_seed_msgs:
                        continue

                    # Stage B (Restricted/Protected or fallback items): 2x Prefetch + 2x Upload Workers
                    if sync_state["should_stop"] or (limit_count and copied >= limit_count):
                        continue

                    logger.info(f"🚀 [{prof_key}] Running Turbo Prefetch Pipeline on: {s_title} ({pass_name}, seeds={len(fallback_seed_msgs)}, stream={drain_remaining_iter})")
                    prefetch_q: asyncio.Queue = asyncio.Queue(maxsize=4)
                    dl_sem = asyncio.Semaphore(2)
                    active_tasks: set[asyncio.Task] = set()
                    num_upload_workers = 2

                    async def _prefetch_job(m):
                        async with dl_sem:
                            if sync_state["should_stop"]:
                                return None
                            return await prepare_video_payload(src_client, m, src_canon, source_peer=sid)

                    def _spawn_prefetch(m):
                        t = asyncio.create_task(_prefetch_job(m))
                        active_tasks.add(t)
                        t.add_done_callback(active_tasks.discard)
                        return t

                    async def _producer():
                        nonlocal scanned, skipped_dup, skipped_other
                        enqueued = 0
                        try:
                            for seed_m in fallback_seed_msgs:
                                if sync_state["should_stop"] or (limit_count and (copied + enqueued) >= limit_count):
                                    break
                                t = _spawn_prefetch(seed_m)
                                await prefetch_q.put((seed_m, t))
                                enqueued += 1

                            if drain_remaining_iter:
                                async for msg in msg_iter:
                                    if sync_state["should_stop"] or (limit_count and (copied + enqueued) >= limit_count):
                                        break
                                    scanned += 1
                                    sync_state["scanned"] = scanned

                                    if not is_eligible_media(msg, profile):
                                        skipped_other += 1
                                        continue
                                    fp = get_media_fingerprint(msg)
                                    if dup_on and fp:
                                        if fp in seen_set:
                                            skipped_dup += 1
                                            if skipped_dup % 100 == 0:
                                                await maybe_update_progress(s_title, "টার্বো ৫১২কেবি")
                                            continue
                                        seen_set.add(fp)

                                    t = _spawn_prefetch(msg)
                                    await prefetch_q.put((msg, t))
                                    enqueued += 1
                        except asyncio.CancelledError:
                            raise
                        except Exception as pe:
                            logger.error(f"Prefetch producer error on {s_title}: {pe}")
                        finally:
                            for _ in range(num_upload_workers):
                                await prefetch_q.put(None)

                    async def _upload_worker():
                        nonlocal copied, failed, unsaved_ops
                        while True:
                            item = await prefetch_q.get()
                            if item is None:
                                prefetch_q.task_done()
                                break
                            m, dl_task = item
                            payload = None
                            try:
                                if sync_state["should_stop"] or (limit_count and copied >= limit_count):
                                    dl_task.cancel()
                                    continue
                                try:
                                    payload = await dl_task
                                except asyncio.CancelledError:
                                    raise
                                except Exception as dl_ex:
                                    logger.warning(f"Prefetch failed for msg {m.id}: {dl_ex}")
                                    payload = None

                                if sync_state["should_stop"] or (limit_count and copied >= limit_count):
                                    _cleanup_payload(payload)
                                    continue

                                res = await distribute_video(
                                    src_client, m, sid, s_title, prof_key,
                                    send_notification=False, preloaded=payload, defer_save=True, flood_cb=sync_flood_cb
                                )
                                payload = None
                                async with state_lock:
                                    if res:
                                        copied += 1
                                        unsaved_ops += 1
                                        sync_state["copied"] = copied
                                        if unsaved_ops >= 10:
                                            save_state()
                                            unsaved_ops = 0
                                    else:
                                        failed += 1
                                    await maybe_update_progress(s_title, "টার্বো ৫১২কেবি")
                            finally:
                                _cleanup_payload(payload)
                                prefetch_q.task_done()

                    prod_task = asyncio.create_task(_producer())
                    workers = [asyncio.create_task(_upload_worker()) for _ in range(num_upload_workers)]
                    try:
                        await asyncio.gather(*workers)
                    finally:
                        prod_task.cancel()
                        for w in workers:
                            if not w.done():
                                w.cancel()
                        await asyncio.gather(prod_task, *workers, return_exceptions=True)
                        for t in list(active_tasks):
                            if not t.done():
                                t.cancel()
                        while not prefetch_q.empty():
                            left = prefetch_q.get_nowait()
                            if left and isinstance(left, tuple):
                                _, lt = left
                                if not lt.done():
                                    lt.cancel()
                                elif not lt.cancelled():
                                    try:
                                        _cleanup_payload(lt.result())
                                    except Exception:
                                        pass
            except asyncio.CancelledError:
                raise
            except Exception as src_err:
                logger.error(f"Source sync error on {sid} ({prof_key}): {src_err}")
                failed += 1
                continue
    except asyncio.CancelledError:
        was_cancelled = True
        sync_state["should_stop"] = True
        logger.info(f"🛑 Multi-sync task cancelled immediately for [{prof_key}] (copied={copied})")
    except Exception as e:
        logger.error(f"Bulk sync error [{prof_key}]: {e}")
        try:
            await bot_client.edit_message(
                reply_chat_id,
                status_msg_id,
                f"❌ সিঙ্ক সমস্যা: <code>{h(e)}</code>\n(কপি হয়েছিল: {copied} টি)",
                buttons=[[Button.inline("🔙 মূল মেন্যু", f"home:{prof_key}".encode())]]
            )
        except Exception:
            pass
    else:
        status_hdr = "🛑 <b>সিঙ্ক বন্ধ করা হয়েছে!</b>" if sync_state["should_stop"] else "🎉 <b>মাল্টি-সিঙ্ক সফলভাবে সম্পন্ন হয়েছে!</b>"
        try:
            await bot_client.edit_message(
                reply_chat_id,
                status_msg_id,
                f"{status_hdr}\n\n"
                f"🔎 মোট স্ক্যান: <code>{scanned}</code> টি | ✅ ফরোয়ার্ড: <code>{copied}</code> টি\n"
                f"♻️ ডুপ্লিকেট বাদ: <code>{skipped_dup}</code> | 🚫 স্টিকার/অন্যান্য বাদ: <code>{skipped_other}</code> | ⚠️ ব্যর্থ: <code>{failed}</code>\n"
                f"📊 সর্বমোট ফরোয়ার্ড: <code>{profile.get('forwarded_count', 0)}</code> টি",
                buttons=[[Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())]]
            )
        except Exception:
            pass
    finally:
        if was_cancelled:
            try:
                await bot_client.edit_message(
                    reply_chat_id,
                    status_msg_id,
                    f"🛑 <b>সিঙ্ক তাৎক্ষণিকভাবে বন্ধ করা হয়েছে!</b>\n\n"
                    f"🔎 মোট স্ক্যান: <code>{scanned}</code> টি | ✅ ফরোয়ার্ড: <code>{copied}</code> টি\n"
                    f"♻️ ডুপ্লিকেট বাদ: <code>{skipped_dup}</code> | 🚫 স্টিকার/অন্যান্য বাদ: <code>{skipped_other}</code> | ⚠️ ব্যর্থ: <code>{failed}</code>\n"
                    f"📊 সর্বমোট ফরোয়ার্ড: <code>{profile.get('forwarded_count', 0)}</code> টি",
                    buttons=[[Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())]]
                )
            except Exception:
                pass
        save_state()
        sync_state["running"] = False
        sync_state["task"] = None

# ==================== LINK HUNTER (100+ VIDEO GROUP FINDER) ====================

HUNT_MIN_VIDEOS = 100  # Strictly > 100 videos required
_RESERVED_TME_PATHS = {
    "addstickers", "addemoji", "addtheme", "share", "proxy", "socks", "iv",
    "setlanguage", "bg", "boost", "joinchat", "c", "s", "nft", "giftcode",
    "invoice", "login", "confirm", "contact", "botfather", "spambot", "telegram",
}
_TME_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?"
    r"(\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+|c/\d+(?:/\d+)?|[A-Za-z][A-Za-z0-9_]{3,31}(?:/\d+)?)",
    re.IGNORECASE,
)
_AT_USER_RE = re.compile(r"(?<![A-Za-z0-9_.])@([A-Za-z][A-Za-z0-9_]{3,31})")

def _normalize_telegram_candidate(raw_token: str, is_at_username: bool = False) -> tuple[str, str] | None:
    tok = raw_token.strip().rstrip(".,;:!?)\"'<>")
    if not tok:
        return None
    if is_at_username:
        uname = tok.lstrip("@")
        low = uname.lower()
        if low in _RESERVED_TME_PATHS or low.endswith("bot"):
            return None
        return f"user:{low}", f"https://t.me/{uname}"

    if tok.startswith("+"):
        h_code = tok[1:]
        if len(h_code) < 5:
            return None
        return f"inv:{h_code}", f"https://t.me/+{h_code}"

    if tok.lower().startswith("joinchat/"):
        h_code = tok.split("/", 1)[1]
        if len(h_code) < 5:
            return None
        return f"inv:{h_code}", f"https://t.me/+{h_code}"

    if tok.lower().startswith("c/"):
        parts = tok.split("/")
        if len(parts) >= 2 and parts[1].isdigit():
            c_id = parts[1]
            post_part = f"/{parts[2]}" if len(parts) >= 3 and parts[2].isdigit() else ""
            return f"cid:{c_id}", f"https://t.me/c/{c_id}{post_part}"
        return None

    uname = tok.split("/", 1)[0]
    low = uname.lower()
    if low in _RESERVED_TME_PATHS or low.endswith("bot"):
        return None
    return f"user:{low}", f"https://t.me/{uname}"

def _extract_links_from_blobs(texts: list[str]) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for blob in texts:
        for m in _TME_LINK_RE.finditer(blob):
            norm = _normalize_telegram_candidate(m.group(1), is_at_username=False)
            if norm and norm[0] not in seen:
                seen[norm[0]] = norm[1]
        for m in _AT_USER_RE.finditer(blob):
            norm = _normalize_telegram_candidate(m.group(1), is_at_username=True)
            if norm and norm[0] not in seen:
                seen[norm[0]] = norm[1]
    return list(seen.items())

def extract_telegram_links_from_message(message) -> list[tuple[str, str]]:
    """Extracts deduplicated Telegram group/channel (canon_key, display_url) pairs from text, entities & buttons."""
    if not message:
        return []
    texts: list[str] = []
    txt = getattr(message, "raw_text", None) or getattr(message, "message", None) or ""
    if txt:
        texts.append(txt)

    for ent in getattr(message, "entities", None) or []:
        u = getattr(ent, "url", None)
        if u:
            texts.append(u)

    rm = getattr(message, "reply_markup", None)
    if rm and getattr(rm, "rows", None):
        for row in rm.rows:
            for btn in getattr(row, "buttons", None) or []:
                u = getattr(btn, "url", None) or getattr(getattr(btn, "type", None), "url", None)
                if u:
                    texts.append(u)

    wp = getattr(getattr(message, "media", None), "webpage", None)
    if wp and getattr(wp, "url", None):
        texts.append(wp.url)

    return _extract_links_from_blobs(texts)

def get_active_hunt(prof_key: str) -> dict:
    prof_key = str(prof_key)
    if prof_key not in active_link_hunts:
        active_link_hunts[prof_key] = {"running": False, "should_stop": False, "task": None}
    return active_link_hunts[prof_key]

def trigger_hunt_stop(prof_key: str, sender_id: int | None = None) -> tuple[bool, str]:
    candidates = [str(prof_key)]
    if sender_id is not None:
        candidates.append(str(sender_id))
    for k in list(active_link_hunts.keys()):
        if k not in candidates:
            candidates.append(k)
    for k in candidates:
        st = active_link_hunts.get(k)
        if st and st.get("running"):
            st["should_stop"] = True
            t = st.get("task")
            if t and not t.done():
                t.cancel()
            return True, k
    return False, str(prof_key)

async def leave_chat_silently(uc: TelegramClient, ent):
    if ent is None:
        return
    try:
        if isinstance(ent, types.Channel):
            await uc(functions.channels.LeaveChannelRequest(ent))
        elif isinstance(ent, types.Chat):
            await uc(functions.messages.DeleteChatUserRequest(ent.id, types.InputUserSelf()))
    except Exception:
        pass

async def resolve_chat_link_for_hunt(uc: TelegramClient, raw_or_key: str, allow_join: bool = True) -> tuple[object | None, bool, str]:
    """
    Resolves a Telegram link, @username, peer ID, or normalized key ('inv:', 'cid:', 'user:').
    Returns (entity, freshly_joined, status_note). Re-raises FloodWaitError for caller backoff.
    """
    s = str(raw_or_key or "").strip()
    if not s:
        return None, False, "empty"

    canon_key = ""
    if s.startswith(("inv:", "cid:", "user:")):
        canon_key = s
    elif (s.startswith("-") and s[1:].isdigit()) or s.isdigit():
        canon_key = f"peer:{int(s)}"
    else:
        extracted = _extract_links_from_blobs([s])
        if extracted:
            canon_key = extracted[0][0]
        else:
            cleaned = clean_chat_input(s)
            if isinstance(cleaned, int):
                canon_key = f"peer:{cleaned}"
            else:
                canon_key = f"user:{str(cleaned).lstrip('@').lower()}"

    ent = None
    freshly_joined = False
    try:
        if canon_key.startswith("inv:"):
            invite_hash = canon_key.split(":", 1)[1]
            chk = await uc(functions.messages.CheckChatInviteRequest(invite_hash))
            if isinstance(chk, (types.ChatInviteAlready, types.ChatInvitePeek)):
                ent = getattr(chk, "chat", None)
            elif isinstance(chk, types.ChatInvite):
                if getattr(chk, "request_needed", False):
                    return None, False, "approval_required"
                if not allow_join:
                    return None, False, "join_skipped"
                updates = await uc(functions.messages.ImportChatInviteRequest(invite_hash))
                if getattr(updates, "chats", None):
                    ent = updates.chats[0]
                    freshly_joined = True
        elif canon_key.startswith("cid:"):
            peer_id = int(f"-100{canon_key.split(':', 1)[1]}")
            ent = await uc.get_entity(peer_id)
        elif canon_key.startswith("peer:"):
            peer_id = int(canon_key.split(":", 1)[1])
            ent = await uc.get_entity(peer_id)
        elif canon_key.startswith("user:"):
            uname = canon_key.split(":", 1)[1]
            ent = await uc.get_entity(uname)
    except FloodWaitError:
        raise
    except tl_errors.UserAlreadyParticipantError:
        if canon_key.startswith("inv:"):
            try:
                chk = await uc(functions.messages.CheckChatInviteRequest(canon_key.split(":", 1)[1]))
                ent = getattr(chk, "chat", None)
            except FloodWaitError:
                raise
            except Exception:
                return None, False, "private"
    except (tl_errors.InviteHashExpiredError, tl_errors.InviteHashInvalidError, tl_errors.UsernameInvalidError, tl_errors.UsernameNotOccupiedError):
        return None, False, "invalid"
    except tl_errors.InviteRequestSentError:
        return None, False, "approval_required"
    except tl_errors.ChannelsTooMuchError:
        return None, False, "too_many_channels"
    except (tl_errors.ChannelPrivateError, ValueError, PeerIdInvalidError):
        return None, False, "private"
    except Exception as e:
        logger.debug(f"Hunt resolve error ({canon_key}): {e}")
        return None, False, "error"

    if ent is None or isinstance(ent, types.User):
        return None, freshly_joined, "not_chat"
    if getattr(ent, "deactivated", False) or type(ent).__name__.endswith("Forbidden"):
        return None, freshly_joined, "private"
    return ent, freshly_joined, "ok"

async def count_real_videos_in_chat(uc: TelegramClient, ent) -> int:
    """Counts videos in a group/channel via VideoIndex and checks Document-videos if needed."""
    vid_count = 0
    try:
        vid_probe = await uc.get_messages(ent, limit=1, filter=types.InputMessagesFilterVideo)
        vid_count = getattr(vid_probe, "total", 0) or len(vid_probe)
    except FloodWaitError:
        raise
    except Exception:
        return 0

    if vid_count > HUNT_MIN_VIDEOS:
        return vid_count

    # Also check video files uploaded as documents if gallery count alone <= 100
    doc_tot = 0
    try:
        doc_probe = await uc.get_messages(ent, limit=1, filter=types.InputMessagesFilterDocument)
        doc_tot = getattr(doc_probe, "total", 0) or len(doc_probe)
    except FloodWaitError:
        raise
    except Exception:
        doc_tot = 0

    if vid_count + doc_tot <= HUNT_MIN_VIDEOS:
        return vid_count

    doc_vids = 0
    try:
        async for dm in uc.iter_messages(ent, limit=min(doc_tot, 400), filter=types.InputMessagesFilterDocument):
            if is_video_message(dm):
                doc_vids += 1
    except FloodWaitError:
        raise
    except Exception:
        pass
    return vid_count + doc_vids

async def send_link_hunt_report(
    reply_chat_id: int,
    status_msg_id: int,
    prof_key: str,
    start_title: str,
    total_links: int,
    checked_count: int,
    matches: list[dict],
    skipped_under: int,
    skipped_err: int,
    stop_reason: str = "",
):
    matches_sorted = sorted(matches, key=lambda x: x["videos"], reverse=True)
    header_tag = "✅ <b>লিংক খুঁজা রিপোর্ট সম্পন্ন!</b>" if not stop_reason else f"⚠️ <b>লিংক খুঁজা {stop_reason}</b>"
    summary = (
        f"{header_tag}\n\n"
        f"📂 <b>মূল গ্রুপ:</b> {h(start_title)}\n"
        f"🔗 <b>মোট ইউনিক লিংক পাওয়া গেছে:</b> <code>{total_links}</code> টি\n"
        f"🔎 <b>যাচাই করা হয়েছে:</b> <code>{checked_count}</code> টি\n"
        f"🎬 <b>{HUNT_MIN_VIDEOS}+ ভিডিওযুক্ত লিংক:</b> <code>{len(matches_sorted)}</code> টি\n"
        f"♻️ <b>{HUNT_MIN_VIDEOS} বা কম / অকার্যকর বাদ:</b> <code>{skipped_under + skipped_err}</code> টি"
    )

    nav_buttons = [
        [Button.inline("🔍 আরেকটি গ্রুপে খুঁজুন", f"hunt:{prof_key}".encode())],
        [Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())],
    ]

    if not matches_sorted:
        try:
            await bot_client.edit_message(
                reply_chat_id,
                status_msg_id,
                summary + f"\n\n😔 এই গ্রুপে {HUNT_MIN_VIDEOS}-টির বেশি ভিডিও আছে এমন কোনো কার্যকর লিংক পাওয়া যায়নি।",
                buttons=nav_buttons,
            )
        except Exception:
            await bot_client.send_message(
                reply_chat_id,
                summary + f"\n\n😔 এই গ্রুপে {HUNT_MIN_VIDEOS}-টির বেশি ভিডিও আছে এমন কোনো কার্যকর লিংক পাওয়া যায়নি।",
                buttons=nav_buttons,
                link_preview=False,
            )
        return

    try:
        await bot_client.edit_message(
            reply_chat_id,
            status_msg_id,
            summary + "\n\n👇 <b>নিচে ১০০+ ভিডিওযুক্ত সব লিংকের তালিকা দেওয়া হলো:</b>",
        )
    except Exception:
        pass

    # Chunk report safely under Telegram's 4096-character limit per message
    chunks: list[str] = []
    cur_lines: list[str] = []
    cur_len = 0
    for idx, item in enumerate(matches_sorted, 1):
        entry = (
            f"{idx}. <b>{h(item['title'][:50])}</b> — 🎬 <code>{item['videos']}</code> টি ভিডিও\n"
            f"🔗 {h(item['link'])}"
        )
        if cur_len + len(entry) + 2 > 3500 and cur_lines:
            chunks.append("\n\n".join(cur_lines))
            cur_lines = [entry]
            cur_len = len(entry)
        else:
            cur_lines.append(entry)
            cur_len += len(entry) + 2
    if cur_lines:
        chunks.append("\n\n".join(cur_lines))

    for ci, chunk in enumerate(chunks):
        is_last = (ci == len(chunks) - 1)
        await bot_client.send_message(
            reply_chat_id,
            chunk,
            buttons=nav_buttons if is_last else None,
            link_preview=False,
        )

async def run_link_hunter(prof_key: str, acc_id: int | None, target_input: str, reply_chat_id: int, status_msg_id: int):
    hunt_state = get_active_hunt(prof_key)
    hunt_state["running"] = True
    hunt_state["should_stop"] = False
    hunt_state["task"] = asyncio.current_task()

    uc = user_clients.get(acc_id) if (acc_id and acc_id in user_clients) else None
    if not uc:
        await bot_client.edit_message(
            reply_chat_id,
            status_msg_id,
            "⚠️ গ্রুপ লিংক চেক করতে হলে আপনার টেলিগ্রাম অ্যাকাউন্ট কানেক্ট থাকতে হবে!",
            buttons=[[Button.inline("📲 অ্যাকাউন্ট কানেক্ট করুন", f"login:{prof_key}".encode())], [Button.inline("🔙 ড্যাশবোর্ড", f"home:{prof_key}".encode())]],
        )
        hunt_state["running"] = False
        hunt_state["task"] = None
        return

    matches: list[dict] = []
    discovered: dict[str, str] = {}
    checked_count = 0
    skipped_under = 0
    skipped_err = 0
    start_title = str(target_input)
    stop_reason = ""

    try:
        # Step 1: Resolve starting group
        try:
            start_ent, start_joined, note = await resolve_chat_link_for_hunt(uc, target_input, allow_join=True)
        except FloodWaitError as fl:
            await bot_client.edit_message(
                reply_chat_id,
                status_msg_id,
                f"⏳ টেলিগ্রাম রেট-লিমিটের কারণে এখন এই লিংকটি খোলা যাচ্ছে না ({fl.seconds} সেকেন্ড বিরতি)।",
                buttons=[[Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())]],
            )
            return

        if not start_ent:
            reason_bn = {
                "invalid": "লিংকটি ভুল বা মেয়াদ শেষ হয়েছে",
                "approval_required": "গ্রুপটিতে অ্যাডমিন অ্যাপ্রুভাল লাগে",
                "private": "গ্রুপটি প্রাইভেট বা অ্যাক্সেস নেই",
                "too_many_channels": "আপনার অ্যাকাউন্টে সর্বোচ্চ চ্যানেল জয়েন লিমিট পূর্ণ হয়েছে",
            }.get(note, "গ্রুপটি পাওয়া যায়নি")
            await bot_client.edit_message(
                reply_chat_id,
                status_msg_id,
                f"❌ মূল গ্রুপটি খোলা যায়নি: <b>{reason_bn}</b>\n🔗 <code>{h(target_input)}</code>",
                buttons=[[Button.inline("🔍 আবার চেষ্টা করুন", f"hunt:{prof_key}".encode())], [Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())]],
            )
            return

        start_pid = normalize_peer_id(start_ent)
        start_canon = to_canonical_id(start_pid)
        start_title = getattr(start_ent, "title", None) or getattr(start_ent, "username", str(start_pid))

        await bot_client.edit_message(
            reply_chat_id,
            status_msg_id,
            f"🔍 <b>গ্রুপ থেকে সব লিংক সংগ্রহ করা হচ্ছে...</b>\n\n"
            f"📂 <b>গ্রুপ:</b> {h(start_title)}\n"
            f"⏳ অনুগ্রহ করে অপেক্ষা করুন...",
            buttons=[[Button.inline("🛑 থামান", f"hs:{prof_key}".encode())]],
        )

        # Step 2: Scan messages inside starting group for all Telegram links
        total_msgs = 0
        try:
            probe = await uc.get_messages(start_ent, limit=1)
            total_msgs = getattr(probe, "total", 0) or len(probe)
        except Exception:
            pass

        scan_filter = types.InputMessagesFilterUrl if total_msgs > 25000 else None
        scanned_msgs = 0
        last_edit = 0.0
        async for m in resilient_iter_messages(
            uc,
            start_ent,
            filter_obj=scan_filter,
            server_total=total_msgs if scan_filter is None else 0,
            should_stop_fn=lambda: bool(hunt_state["should_stop"]),
            label=f"hunt:{start_title}",
        ):
            if hunt_state["should_stop"]:
                stop_reason = "ব্যবহারকারী থামিয়েছেন"
                break
            scanned_msgs += 1
            for ck, disp in extract_telegram_links_from_message(m):
                if ck not in discovered:
                    discovered[ck] = disp

            now_m = time.monotonic()
            if now_m - last_edit >= 3.5:
                last_edit = now_m
                try:
                    await bot_client.edit_message(
                        reply_chat_id,
                        status_msg_id,
                        f"🔍 <b>গ্রুপ থেকে সব লিংক খোঁজা হচ্ছে...</b>\n\n"
                        f"📂 <b>গ্রুপ:</b> {h(start_title)}\n"
                        f"🔎 স্ক্যান করা মেসেজ: <code>{scanned_msgs}</code> টি\n"
                        f"🔗 পাওয়া ইউনিক লিংক: <code>{len(discovered)}</code> টি",
                        buttons=[[Button.inline("🛑 থামান", f"hs:{prof_key}".encode())]],
                    )
                except Exception:
                    pass

        link_items = list(discovered.items())
        if not link_items:
            await send_link_hunt_report(reply_chat_id, status_msg_id, prof_key, start_title, 0, 0, [], 0, 0, stop_reason)
            return

        # Step 3: Verify each link 1-by-1 cleanly, checking > 100 videos
        seen_peer_canons: set[int] = {start_canon}
        last_edit = 0.0

        for idx, (ck, disp_url) in enumerate(link_items, 1):
            if hunt_state["should_stop"]:
                stop_reason = "ব্যবহারকারী থামিয়েছেন"
                break

            ent = None
            freshly_joined = False
            is_dup_chat = False
            vid_total = 0
            status_note = "error"
            try:
                for attempt in range(2):
                    try:
                        ent, freshly_joined, status_note = await resolve_chat_link_for_hunt(uc, ck, allow_join=True)
                        break
                    except FloodWaitError as fl:
                        if fl.seconds > 90:
                            stop_reason = f"টেলিগ্রাম লিমিটের ({fl.seconds}s) কারণে বিরতি"
                            raise StopAsyncIteration
                        await bot_client.edit_message(
                            reply_chat_id,
                            status_msg_id,
                            f"⏳ <b>টেলিগ্রাম লিমিটের কারণে {fl.seconds} সেকেন্ড অপেক্ষা করছে...</b>\n"
                            f"🔎 যাচাই: <code>{checked_count}/{len(link_items)}</code> | ✅ ১০০+ ভিডিও: <code>{len(matches)}</code> টি",
                            buttons=[[Button.inline("🛑 থামান ও রিপোর্ট দেখুন", f"hs:{prof_key}".encode())]],
                        )
                        for _ in range((fl.seconds + 2) // 2 + 1):
                            if hunt_state["should_stop"]:
                                break
                            await asyncio.sleep(2.0)

                if hunt_state["should_stop"]:
                    stop_reason = "ব্যবহারকারী থামিয়েছেন"
                    break

                checked_count += 1
                if status_note == "too_many_channels":
                    stop_reason = "অ্যাকাউন্টের সর্বোচ্চ চ্যানেল জয়েন লিমিট পূর্ণ হওয়ায় থেমেছে"
                    break
                if not ent:
                    skipped_err += 1
                    continue

                pid = normalize_peer_id(ent)
                pcanon = to_canonical_id(pid)
                if pcanon in seen_peer_canons:
                    is_dup_chat = True
                    skipped_err += 1
                    continue
                seen_peer_canons.add(pcanon)

                try:
                    vid_total = await count_real_videos_in_chat(uc, ent)
                except FloodWaitError as fl:
                    if fl.seconds > 90:
                        stop_reason = f"টেলিগ্রাম লিমিটের ({fl.seconds}s) কারণে বিরতি"
                        break
                    await asyncio.sleep(min(fl.seconds + 2, 92))
                    vid_total = await count_real_videos_in_chat(uc, ent)

                if vid_total > HUNT_MIN_VIDEOS:
                    ctitle = getattr(ent, "title", None) or getattr(ent, "username", None) or disp_url
                    matches.append({"title": ctitle, "videos": vid_total, "link": disp_url, "id": pid})
                else:
                    skipped_under += 1
            except StopAsyncIteration:
                break
            finally:
                # Auto-leave temporary freshly joined junk/duplicate chats so account doesn't hit 500-channel cap
                if freshly_joined and ent and (is_dup_chat or vid_total <= HUNT_MIN_VIDEOS):
                    await leave_chat_silently(uc, ent)

            # Polite pacing to minimize Invite/Resolve FloodWaits
            await asyncio.sleep(1.5 if freshly_joined else 0.45)

            now_m = time.monotonic()
            if now_m - last_edit >= 3.0 or idx == len(link_items):
                last_edit = now_m
                try:
                    await bot_client.edit_message(
                        reply_chat_id,
                        status_msg_id,
                        f"🔎 <b>লিংক যাচাই চলছে...</b>\n\n"
                        f"📂 <b>মূল গ্রুপ:</b> {h(start_title)}\n"
                        f"🔗 <b>পাওয়া মোট লিংক:</b> <code>{len(link_items)}</code> টি\n"
                        f"📊 <b>চেক সম্পন্ন:</b> <code>{idx}/{len(link_items)}</code> টি\n"
                        f"✅ <b>{HUNT_MIN_VIDEOS}+ ভিডিও মিলেছে:</b> <code>{len(matches)}</code> টি\n"
                        f"♻️ <b>বাদ পড়েছে:</b> <code>{skipped_under + skipped_err}</code> টি",
                        buttons=[[Button.inline("🛑 থামান ও এখনকার রিপোর্ট দেখুন", f"hs:{prof_key}".encode())]],
                    )
                except Exception:
                    pass

        await send_link_hunt_report(
            reply_chat_id, status_msg_id, prof_key, start_title,
            len(link_items), checked_count, matches, skipped_under, skipped_err, stop_reason
        )
    except asyncio.CancelledError:
        hunt_state["should_stop"] = True
        await send_link_hunt_report(
            reply_chat_id, status_msg_id, prof_key, start_title,
            len(discovered), checked_count, matches, skipped_under, skipped_err, "ব্যবহারকারী থামিয়েছেন"
        )
    except Exception as e:
        logger.error(f"Link hunter error [{prof_key}]: {e}")
        await send_link_hunt_report(
            reply_chat_id, status_msg_id, prof_key, start_title,
            len(discovered), checked_count, matches, skipped_under, skipped_err, f"সমস্যা: {h(e)}"
        )
    finally:
        hunt_state["running"] = False
        hunt_state["task"] = None

async def prompt_link_hunt(send_or_edit_fn, sender_id: int, prof_key: str):
    profile = get_profile(prof_key)
    acc_id = profile.get("active_account_id")
    if not acc_id or acc_id not in user_clients:
        await send_or_edit_fn(
            "⚠️ গ্রুপের ভেতরের সব লিংক খুঁজতে আপনার টেলিগ্রাম অ্যাকাউন্ট কানেক্ট থাকতে হবে। নিচের বাটনে চাপুন:",
            buttons=[
                [Button.inline("📲 অ্যাকাউন্ট কানেক্ট করুন", f"login:{prof_key}".encode())],
                [Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())],
            ],
        )
        return

    hunt_state = get_active_hunt(prof_key)
    if hunt_state.get("running"):
        await send_or_edit_fn(
            "⚠️ ইতিমধ্যেই একটি লিংক খোঁজার কাজ চলছে!",
            buttons=[[Button.inline("🛑 চলমান স্ক্যান থামান", f"hs:{prof_key}".encode())], [Button.inline("🔙 ড্যাশবোর্ড", f"home:{prof_key}".encode())]],
        )
        return

    pending_logins.pop(sender_id, None)
    pending_link_hunts[sender_id] = {"prof_key": prof_key, "created_at": time.monotonic()}

    btns = []
    # Quick picks from connected user's cached groups or configured sources
    quick_items = dialog_caches.get(acc_id, {}).get("list", [])[:4]
    if not quick_items and profile.get("source_ids"):
        s_titles = profile.get("source_titles", {})
        quick_items = [
            {"id": sid, "title": s_titles.get(str(sid), str(sid))}
            for sid in profile.get("source_ids", [])[:4]
        ]
    for it in quick_items:
        cid = it["id"]
        cb = f"hp:{prof_key}:{cid}".encode()
        if len(cb) <= 64:
            title = str(it.get("title", cid))[:24]
            btns.append([Button.inline(f"📂 {title}", cb)])
    btns.append([Button.inline("❌ বাতিল করুন", f"hc:{prof_key}".encode())])

    await send_or_edit_fn(
        "🔍 <b>লিংক খুজা (১০০+ ভিডিও ফাইন্ডার)</b>\n\n"
        "যেকোনো একটি গ্রুপ বা চ্যানেলের লিংক এখানে পাঠান (যেমন: <code>https://t.me/+...</code> বা <code>https://t.me/channel</code> বা <code>@username</code>)\n\n"
        "💡 <i>বট ওই গ্রুপের ভেতরের সব লিংক একটি একটি করে চেক করে, যেসব গ্রুপ/চ্যানেলে <b>১০০-টির বেশি ভিডিও</b> আছে সবগুলোর লিংকসহ একসাথে রিপোর্ট দিবে!</i>\n\n"
        "অথবা নিচ থেকে আপনার যেকোনো গ্রুপে চাপুন:",
        buttons=btns,
    )

# ==================== UI TEXT & BUTTON BUILDERS ====================

def get_account_badge(profile: dict) -> str:
    acc_id = profile.get("active_account_id")
    if acc_id and acc_id in user_clients:
        info = app_state["accounts"].get(str(acc_id), {})
        name = info.get("name", str(acc_id))
        phone = info.get("phone", "")
        p_str = f" (+{phone})" if phone and not str(phone).startswith("+") else f" ({phone})" if phone else ""
        return f"🟢 <b>{h(name)}</b>{h(p_str)}"
    return "🟠 সংযুক্ত নয় (শুধুমাত্র বট-অ্যাডমিন চ্যাট)"

def get_main_menu_text(prof_key: str) -> str:
    profile = get_profile(prof_key)
    s_ids = profile.get("source_ids", [])
    s_titles = profile.get("source_titles", {})
    t_ids = profile.get("target_ids", [])
    t_titles = profile.get("target_titles", {})

    s_disp = ", ".join(h(s_titles.get(str(i), str(i))) for i in s_ids[:3]) or "কোনোটি নির্বাচিত নেই"
    if len(s_ids) > 3:
        s_disp += f" (+{len(s_ids)-3}টি)"

    t_disp = ", ".join(h(t_titles.get(str(i), str(i))) for i in t_ids[:3]) or "কোনোটি নির্বাচিত নেই"
    if len(t_ids) > 3:
        t_disp += f" (+{len(t_ids)-3}টি)"

    c_mode = "আসল ক্যাপশন" if profile.get("caption_mode") == "original" else "ক্লিন (মুছে ফেলা)"
    auto_st = "🟢 চালু" if profile.get("auto_forward_enabled", True) else "🔴 বন্ধ"
    dup_st = "🟢 সক্রিয়" if profile.get("duplicate_protection", True) else "🔴 নিষ্ক্রিয়"
    f_count = profile.get("forwarded_count", 0)

    warn = ""
    if not profile.get("active_account_id") or profile.get("active_account_id") not in user_clients:
        warn = (
            "\n⚠️ <i>নোট: আপনার এই অ্যাকাউন্টের সব প্রাইভেট/পাবলিক গ্রুপ স্ক্যান করতে "
            "নিচের '📲 অ্যাকাউন্ট কানেক্ট করুন' বাটনে চাপ দিন।</i>\n"
        )

    return (
        f"🤖 <b>মাল্টি-ফরোয়ার্ডার কন্ট্রোল প্যানেল</b> 🚀\n\n"
        f"👤 <b>অ্যাকাউন্ট:</b> {get_account_badge(profile)}\n"
        f"📥 <b>সোর্স ({len(s_ids)}টি):</b> {s_disp}\n"
        f"📤 <b>টার্গেট ({len(t_ids)}টি):</b> {t_disp}\n"
        f"🛡️ <b>ডুপ্লিকেট ফিল্টার:</b> {dup_st} | ⚡ <b>লাইভ:</b> {auto_st}\n"
        f"📝 <b>ক্যাপশন:</b> {c_mode} | 📊 <b>মোট ফরোয়ার্ড:</b> <code>{f_count}</code> টি"
        f"{warn}\n"
        f"👇 <b>শুধুমাত্র নিচের বাটনে চাপুন:</b>"
    )

def get_main_menu_buttons(prof_key: str, is_private: bool = True):
    profile = get_profile(prof_key)
    sync_state = get_active_sync(prof_key)

    if sync_state["running"]:
        return [
            [Button.inline(f"🛑 সিঙ্ক চলছে ({sync_state['copied']}টি কপি)... থামাতে চাপুন", f"stop:{prof_key}".encode())],
            [Button.inline("🔄 প্রগ্রেস রিফ্রেশ", f"home:{prof_key}".encode())]
        ]

    rows = []
    acc_id = profile.get("active_account_id")
    if not acc_id or acc_id not in user_clients:
        if is_private:
            rows.append([Button.inline("📲 এই অ্যাকাউন্ট কানেক্ট করুন (সব গ্রুপ আনুন)", f"login:{prof_key}".encode())])
        else:
            rows.append([Button.url("📲 ইনবক্সে অ্যাকাউন্ট কানেক্ট করুন", "https://t.me/autforwbdbot?start=login")])

    rows.extend([
        [Button.inline(f"📥 সোর্স গ্রুপ ম্যানেজার ({len(profile.get('source_ids', []))}টি)", f"src_pg:{prof_key}:0".encode())],
        [Button.inline(f"📤 টার্গেট গ্রুপ ম্যানেজার ({len(profile.get('target_ids', []))}টি)", f"tgt_pg:{prof_key}:0".encode())],
        [Button.inline("🔄 নতুন গ্রুপ স্ক্যান (সব গ্রুপ লিস্ট আনুন)", f"scan_src:{prof_key}".encode())],
        [Button.inline("🚀 সব সোর্স থেকে সব ভিডিও ফরোয়ার্ড করুন", f"sync:{prof_key}:0".encode())],
        [
            Button.inline("🔄 ৫০টি সিঙ্ক", f"sync:{prof_key}:50".encode()),
            Button.inline("🔄 ২০০টি সিঙ্ক", f"sync:{prof_key}:200".encode()),
            Button.inline("🔄 ৫০০টি সিঙ্ক", f"sync:{prof_key}:500".encode()),
        ],
        [Button.inline("🔍 লিংক খুজা", f"hunt:{prof_key}".encode())],
        [
            Button.inline("👤 অ্যাকাউন্ট ম্যানেজার", f"acc:{prof_key}".encode()),
            Button.inline("⚙️ সেটিংস ও অপশন", f"cfg:{prof_key}".encode()),
        ],
        [Button.inline("🔄 ড্যাশবোর্ড রিফ্রেশ", f"home:{prof_key}".encode())],
    ])
    return rows

def get_settings_text(prof_key: str) -> str:
    p = get_profile(prof_key)
    c_mode = "আসল ক্যাপশন" if p.get("caption_mode") == "original" else "ক্লিন (মুছে ফেলা)"
    m_flt = "শুধুমাত্র ভিডিও" if p.get("media_filter") == "video_only" else "সব মিডিয়া (ভিডিও + ফটো)"
    auto_st = "🟢 চালু" if p.get("auto_forward_enabled", True) else "🔴 বন্ধ"
    dup_st = "🟢 সক্রিয়" if p.get("duplicate_protection", True) else "🔴 নিষ্ক্রিয়"
    ntf_st = "🟢 চালু" if p.get("notify_on_forward", True) else "🔴 বন্ধ"

    return (
        f"⚙️ <b>ফরোয়ার্ডার সেটিংস</b>\n\n"
        f"🛡️ <b>ডুপ্লিকেট ফিল্টার:</b> {dup_st}\n"
        f"⚡ <b>লাইভ অটো-ফরোয়ার্ড:</b> {auto_st}\n"
        f"📝 <b>ক্যাপশন মোড:</b> {c_mode}\n"
        f"🎥 <b>মিডিয়া ফিল্টার:</b> {m_flt}\n"
        f"🔔 <b>লাইভ নোটিফিকেশন:</b> {ntf_st}"
    )

def get_settings_buttons(prof_key: str):
    p = get_profile(prof_key)
    return [
        [Button.inline("🔴 ডুপ্লিকেট ফিল্টার বন্ধ করুন" if p.get("duplicate_protection", True) else "🟢 ডুপ্লিকেট ফিল্টার চালু করুন", f"tog:{prof_key}:dup".encode())],
        [Button.inline("🔴 অটো-ফরোয়ার্ড বন্ধ করুন" if p.get("auto_forward_enabled", True) else "🟢 অটো-ফরোয়ার্ড চালু করুন", f"tog:{prof_key}:auto".encode())],
        [Button.inline("🧹 ক্লিন মোড (ক্যাপশন মুছুন)" if p.get("caption_mode") == "original" else "📄 আসল ক্যাপশন রাখুন", f"tog:{prof_key}:cap".encode())],
        [Button.inline("📁 সব মিডিয়া (ভিডিও+ফটো)" if p.get("media_filter") == "video_only" else "🎥 শুধুমাত্র ভিডিও", f"tog:{prof_key}:flt".encode())],
        [Button.inline("🔕 নোটিফিকেশন বন্ধ করুন" if p.get("notify_on_forward", True) else "🔔 নোটিফিকেশন চালু করুন", f"tog:{prof_key}:ntf".encode())],
        [Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())],
    ]

# ==================== FAST PAGE RENDERERS ====================

async def safe_edit_message(event, text: str, buttons=None):
    try:
        return await event.edit(text, buttons=buttons)
    except MessageNotModifiedError:
        return None
    except Exception as e:
        logger.debug(f"Edit fallback to respond: {e}")
        try:
            return await event.respond(text, buttons=buttons)
        except Exception:
            return None

async def render_sources_page(event, prof_key: str, page: int, force_refresh: bool = False, toast: str | None = None):
    profile = get_profile(prof_key)
    acc_id = profile.get("active_account_id")

    # Answer callback ONCE cleanly
    if toast is not None:
        try:
            await event.answer(toast, alert=False)
        except Exception:
            pass
    elif force_refresh:
        try:
            await event.answer("🔄 সব গ্রুপ ও চ্যানেল স্ক্যান হচ্ছে...", alert=False)
        except Exception:
            pass
    else:
        try:
            await event.answer()
        except Exception:
            pass

    all_items, scan_time = await scan_account_dialogs(acc_id, force_refresh=force_refresh)

    active_src_canons = {to_canonical_id(x) for x in profile.get("source_ids", [])}
    active_tgt_canons = {to_canonical_id(x) for x in profile.get("target_ids", [])}

    per_page = 12
    total_pages = max(1, (len(all_items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    current_slice = all_items[page * per_page : (page + 1) * per_page]

    buttons = []
    for item in current_slice:
        cid = item["id"]
        canon = to_canonical_id(cid)
        title = item["title"][:22]
        if canon in active_src_canons:
            icon = "✅ [সোর্স]"
        elif canon in active_tgt_canons:
            icon = "📤 [টার্গেট]"
        else:
            icon = "➕"
        tag = " 🤖" if item.get("source") == "bot" else ""
        buttons.append([Button.inline(f"{icon} {title}{tag}", f"t_src:{prof_key}:{cid}:{page}".encode())])

    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️ পূর্ববর্তী", f"src_pg:{prof_key}:{page - 1}".encode()))
    nav.append(Button.inline(f"📄 পেজ {page + 1}/{total_pages}", b"noop"))
    if page < total_pages - 1:
        nav.append(Button.inline("পরবর্তী ➡️", f"src_pg:{prof_key}:{page + 1}".encode()))
    if nav:
        buttons.append(nav)

    buttons.append([
        Button.inline("🔄 নতুন গ্রুপ স্ক্যান", f"scan_src:{prof_key}".encode()),
        Button.inline("🚀 সব ভিডিও সিঙ্ক", f"sync:{prof_key}:0".encode()),
    ])

    if not acc_id or acc_id not in user_clients:
        if getattr(event, "is_private", True):
            buttons.append([Button.inline("📲 অ্যাকাউন্ট কানেক্ট করুন (সব গ্রুপ পান)", f"login:{prof_key}".encode())])
        else:
            buttons.append([Button.url("📲 ইনবক্সে অ্যাকাউন্ট কানেক্ট করুন", "https://t.me/autforwbdbot?start=login")])

    buttons.append([Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())])

    acc_hint = ""
    if not acc_id or acc_id not in user_clients:
        acc_hint = (
            "\n⚠️ <i>আপনার অ্যাকাউন্ট কানেক্ট না থাকায় কেবল বট যে চ্যানেল/গ্রুপে আছে সেগুলো দেখাচ্ছে। "
            "সব ব্যক্তিগত গ্রুপ আনতে নিচের '📲 অ্যাকাউন্ট কানেক্ট করুন' বাটনে চাপুন!</i>\n"
        )

    text = (
        f"📥 <b>সোর্স গ্রুপ ম্যানেজার ({len(active_src_canons)}টি নির্বাচিত)</b>\n"
        f"📊 <b>মোট গ্রুপ/চ্যানেল:</b> <code>{len(all_items)}</code> টি | 🕒 <b>স্ক্যান:</b> <code>{scan_time}</code>\n"
        f"{acc_hint}\n"
        f"যেসব গ্রুপ থেকে ভিডিও আনতে চান, সেগুলোর নামের ওপর চাপ দিয়ে ✅ (অন) করুন:"
    )
    await safe_edit_message(event, text, buttons)

async def render_targets_page(event, prof_key: str, page: int, force_refresh: bool = False, toast: str | None = None):
    profile = get_profile(prof_key)
    acc_id = profile.get("active_account_id")

    if toast is not None:
        try:
            await event.answer(toast, alert=False)
        except Exception:
            pass
    elif force_refresh:
        try:
            await event.answer("🔄 সব গ্রুপ ও চ্যানেল স্ক্যান হচ্ছে...", alert=False)
        except Exception:
            pass
    else:
        try:
            await event.answer()
        except Exception:
            pass

    all_items, scan_time = await scan_account_dialogs(acc_id, force_refresh=force_refresh)
    active_tgt_canons = {to_canonical_id(x) for x in profile.get("target_ids", [])}

    per_page = 12
    total_pages = max(1, (len(all_items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    current_slice = all_items[page * per_page : (page + 1) * per_page]

    buttons = []
    for item in current_slice:
        cid = item["id"]
        canon = to_canonical_id(cid)
        title = item["title"][:22]
        icon = "🎯 ✅ [টার্গেট]" if canon in active_tgt_canons else "🎯 ➕"
        tag = " 🤖" if item.get("source") == "bot" else ""
        buttons.append([Button.inline(f"{icon} {title}{tag}", f"t_tgt:{prof_key}:{cid}:{page}".encode())])

    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️ পূর্ববর্তী", f"tgt_pg:{prof_key}:{page - 1}".encode()))
    nav.append(Button.inline(f"📄 পেজ {page + 1}/{total_pages}", b"noop"))
    if page < total_pages - 1:
        nav.append(Button.inline("পরবর্তী ➡️", f"tgt_pg:{prof_key}:{page + 1}".encode()))
    if nav:
        buttons.append(nav)

    buttons.append([
        Button.inline("🔄 নতুন গ্রুপ স্ক্যান", f"scan_tgt:{prof_key}".encode()),
        Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode()),
    ])

    text = (
        f"📤 <b>টার্গেট গ্রুপ ম্যানেজার ({len(active_tgt_canons)}টি নির্বাচিত)</b>\n"
        f"📊 <b>মোট গ্রুপ/চ্যানেল:</b> <code>{len(all_items)}</code> টি | 🕒 <b>স্ক্যান:</b> <code>{scan_time}</code>\n\n"
        f"যেসব গ্রুপে ভিডিও পাঠাতে চান, সেগুলোর নামের ওপর চাপ দিন:"
    )
    await safe_edit_message(event, text, buttons)

# ==================== ACCOUNT MANAGER, QR LOGIN & IN-BOT NUMPAD ====================

def make_login_client() -> TelegramClient:
    return TelegramClient(
        StringSession(),
        API_ID,
        API_HASH,
        device_model="Samsung Galaxy S24",
        system_version="Android 14",
        app_version="11.5.0",
        lang_code="en",
        system_lang_code="en",
    )

def describe_sent_code(sent) -> str:
    t_name = type(getattr(sent, "type", None)).__name__
    nt_name = type(getattr(sent, "next_type", None)).__name__
    logger.info(f"SentCode delivery: type={t_name}, next_type={nt_name}")
    if "App" in t_name:
        return (
            "📲 কোডটি <b>SMS-এ নয়</b>, আপনার ওই অ্যাকাউন্টের <b>টেলিগ্রাম অ্যাপের 'Telegram' অফিশিয়াল চ্যাটে</b> পাঠানো হয়েছে!\n"
            "<i>(যদি টেলিগ্রাম অ্যাপে না পান তবে নিচের '🔄 SMS/কল-এ পাঠান' বাটনে চাপুন অথবা '📷 QR কোড' ব্যবহার করুন)</i>"
        )
    if "Sms" in t_name:
        return "📩 কোডটি আপনার নম্বরে <b>SMS</b>-এ পাঠানো হয়েছে। না পেলে নিচের '🔄 SMS/কল-এ পাঠান' অথবা '📷 QR কোড' ব্যবহার করুন।"
    if "Call" in t_name or "MissedCall" in t_name:
        return "📞 আপনার নম্বরে টেলিগ্রাম থেকে একটি <b>ভয়েস কল / মিসড কল</b> যাবে (শেষের ৫ ডিজিট হলো কোড)।"
    if "Email" in t_name:
        return "📧 টেলিগ্রাম এই নম্বরের জন্য ইমেইল ভেরিফিকেশন চাইছে! ইমেইল না থাকলে নিচের '📷 QR কোড' ব্যবহার করুন।"
    return f"📨 ডেলিভারি মাধ্যম: <code>{h(t_name)}</code>"

def build_numpad_text(code_digits: str, phone: str, delivery_note: str = "") -> str:
    padded = (code_digits + "_____")[:5]
    spaced = " ".join(list(padded))
    note_block = f"\n{delivery_note}\n" if delivery_note else "\nআপনার টেলিগ্রাম অ্যাপের 'Telegram' চ্যাটে বা SMS-এ ৫ ডিজিটের কোড পাঠানো হয়েছে।\n"
    return (
        f"🔐 <b>টেলিগ্রাম ভেরিফিকেশন কোড দিন</b>\n\n"
        f"📱 নম্বর: <code>{h(phone)}</code>"
        f"{note_block}\n"
        f"⚠️ <i>নিরাপত্তার জন্য কোডটি সরাসরি চ্যাটে না লিখে নিচের বাটনে চাপ দিয়ে বসান:</i>\n\n"
        f"📝 কোড: <code>[ {spaced} ]</code>"
    )

def build_numpad_buttons(code_digits: str, prof_key: str = ""):
    rows = [
        [Button.inline("1️⃣", b"num:1"), Button.inline("2️⃣", b"num:2"), Button.inline("3️⃣", b"num:3")],
        [Button.inline("4️⃣", b"num:4"), Button.inline("5️⃣", b"num:5"), Button.inline("6️⃣", b"num:6")],
        [Button.inline("7️⃣", b"num:7"), Button.inline("8️⃣", b"num:8"), Button.inline("9️⃣", b"num:9")],
        [Button.inline("⌫ মুছুন", b"num:del"), Button.inline("0️⃣", b"num:0"), Button.inline("✅ কনফার্ম", b"num:ok")],
        [Button.inline("🔄 SMS / কল-এ আবার কোড পাঠান", b"num:resend")],
    ]
    if prof_key:
        rows.append([Button.inline("📷 কোড ছাড়াই QR দিয়ে কানেক্ট করুন", f"qr_login:{prof_key}".encode())])
    rows.append([Button.inline("❌ বাতিল করুন", b"num:cancel")])
    return rows

async def run_qr_login_flow(uid: int, chat_id: int, prof_key: str):
    """Generates a Telegram QR login image so users connect in 3s without needing SMS/OTP codes"""
    old = pending_logins.get(uid)
    if old and old.get("client"):
        try:
            await old["client"].disconnect()
        except Exception:
            pass

    tmp_client = make_login_client()
    st = {
        "step": "WAIT_QR",
        "prof_key": prof_key,
        "created_at": time.monotonic(),
        "client": tmp_client,
        "digits": "",
    }
    pending_logins[uid] = st
    qr_msg = None
    try:
        await tmp_client.connect()
        qr_obj = await tmp_client.qr_login()
        logger.info(f"📷 QR Login started for user {uid}")

        for attempt in range(3):
            if pending_logins.get(uid) is not st or st.get("step") != "WAIT_QR":
                return
            if qrcode is None:
                raise RuntimeError("qrcode package not installed yet")

            img = qrcode.make(qr_obj.url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.name = "telegram_qr.png"
            buf.seek(0)

            cap = (
                "📷 <b>QR কোড দিয়ে লগইন (কোনো কোড বা SMS লাগবে না!)</b>\n\n"
                "১. যে অ্যাকাউন্টটি কানেক্ট করবেন সেই টেলিগ্রাম অ্যাপ খুলুন।\n"
                "২. যান: <b>Settings ➡️ Devices ➡️ Link Desktop Device</b>\n"
                "৩. এই QR কোডটি স্ক্যান করুন (অথবা স্ক্রিনশট অন্য ডিভাইস থেকে স্ক্যান করুন)।\n\n"
                "⏳ <i>প্রতি ৩০ সেকেন্ডে অটো আপডেট হবে...</i>"
            )
            if qr_msg:
                try:
                    await qr_msg.delete()
                except Exception:
                    pass
            qr_msg = await bot_client.send_file(
                chat_id,
                file=buf,
                caption=cap,
                buttons=[[Button.inline("❌ বাতিল করুন", b"num:cancel")]],
            )
            try:
                await qr_obj.wait(timeout=30)
                if pending_logins.get(uid) is not st or st.get("step") != "WAIT_QR":
                    return
                if qr_msg:
                    try:
                        await qr_msg.delete()
                    except Exception:
                        pass
                await complete_inbot_login(uid, st, chat_id)
                return
            except asyncio.TimeoutError:
                if attempt < 2 and pending_logins.get(uid) is st and st.get("step") == "WAIT_QR":
                    await qr_obj.recreate()
                continue
            except SessionPasswordNeededError:
                if pending_logins.get(uid) is not st or st.get("step") != "WAIT_QR":
                    return
                if qr_msg:
                    try:
                        await qr_msg.delete()
                    except Exception:
                        pass
                st["step"] = "WAIT_2FA"
                await bot_client.send_message(
                    chat_id,
                    "🔒 <b>QR স্ক্যান সম্পন্ন! এখন আপনার 2FA পাসওয়ার্ড লিখুন:</b>\n"
                    "<i>(নিরাপত্তার জন্য মেসেজটি সাথে সাথেই মুছে ফেলা হবে)</i>",
                    buttons=[[Button.inline("❌ বাতিল", b"num:cancel")]],
                )
                return

        if pending_logins.get(uid) is st and st.get("step") == "WAIT_QR":
            if qr_msg:
                try:
                    await qr_msg.delete()
                except Exception:
                    pass
            pending_logins.pop(uid, None)
            await bot_client.send_message(
                chat_id,
                "⏰ QR কোডের সময় শেষ হয়েছে। আবার চেষ্টা করতে বাটনে চাপুন:",
                buttons=[
                    [Button.inline("📷 আবার QR কোড নিন", f"qr_login:{prof_key}".encode())],
                    [Button.inline("🔙 ড্যাশবোর্ড", f"home:{prof_key}".encode())],
                ],
            )
    except Exception as e:
        logger.warning(f"QR login error for {uid}: {e}")
        if pending_logins.get(uid) is st and st.get("step") == "WAIT_QR":
            pending_logins.pop(uid, None)
            await bot_client.send_message(
                chat_id,
                f"❌ QR লগইন সমস্যা: <code>{h(e)}</code>",
                buttons=[[Button.inline("🔙 ড্যাশবোর্ড", f"home:{prof_key}".encode())]],
            )
    finally:
        cur = pending_logins.get(uid)
        if cur is not st and tmp_client not in user_clients.values():
            try:
                await tmp_client.disconnect()
            except Exception:
                pass

async def start_inbot_login_prompt(reply_fn, sender_id: int, prof_key: str):
    old = pending_logins.get(sender_id)
    if old and old.get("client"):
        try:
            await old["client"].disconnect()
        except Exception:
            pass
    pending_logins[sender_id] = {
        "step": "WAIT_PHONE",
        "prof_key": prof_key,
        "created_at": time.monotonic(),
        "digits": "",
    }
    await reply_fn(
        "📲 <b>আপনার টেলিগ্রাম অ্যাকাউন্ট কানেক্ট করুন</b>\n\n"
        "এই অ্যাকাউন্টের সকল প্রাইভেট ও পাবলিক গ্রুপ স্ক্যান করতে যেকোনো ১টি সহজ পদ্ধতি বেছে নিন:\n\n"
        "🥇 <b>সবচেয়ে সহজ (কোড ছাড়াই):</b> নিচের <b>📷 QR কোড দিয়ে কানেক্ট</b> বাটনে চাপুন।\n"
        "🥈 <b>ফোন নম্বর দিয়ে:</b> নিচের <b>📱 আমার ফোন নম্বর দিন</b> বাটনে চাপুন বা দেশ কোডসহ নম্বর লিখুন (<code>+88017xxxxxxxx</code>)।\n"
        "🥉 <b>সেশন স্ট্রিং:</b> পিসি (<code>login.bat</code>) থেকে তৈরি Session String সরাসরি এখানে পেস্ট করতে পারেন।",
        buttons=[
            [Button.inline("📷 QR কোড দিয়ে কানেক্ট (সবচেয়ে সহজ, কোড লাগবে না)", f"qr_login:{prof_key}".encode())],
            [Button.inline("❌ বাতিল", b"num:cancel")],
        ],
    )
    await bot_client.send_message(
        sender_id,
        "👇 ফোন নম্বর শেয়ার করতে চাইলে নিচের বোতামে চাপুন অথবা নম্বর লিখুন:",
        buttons=[
            [Button.request_phone("📱 আমার ফোন নম্বর দিন", resize=True, single_use=True)],
            [Button.text("❌ বাতিল করুন", resize=True)],
        ],
    )

async def complete_inbot_login(uid: int, state_data: dict, reply_target):
    tmp_client: TelegramClient = state_data["client"]
    me = await tmp_client.get_me()
    sess_str = tmp_client.session.save()
    new_uid = me.id
    full_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or str(new_uid)

    app_state["accounts"][str(new_uid)] = {
        "uid": new_uid,
        "session_string": sess_str,
        "name": full_name,
        "username": getattr(me, "username", None),
        "phone": getattr(me, "phone", None) or state_data.get("phone", ""),
    }

    prof_key = state_data.get("prof_key", str(uid))
    profile = get_profile(prof_key)
    profile["active_account_id"] = new_uid
    save_state()

    # Replace stale/dead client or keep fresh authenticated client live in user_clients pool
    old_client = user_clients.get(new_uid)
    if old_client is not tmp_client:
        if old_client is not None:
            try:
                await old_client.disconnect()
            except Exception:
                pass
        user_clients[new_uid] = tmp_client
        attach_userbot_listeners(tmp_client, new_uid)
        asyncio.create_task(run_client_forever(tmp_client, f"User:{new_uid}"))

    pending_logins.pop(uid, None)
    items, scan_time = await scan_account_dialogs(new_uid, force_refresh=True)

    await bot_client.send_message(
        reply_target,
        f"🎉 <b>অভিনন্দন! আপনার অ্যাকাউন্ট কানেক্ট হয়েছে!</b>\n\n"
        f"👤 <b>নাম:</b> {h(full_name)} (<code>{new_uid}</code>)\n"
        f"📊 <b>মোট গ্রুপ/চ্যানেল পাওয়া গেছে:</b> <code>{len(items)}</code> টি\n\n"
        f"এখন নিচের বাটনে চাপ দিয়ে আপনার সোর্স ও টার্গেট গ্রুপ নির্বাচন করুন:",
        buttons=[
            [Button.inline(f"📥 সোর্স গ্রুপ দেখুন ({len(items)}টি)", f"src_pg:{prof_key}:0".encode())],
            [Button.inline(f"📤 টার্গেট গ্রুপ দেখুন ({len(items)}টি)", f"tgt_pg:{prof_key}:0".encode())],
            [Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())],
        ]
    )

async def render_account_manager(event, prof_key: str):
    try:
        await event.answer()
    except Exception:
        pass
    profile = get_profile(prof_key)
    sender_id = event.sender_id
    active_acc = profile.get("active_account_id")

    buttons = []
    # Only show accounts owned by this user (or all accounts if OWNER_ID)
    for uid_str, acc in app_state.get("accounts", {}).items():
        uid_int = int(uid_str)
        if sender_id != DEFAULT_OWNER_ID and str(uid_int) != str(sender_id) and str(uid_int) != str(active_acc):
            continue
        mark = "✅ " if uid_int == active_acc else "🔄 "
        status_dot = "🟢" if uid_int in user_clients else "🔴"
        buttons.append([Button.inline(f"{mark}{status_dot} {acc.get('name', uid_str)[:20]}", f"sw_acc:{prof_key}:{uid_int}".encode())])

    if getattr(event, "is_private", True):
        buttons.append([Button.inline("➕ নতুন অ্যাকাউন্ট কানেক্ট করুন", f"login:{prof_key}".encode())])
    else:
        buttons.append([Button.url("📲 ইনবক্সে নতুন অ্যাকাউন্ট কানেক্ট করুন", "https://t.me/autforwbdbot?start=login")])

    buttons.append([Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())])

    await safe_edit_message(
        event,
        f"👤 <b>অ্যাকাউন্ট ম্যানেজার</b>\n\n"
        f"বর্তমান সক্রিয় অ্যাকাউন্ট: {get_account_badge(profile)}\n\n"
        f"আপনি চাইলে একাধিক টেলিগ্রাম অ্যাকাউন্ট কানেক্ট করে এক ক্লিকে সুইচ করতে পারেন:",
        buttons
    )

# ==================== BOT COMMAND & CHANNEL HANDLERS ====================

async def is_operator_authorized(event) -> bool:
    if event.is_private:
        return True
    uid = getattr(event, "sender_id", None)
    if not uid:
        # Anonymous channel post (Broadcast channel admin posted command)
        return bool(event.is_channel and not event.is_group)
    if uid == DEFAULT_OWNER_ID or uid in user_clients:
        return True
    try:
        perms = await bot_client.get_permissions(event.chat_id, uid)
        if perms and (perms.is_admin or perms.is_creator):
            return True
    except Exception:
        pass
    return False

@bot_client.on(events.ChatAction)
async def bot_chat_action_handler(event):
    """Auto-discovers any group/channel when @autforwbdbot is added or updated"""
    try:
        chat = await event.get_chat()
        record_bot_known_chat(chat, event.chat_id)
        save_state()
    except Exception:
        pass

@bot_client.on(events.NewMessage(pattern=r"^/(start|menu)(?:@autforwbdbot)?(?:\s+(.+))?$"))
async def bot_start_handler(event):
    if event.out:
        return
    if not await is_operator_authorized(event):
        return

    arg = (event.pattern_match.group(2) or "").strip().lower()
    prof_key, acc_id = await resolve_profile_key_from_chat(event)

    if arg == "login" and event.is_private:
        await start_inbot_login_prompt(event.reply, event.sender_id, prof_key)
        return

    if not event.is_private:
        chat = await event.get_chat()
        pid = normalize_peer_id(chat, event.chat_id)
        title = getattr(chat, "title", None) or str(pid)
        profile = get_profile(prof_key)
        is_src = to_canonical_id(pid) in {to_canonical_id(x) for x in profile.get("source_ids", [])}
        is_tgt = to_canonical_id(pid) in {to_canonical_id(x) for x in profile.get("target_ids", [])}

        quick_rows = [
            [
                Button.inline("❌ সোর্স থেকে সরান" if is_src else "📥 এই চ্যানেল/গ্রুপ সোর্স করুন", f"q_src:{prof_key}:{pid}".encode()),
                Button.inline("❌ টার্গেট থেকে সরান" if is_tgt else "📤 এই চ্যানেল/গ্রুপ টার্গেট করুন", f"q_tgt:{prof_key}:{pid}".encode()),
            ]
        ]
        all_rows = quick_rows + get_main_menu_buttons(prof_key, is_private=False)
        await event.reply(
            f"🏷️ <b>চ্যানেল/গ্রুপ যুক্ত হয়েছে:</b> {h(title)} (<code>{pid}</code>)\n\n" + get_main_menu_text(prof_key),
            buttons=all_rows,
        )
        return

    await event.reply(get_main_menu_text(prof_key), buttons=get_main_menu_buttons(prof_key, is_private=True))

@bot_client.on(events.NewMessage(pattern=r"^/scan(?:@autforwbdbot)?$"))
async def bot_scan_cmd_handler(event):
    if event.out or not await is_operator_authorized(event):
        return
    prof_key, acc_id = await resolve_profile_key_from_chat(event)
    status = await event.reply("🔄 টেলিগ্রাম অ্যাকাউন্টের সব গ্রুপ ও চ্যানেল স্ক্যান হচ্ছে...")
    all_items, scan_time = await scan_account_dialogs(acc_id, force_refresh=True)
    await bot_client.edit_message(
        event.chat_id,
        status.id,
        f"✅ <b>স্ক্যান সম্পন্ন!</b> (<code>{scan_time}</code>)\n"
        f"📊 <b>মোট গ্রুপ/চ্যানেল পাওয়া গেছে:</b> <code>{len(all_items)}</code> টি\n\n"
        f"নিচের বাটন থেকে সোর্স বা টার্গেট তালিকা দেখুন:",
        buttons=[
            [Button.inline(f"📥 সোর্স গ্রুপ দেখুন ({len(all_items)}টি)", f"src_pg:{prof_key}:0".encode())],
            [Button.inline(f"📤 টার্গেট গ্রুপ দেখুন ({len(all_items)}টি)", f"tgt_pg:{prof_key}:0".encode())],
            [Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())],
        ],
    )

@bot_client.on(events.NewMessage(pattern=r"^/(setgroup|settarget|setsource)(?:@autforwbdbot)?$"))
async def bot_quick_set_handler(event):
    if event.out or event.is_private or not await is_operator_authorized(event):
        return
    cmd = event.pattern_match.group(1).lower()
    chat = await event.get_chat()
    pid = normalize_peer_id(chat, event.chat_id)
    title = getattr(chat, "title", None) or str(pid)
    prof_key, _ = await resolve_profile_key_from_chat(event)
    profile = get_profile(prof_key)

    if cmd == "setsource":
        src_canons = {to_canonical_id(x) for x in profile["source_ids"]}
        if to_canonical_id(pid) not in src_canons:
            profile["source_ids"].append(pid)
            profile["source_titles"][str(pid)] = title
            save_state()
        await event.reply(f"✅ <b>{h(title)}</b> সোর্স হিসেবে সেট হয়েছে!", buttons=[[Button.inline("🎛️ ড্যাশবোর্ড", f"home:{prof_key}".encode())]])
    else:
        tgt_canons = {to_canonical_id(x) for x in profile["target_ids"]}
        if to_canonical_id(pid) not in tgt_canons:
            profile["target_ids"].append(pid)
            profile["target_titles"][str(pid)] = title
            save_state()
        await event.reply(f"🎯 <b>{h(title)}</b> টার্গেট হিসেবে সেট হয়েছে!", buttons=[[Button.inline("🎛️ ড্যাশবোর্ড", f"home:{prof_key}".encode())]])

@bot_client.on(events.NewMessage(pattern=r"^/login(?:@autforwbdbot)?$"))
async def bot_login_cmd_handler(event):
    if event.out:
        return
    if not event.is_private:
        await event.reply(
            "🔐 নিরাপত্তার স্বার্থে অ্যাকাউন্ট লগইন কেবল বটের ইনবক্সে করতে হয়।",
            buttons=[[Button.url("📲 ইনবক্সে কানেক্ট করুন", "https://t.me/autforwbdbot?start=login")]],
        )
        return
    prof_key, _ = await resolve_profile_key_from_chat(event)
    await start_inbot_login_prompt(event.reply, event.sender_id, prof_key)

@bot_client.on(events.NewMessage(pattern=r"^/id(?:@autforwbdbot)?$"))
async def bot_id_cmd_handler(event):
    if event.out or not await is_operator_authorized(event):
        return
    chat = await event.get_chat()
    pid = normalize_peer_id(chat, event.chat_id)
    title = getattr(chat, "title", None) or getattr(chat, "first_name", "") or str(pid)
    await event.reply(
        f"🆔 <b>চ্যাট তথ্য:</b>\n"
        f"• নাম: <b>{h(title)}</b>\n"
        f"• Chat Peer ID: <code>{pid}</code>\n"
        f"• Sender ID: <code>{event.sender_id}</code>"
    )

@bot_client.on(events.NewMessage(pattern=r"^/stop(?:@autforwbdbot)?$"))
async def bot_stop_cmd_handler(event):
    if event.out or not await is_operator_authorized(event):
        return
    prof_key, _ = await resolve_profile_key_from_chat(event)
    stopped, matched_key = trigger_sync_stop(prof_key, getattr(event, "sender_id", None), event.chat_id)
    if stopped:
        await event.reply("🛑 <b>সিঙ্ক তাৎক্ষণিকভাবে বন্ধ করা হচ্ছে...</b>")
    else:
        await event.reply(
            "ℹ️ এই মুহূর্তে কোনো সিঙ্ক চলছে না।",
            buttons=[[Button.inline("🎛️ মূল ড্যাশবোর্ড", f"home:{matched_key}".encode())]],
        )

@bot_client.on(events.NewMessage(pattern=r"^/(findlinks|linkhunt|hunt)(?:@autforwbdbot)?$"))
async def bot_findlinks_cmd_handler(event):
    if event.out or not await is_operator_authorized(event):
        return
    prof_key, _ = await resolve_profile_key_from_chat(event)
    sender_id = getattr(event, "sender_id", None) or event.chat_id
    await prompt_link_hunt(event.reply, sender_id, prof_key)

# ==================== BOT CALLBACK HANDLER ====================

@bot_client.on(events.CallbackQuery)
async def bot_callback_handler(event):
    if not await is_operator_authorized(event):
        await event.answer("⚠️ শুধুমাত্র অ্যাডমিন বা অনুমোদিত ব্যবহারকারী এটি নিয়ন্ত্রণ করতে পারবেন!", alert=True)
        return

    raw_data = event.data.decode("utf-8", errors="ignore")

    # 1. Handle Inline Login Numpad & Cancel across any step
    if raw_data.startswith("num:"):
        uid = event.sender_id
        st = pending_logins.get(uid)
        action = raw_data.split(":", 1)[1]

        if action == "cancel":
            if st and st.get("client"):
                try:
                    await st["client"].disconnect()
                except Exception:
                    pass
            prof_k = st.get("prof_key", str(event.chat_id)) if st else str(event.chat_id)
            pending_logins.pop(uid, None)
            await event.answer("❌ বাতিল করা হয়েছে", alert=False)
            await safe_edit_message(event, "❌ অ্যাকাউন্ট লগইন বাতিল করা হয়েছে।", buttons=[[Button.inline("🔙 ড্যাশবোর্ড", f"home:{prof_k}".encode())]])
            return

        if not st or st.get("step") != "WAIT_OTP":
            await event.answer("⚠️ লগইন সেশন শেষ হয়ে গেছে। আবার /login দিন।", alert=True)
            return

        if action == "resend":
            await event.answer("🔄 SMS/কল-এ পুনরায় কোড পাঠানো হচ্ছে...", alert=False)
            try:
                resent = await st["client"](functions.auth.ResendCodeRequest(st["phone"], st["hash"]))
                st["hash"] = resent.phone_code_hash
                st["delivery_note"] = describe_sent_code(resent)
                st["digits"] = ""
                await safe_edit_message(
                    event,
                    build_numpad_text("", st["phone"], st["delivery_note"]),
                    buttons=build_numpad_buttons("", st["prof_key"]),
                )
            except Exception as re_err:
                logger.warning(f"ResendCode failed for {st.get('phone')}: {re_err}")
                await safe_edit_message(
                    event,
                    f"⚠️ পুনরায় কোড পাঠানো যায়নি: <code>{h(re_err)}</code>\n\n"
                    f"💡 <b>সমাধান:</b> কোনো SMS ছাড়াই নিচের <b>📷 QR কোড দিয়ে কানেক্ট</b> বাটনে চাপ দিয়ে ৩ সেকেন্ডে কানেক্ট করুন!",
                    buttons=[
                        [Button.inline("📷 QR কোড দিয়ে কানেক্ট (সবচেয়ে সহজ)", f"qr_login:{st['prof_key']}".encode())],
                        [Button.inline("❌ বাতিল", b"num:cancel")],
                    ],
                )
            return

        if action == "del":
            st["digits"] = st["digits"][:-1]
            await event.answer()
            await safe_edit_message(event, build_numpad_text(st["digits"], st["phone"], st.get("delivery_note", "")), buttons=build_numpad_buttons(st["digits"], st.get("prof_key", "")))
            return

        if action.isdigit():
            if len(st["digits"]) < 5:
                st["digits"] += action
                await event.answer()
                await safe_edit_message(event, build_numpad_text(st["digits"], st["phone"], st.get("delivery_note", "")), buttons=build_numpad_buttons(st["digits"], st.get("prof_key", "")))
            else:
                await event.answer("৫ ডিজিট পূর্ণ হয়েছে! ✅ কনফার্ম চাপুন।", alert=False)
            return

        if action == "ok":
            code = st["digits"]
            if len(code) < 5:
                await event.answer("⚠️ আগে ৫ ডিজিটের কোডটি চাপুন!", alert=True)
                return
            await event.answer("⏳ যাচাই করা হচ্ছে...", alert=False)
            tmp: TelegramClient = st["client"]
            try:
                await tmp.sign_in(phone=st["phone"], code=code, phone_code_hash=st["hash"])
                await safe_edit_message(event, "✅ ভেরিফিকেশন সফল! গ্রুপসমূহ লোড হচ্ছে...")
                await complete_inbot_login(uid, st, event.chat_id)
            except SessionPasswordNeededError:
                st["step"] = "WAIT_2FA"
                await safe_edit_message(
                    event,
                    "🔒 <b>আপনার অ্যাকাউন্টে 2-Step Verification পাসওয়ার্ড চালু আছে!</b>\n\n"
                    "এখন চ্যাটে আপনার 2FA পাসওয়ার্ডটি লিখে পাঠান (পড়ার সাথে সাথে নিরাপত্তার জন্য মেসেজটি মুছে ফেলা হবে):",
                    buttons=[[Button.inline("❌ বাতিল", b"num:cancel")]],
                )
            except PhoneCodeInvalidError:
                st["digits"] = ""
                await safe_edit_message(event, "❌ <b>কোডটি ভুল ছিল!</b> আবার সঠিক কোড চাপুন:\n\n" + build_numpad_text("", st["phone"], st.get("delivery_note", "")), buttons=build_numpad_buttons("", st.get("prof_key", "")))
            except PhoneCodeExpiredError:
                try:
                    await tmp.disconnect()
                except Exception:
                    pass
                pending_logins.pop(uid, None)
                await safe_edit_message(event, "⏰ কোডের মেয়াদ শেষ হয়েছে। আবার '📲 অ্যাকাউন্ট কানেক্ট করুন' চাপুন।", buttons=[[Button.inline("📲 আবার চেষ্টা করুন", f"login:{st['prof_key']}".encode())]])
            except Exception as e:
                await safe_edit_message(event, f"❌ লগইন সমস্যা: <code>{h(e)}</code>", buttons=[[Button.inline("🔙 ড্যাশবোর্ড", f"home:{st['prof_key']}".encode())]])
            return

    if raw_data.startswith("qr_login:"):
        if not event.is_private:
            await event.answer("ইনবক্সে গিয়ে QR লগইন করুন!", alert=True)
            return
        prof_k = raw_data.split(":", 1)[1]
        await event.answer("📷 QR কোড তৈরি হচ্ছে...", alert=False)
        asyncio.create_task(run_qr_login_flow(event.sender_id, event.chat_id, prof_k))
        return

    if raw_data == "noop":
        await event.answer()
        return

    parts = raw_data.split(":")
    cmd = parts[0]
    prof_key = parts[1] if len(parts) > 1 else str(event.chat_id)
    cb_uid = getattr(event, "sender_id", None) or event.chat_id
    profile = get_profile(prof_key)

    if cmd == "home":
        pending_link_hunts.pop(cb_uid, None)
        await event.answer()
        await safe_edit_message(event, get_main_menu_text(prof_key), buttons=get_main_menu_buttons(prof_key, is_private=event.is_private))
        return

    if cmd == "hunt":
        await event.answer()
        async def _edit_or_send(txt, buttons=None):
            return await safe_edit_message(event, txt, buttons=buttons)
        await prompt_link_hunt(_edit_or_send, cb_uid, prof_key)
        return

    if cmd == "hc":
        pending_link_hunts.pop(cb_uid, None)
        await event.answer("❌ বাতিল করা হয়েছে")
        await safe_edit_message(event, get_main_menu_text(prof_key), buttons=get_main_menu_buttons(prof_key, is_private=event.is_private))
        return

    if cmd == "hs":
        stopped, _ = trigger_hunt_stop(prof_key, cb_uid)
        if stopped:
            await event.answer("🛑 থামানো হচ্ছে ও রিপোর্ট তৈরি হচ্ছে...", alert=False)
        else:
            await event.answer("কোনো লিংক খোঁজার কাজ চলছে না।", alert=True)
        return

    if cmd == "hp":
        pending_link_hunts.pop(cb_uid, None)
        hunt_state = get_active_hunt(prof_key)
        if hunt_state.get("running"):
            await event.answer("⚠️ ইতিমধ্যেই একটি লিংক খোঁজার কাজ চলছে!", alert=True)
            return
        cid = parts[2] if len(parts) > 2 else ""
        acc_id = profile.get("active_account_id")
        await event.answer("🔍 লিংক খোঁজা শুরু হচ্ছে...")
        status_msg = await safe_edit_message(
            event,
            "🔍 <b>লিংক খোঁজা শুরু হচ্ছে...</b>",
            buttons=[[Button.inline("🛑 থামান", f"hs:{prof_key}".encode())]],
        )
        msg_id = status_msg.id if status_msg else event.message_id
        hunt_state["task"] = asyncio.create_task(run_link_hunter(prof_key, acc_id, cid, event.chat_id, msg_id))
        return

    if cmd == "cfg":
        await event.answer()
        await safe_edit_message(event, get_settings_text(prof_key), buttons=get_settings_buttons(prof_key))
        return

    if cmd == "tog":
        key = parts[2]
        if key == "dup":
            profile["duplicate_protection"] = not profile.get("duplicate_protection", True)
            await event.answer("🛡️ ডুপ্লিকেট ফিল্টার আপডেট হয়েছে!")
        elif key == "auto":
            profile["auto_forward_enabled"] = not profile.get("auto_forward_enabled", True)
            await event.answer("⚡ অটো-ফরোয়ার্ড আপডেট হয়েছে!")
        elif key == "cap":
            profile["caption_mode"] = "none" if profile.get("caption_mode", "original") == "original" else "original"
            await event.answer("📝 ক্যাপশন মোড পরিবর্তন হয়েছে!")
        elif key == "flt":
            profile["media_filter"] = "all_media" if profile.get("media_filter", "video_only") == "video_only" else "video_only"
            await event.answer("🎥 মিডিয়া ফিল্টার পরিবর্তন হয়েছে!")
        elif key == "ntf":
            profile["notify_on_forward"] = not profile.get("notify_on_forward", True)
            await event.answer("🔔 নোটিফিকেশন সেটিংস পরিবর্তন হয়েছে!")
        save_state()
        await safe_edit_message(event, get_settings_text(prof_key), buttons=get_settings_buttons(prof_key))
        return

    if cmd == "acc":
        await render_account_manager(event, prof_key)
        return

    if cmd == "sw_acc":
        new_acc_id = int(parts[2])
        if new_acc_id in user_clients or str(new_acc_id) in app_state["accounts"]:
            profile["active_account_id"] = new_acc_id
            save_state()
            await event.answer("✅ অ্যাকাউন্ট পরিবর্তন করা হয়েছে!")
            await scan_account_dialogs(new_acc_id, force_refresh=True)
            await safe_edit_message(event, get_main_menu_text(prof_key), buttons=get_main_menu_buttons(prof_key, is_private=event.is_private))
        else:
            await event.answer("❌ অ্যাকাউন্টটি খুঁজে পাওয়া যায়নি!", alert=True)
        return

    if cmd == "login":
        if not event.is_private:
            await event.answer("ইনবক্সে গিয়ে লগইন করুন!", alert=True)
            return
        await event.answer()
        await start_inbot_login_prompt(event.respond, event.sender_id, prof_key)
        return

    if cmd == "src_pg":
        await render_sources_page(event, prof_key, int(parts[2]))
        return

    if cmd == "tgt_pg":
        await render_targets_page(event, prof_key, int(parts[2]))
        return

    if cmd == "scan_src":
        await render_sources_page(event, prof_key, 0, force_refresh=True)
        return

    if cmd == "scan_tgt":
        await render_targets_page(event, prof_key, 0, force_refresh=True)
        return

    if cmd in ("t_src", "q_src"):
        cid = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        canon = to_canonical_id(cid)
        src_ids = profile.get("source_ids", [])
        src_titles = profile.get("source_titles", {})

        existing = [x for x in src_ids if to_canonical_id(x) == canon]
        if existing:
            for ex in existing:
                src_ids.remove(ex)
                src_titles.pop(str(ex), None)
            toast = "➖ সোর্স থেকে বাদ দেওয়া হয়েছে!"
        else:
            title = str(cid)
            acc_id = profile.get("active_account_id")
            if acc_id and acc_id in user_clients:
                ent = await get_entity_safe(user_clients[acc_id], cid, f"u_{acc_id}")
                if ent:
                    title = getattr(ent, "title", title)
            elif str(cid) in app_state.get("bot_known_chats", {}):
                title = app_state["bot_known_chats"][str(cid)].get("title", title)
            src_ids.append(cid)
            src_titles[str(cid)] = title
            toast = f"✅ {title[:18]} সোর্স হিসেবে যুক্ত!"

        profile["source_ids"] = src_ids
        profile["source_titles"] = src_titles
        save_state()

        if cmd == "t_src":
            await render_sources_page(event, prof_key, page, toast=toast)
        else:
            await event.answer(toast, alert=False)
            await safe_edit_message(event, get_main_menu_text(prof_key), buttons=get_main_menu_buttons(prof_key, is_private=event.is_private))
        return

    if cmd in ("t_tgt", "q_tgt"):
        cid = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        canon = to_canonical_id(cid)
        tgt_ids = profile.get("target_ids", [])
        tgt_titles = profile.get("target_titles", {})

        existing = [x for x in tgt_ids if to_canonical_id(x) == canon]
        if existing:
            for ex in existing:
                tgt_ids.remove(ex)
                tgt_titles.pop(str(ex), None)
            toast = "➖ টার্গেট থেকে বাদ দেওয়া হয়েছে!"
        else:
            title = str(cid)
            acc_id = profile.get("active_account_id")
            if acc_id and acc_id in user_clients:
                ent = await get_entity_safe(user_clients[acc_id], cid, f"u_{acc_id}")
                if ent:
                    title = getattr(ent, "title", title)
            elif str(cid) in app_state.get("bot_known_chats", {}):
                title = app_state["bot_known_chats"][str(cid)].get("title", title)
            tgt_ids.append(cid)
            tgt_titles[str(cid)] = title
            toast = f"🎯 {title[:18]} টার্গেট হিসেবে যুক্ত!"

        profile["target_ids"] = tgt_ids
        profile["target_titles"] = tgt_titles
        save_state()

        if cmd == "t_tgt":
            await render_targets_page(event, prof_key, page, toast=toast)
        else:
            await event.answer(toast, alert=False)
            await safe_edit_message(event, get_main_menu_text(prof_key), buttons=get_main_menu_buttons(prof_key, is_private=event.is_private))
        return

    if cmd == "stop":
        stopped, matched_key = trigger_sync_stop(prof_key, event.sender_id, event.chat_id)
        if stopped:
            await event.answer("🛑 সিঙ্ক সাথে সাথে বন্ধ করা হচ্ছে...", alert=False)
            await safe_edit_message(
                event,
                "🛑 <b>সিঙ্ক তাৎক্ষণিকভাবে বন্ধ করা হচ্ছে...</b>",
                buttons=[[Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{matched_key}".encode())]],
            )
        else:
            await event.answer("এই মুহূর্তে কোনো সিঙ্ক চলছে না।", alert=True)
            await safe_edit_message(event, get_main_menu_text(prof_key), buttons=get_main_menu_buttons(prof_key, is_private=event.is_private))
        return

    if cmd == "sync":
        sync_state = get_active_sync(prof_key)
        existing_task = sync_state.get("task")
        if sync_state["running"] and existing_task and not existing_task.done():
            await event.answer("⚠️ ইতিমধ্যেই একটি সিঙ্ক চলছে!", alert=True)
            return
        try:
            limit_val = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            limit_val = 0
        limit_count = None if limit_val == 0 else limit_val

        if not profile.get("source_ids"):
            await event.answer("⚠️ আগে সোর্স গ্রুপ সিলেক্ট করুন!", alert=True)
            return
        if not profile.get("target_ids"):
            await event.answer("⚠️ আগে টার্গেট গ্রুপ সিলেক্ট করুন!", alert=True)
            return

        await event.answer("🚀 সিঙ্ক শুরু হচ্ছে...")
        limit_desc = "সবগুলো (All)" if limit_count is None else f"{limit_count}টি"
        status_msg = await safe_edit_message(
            event,
            f"🚀 <b>মাল্টি-সিঙ্ক শুরু হচ্ছে...</b>\n"
            f"📥 সোর্স: <code>{len(profile['source_ids'])}</code> টি | 📤 টার্গেট: <code>{len(profile['target_ids'])}</code> টি\n"
            f"📊 লিমিট: <code>{limit_desc}</code> ভিডিও",
            buttons=[[Button.inline("🛑 সিঙ্ক থামান (Stop)", f"stop:{prof_key}".encode())]]
        )
        msg_id = status_msg.id if status_msg else event.message_id
        sync_state["task"] = asyncio.create_task(run_multi_bulk_sync(prof_key, limit_count, event.chat_id, msg_id))
        return

# ==================== BOT MESSAGE HANDLER (LOGIN, LINKS, MEDIA) ====================

@bot_client.on(events.NewMessage)
async def bot_message_handler(event):
    if event.out:
        return

    # Always auto-discover Group/Channel chats where @autforwbdbot receives messages
    if not event.is_private:
        try:
            chat = await event.get_chat()
            if record_bot_known_chat(chat, event.chat_id):
                save_state()
        except Exception:
            pass

    raw_text = (event.raw_text or "").strip()
    if raw_text.startswith("/"):
        return

    # 1. Handle active In-Bot Login steps in Private DM
    if event.is_private and event.sender_id in pending_logins:
        st = pending_logins[event.sender_id]
        if raw_text == "❌ বাতিল করুন":
            try:
                if "client" in st:
                    await st["client"].disconnect()
            except Exception:
                pass
            pending_logins.pop(event.sender_id, None)
            await event.reply("❌ লগইন বাতিল করা হয়েছে।", buttons=Button.clear())
            await event.reply(get_main_menu_text(st["prof_key"]), buttons=get_main_menu_buttons(st["prof_key"], True))
            return

        if st["step"] == "WAIT_PHONE":
            # Support direct Session String paste during login!
            if raw_text and len(raw_text) > 120 and " " not in raw_text and raw_text.startswith("1"):
                try:
                    await event.delete()
                except Exception:
                    pass
                wait_msg = await event.respond("⏳ Session String যাচাই করা হচ্ছে...", buttons=Button.clear())
                tmp_client = TelegramClient(StringSession(raw_text.strip()), API_ID, API_HASH)
                try:
                    await tmp_client.connect()
                    if not await tmp_client.is_user_authorized():
                        raise RuntimeError("সেশন স্ট্রিংটি বৈধ নয় বা মেয়াদ শেষ।")
                    st["client"] = tmp_client
                    await complete_inbot_login(event.sender_id, st, event.chat_id)
                except Exception as sess_err:
                    await tmp_client.disconnect()
                    await bot_client.edit_message(event.chat_id, wait_msg.id, f"❌ সেশন যুক্ত করা যায়নি: <code>{h(sess_err)}</code>")
                return

            phone = ""
            if getattr(event.message, "contact", None):
                phone = event.message.contact.phone_number or ""
            else:
                phone = raw_text

            phone = re.sub(r"[\s\-()]", "", phone)
            if len(phone) == 11 and phone.startswith("01"):
                phone = "+88" + phone
            elif phone and not phone.startswith("+") and phone.isdigit():
                phone = "+" + phone

            if not phone or not re.match(r"^\+\d{8,15}$", phone):
                await event.reply("⚠️ সঠিক ফোন নম্বর দিন (যেমন: <code>+88017xxxxxxxx</code> বা <code>017xxxxxxxx</code>):")
                return

            await event.reply("✅ নম্বর গ্রহণ করা হয়েছে! কোড পাঠানো হচ্ছে...", buttons=Button.clear())
            wait_msg = await bot_client.send_message(event.chat_id, "⏳ টেলিগ্রাম সার্ভারে রিকোয়েস্ট পাঠানো হচ্ছে...")
            tmp_client = make_login_client()
            try:
                await tmp_client.connect()
                sent = await tmp_client.send_code_request(phone)
                delivery_note = describe_sent_code(sent)
                st["step"] = "WAIT_OTP"
                st["phone"] = phone
                st["client"] = tmp_client
                st["hash"] = sent.phone_code_hash
                st["delivery_note"] = delivery_note
                st["digits"] = ""
                await bot_client.edit_message(
                    event.chat_id,
                    wait_msg.id,
                    build_numpad_text("", phone, delivery_note),
                    buttons=build_numpad_buttons("", st["prof_key"])
                )
            except Exception as e:
                logger.error(f"send_code_request failed for {phone}: {e}")
                await tmp_client.disconnect()
                pending_logins.pop(event.sender_id, None)
                await bot_client.edit_message(
                    event.chat_id,
                    wait_msg.id,
                    f"❌ কোড পাঠানো যায়নি: <code>{h(e)}</code>\n\n"
                    f"💡 <b>বিকল্প সমাধান:</b> কোনো কোড ছাড়াই নিচের <b>📷 QR কোড</b> দিয়ে সহজেই কানেক্ট করুন!",
                    buttons=[
                        [Button.inline("📷 QR কোড দিয়ে কানেক্ট করুন", f"qr_login:{st['prof_key']}".encode())],
                        [Button.inline("🔙 ড্যাশবোর্ড", f"home:{st['prof_key']}".encode())],
                    ],
                )
            return

        if st["step"] == "WAIT_OTP":
            # Allow typed code with separators/spaces like "1 2 3 4 5" or "12-345" as backup to numpad
            digits = re.sub(r"\D", "", raw_text)
            if 5 <= len(digits) <= 6 and pending_logins.get(event.sender_id) is st:
                try:
                    await event.delete()
                except Exception:
                    pass
                tmp = st["client"]
                try:
                    await tmp.sign_in(phone=st["phone"], code=digits, phone_code_hash=st["hash"])
                    await complete_inbot_login(event.sender_id, st, event.chat_id)
                except SessionPasswordNeededError:
                    st["step"] = "WAIT_2FA"
                    await bot_client.send_message(
                        event.chat_id,
                        "🔒 আপনার অ্যাকাউন্টের <b>2FA পাসওয়ার্ড</b> লিখুন (পড়ার পর মেসেজটি ডিলিট করে দেওয়া হবে):",
                        buttons=[[Button.inline("❌ বাতিল", b"num:cancel")]],
                    )
                except Exception as e:
                    await bot_client.send_message(event.chat_id, f"❌ কোড সঠিক নয়: <code>{h(e)}</code>\nনিচের বাটনে কোডটি চাপুন।")
            return

        if st["step"] == "WAIT_2FA":
            pw = raw_text
            try:
                await event.delete()
            except Exception:
                pass
            tmp = st["client"]
            try:
                await tmp.sign_in(password=pw)
                await complete_inbot_login(event.sender_id, st, event.chat_id)
            except Exception as e:
                await event.reply(f"❌ পাসওয়ার্ড ভুল হয়েছে: <code>{h(e)}</code>\nআবার সঠিক পাসওয়ার্ড দিন:")
            return

    # 1b. Handle active Link Hunter prompt (DM or group command reply)
    hunt_sender_key = getattr(event, "sender_id", None) or event.chat_id
    if hunt_sender_key in pending_link_hunts:
        msg_links = extract_telegram_links_from_message(event.message)
        has_link_or_text = bool(raw_text or msg_links)
        if event.is_private or has_link_or_text:
            hst = pending_link_hunts[hunt_sender_key]
            h_prof_key = hst["prof_key"]
            if raw_text == "❌ বাতিল করুন":
                pending_link_hunts.pop(hunt_sender_key, None)
                await event.reply("❌ লিংক খোঁজা বাতিল করা হয়েছে।", buttons=Button.clear())
                await event.reply(get_main_menu_text(h_prof_key), buttons=get_main_menu_buttons(h_prof_key, event.is_private))
                return

            target_input = (
                msg_links[0][1]
                if msg_links
                else (raw_text if re.fullmatch(r"-?\d{5,20}", raw_text) else None)
            )
            if not target_input:
                await event.reply(
                    "⚠️ অনুগ্রহ করে একটি বৈধ টেলিগ্রাম গ্রুপ/চ্যানেলের লিংক পাঠান (যেমন: <code>https://t.me/+...</code> বা <code>@username</code>):",
                    buttons=[[Button.inline("❌ বাতিল করুন", f"hc:{h_prof_key}".encode())]],
                )
                return

            pending_link_hunts.pop(hunt_sender_key, None)
            hunt_state = get_active_hunt(h_prof_key)
            if hunt_state.get("running"):
                await event.reply(
                    "⚠️ ইতিমধ্যেই একটি লিংক খোঁজার কাজ চলছে!",
                    buttons=[[Button.inline("🛑 চলমান স্ক্যান থামান", f"hs:{h_prof_key}".encode())]],
                )
                return

            h_prof = get_profile(h_prof_key)
            h_acc_id = h_prof.get("active_account_id")
            status_msg = await event.reply(
                "🔍 <b>লিংক খোঁজা শুরু হচ্ছে...</b>",
                buttons=[[Button.inline("🛑 থামান", f"hs:{h_prof_key}".encode())]],
            )
            hunt_state["task"] = asyncio.create_task(
                run_link_hunter(h_prof_key, h_acc_id, target_input, event.chat_id, status_msg.id)
            )
            return

    # 2. Bot-side forwarding for Bot-Admin Source Channels/Groups (when Userbot isn't present or handles Bot DMs)
    if not event.is_private:
        canon_chat_id = to_canonical_id(event.chat_id)
        for prof_key, profile in list(app_state["profiles"].items()):
            if not profile.get("auto_forward_enabled", True):
                continue
            active_canons = {to_canonical_id(sid) for sid in profile.get("source_ids", [])}
            if canon_chat_id in active_canons and is_eligible_media(event.message, profile):
                # Only handle via bot_client if profile has no connected userbot in this chat
                acc_id = profile.get("active_account_id")
                has_userbot_in_chat = False
                if acc_id and acc_id in user_clients:
                    uc_cache = dialog_caches.get(acc_id, {}).get("list", [])
                    if any(to_canonical_id(item["id"]) == canon_chat_id for item in uc_cache):
                        has_userbot_in_chat = True
                if not has_userbot_in_chat:
                    s_titles = profile.get("source_titles", {})
                    s_title = s_titles.get(str(event.chat_id), s_titles.get(str(canon_chat_id), "Channel"))
                    enqueue_incoming_video(bot_client, event.message, event.chat_id, s_title, prof_key)
        return

    # 3. Direct Video uploaded to Bot DM -> distributes using bot_client as source_client!
    prof_key, acc_id = await resolve_profile_key_from_chat(event)
    profile = get_profile(prof_key)

    if is_eligible_media(event.message, profile):
        status_msg = await event.reply("🎬 ভিডিও সনাক্ত হয়েছে! সব টার্গেট গ্রুপে পাঠানো হচ্ছে...")
        res = await distribute_video(
            bot_client, event.message, event.chat_id, "Direct Bot Upload", prof_key, send_notification=False
        )
        if res:
            await bot_client.edit_message(event.chat_id, status_msg.id, f"✅ সফলভাবে <code>{len(res)}</code> টি টার্গেট গ্রুপে আপলোড সম্পন্ন হয়েছে!")
        else:
            await bot_client.edit_message(event.chat_id, status_msg.id, "⚠️ ভিডিও পাঠানো সম্ভব হয়নি (টার্গেট চেক করুন বা এটি ডুপ্লিকেট হতে পারে)।")
        return

    # 4. Pasted Group/Channel Link -> resolves (including private invite auto-join!) & adds cleanly
    if raw_text and ("t.me/" in raw_text or raw_text.startswith("@")):
        if not acc_id or acc_id not in user_clients:
            await event.reply(
                "⚠️ প্রাইভেট লিংক থেকে গ্রুপ যুক্ত করতে আপনার অ্যাকাউন্টটি কানেক্ট করুন:",
                buttons=[[Button.inline("📲 অ্যাকাউন্ট কানেক্ট করুন", f"login:{prof_key}".encode())]],
            )
            return

        uc = user_clients[acc_id]
        status_msg = await event.reply("🔍 লিংকটি যাচাই করা হচ্ছে...")
        try:
            inv_match = re.search(r"t\.me/(?:\+|joinchat/)([a-zA-Z0-9_-]+)", raw_text)
            entity = None
            if inv_match:
                invite_hash = inv_match.group(1)
                try:
                    updates = await uc(functions.messages.ImportChatInviteRequest(invite_hash))
                    if getattr(updates, "chats", None):
                        entity = updates.chats[0]
                except UserAlreadyParticipantError:
                    chk = await uc(functions.messages.CheckChatInviteRequest(invite_hash))
                    entity = getattr(chk, "chat", None)
            if entity is None:
                post_info = parse_telegram_link(raw_text)
                if post_info:
                    entity = await uc.get_entity(post_info["channel"])
                else:
                    clean = clean_chat_input(raw_text)
                    entity = await uc.get_entity(clean)

            if entity is None or isinstance(entity, types.User):
                raise ValueError("এটি কোনো গ্রুপ বা চ্যানেলের লিংক নয়!")

            peer_id = normalize_peer_id(entity)
            title = getattr(entity, "title", None) or getattr(entity, "username", str(peer_id))
            src_canons = {to_canonical_id(x) for x in profile["source_ids"]}

            if to_canonical_id(peer_id) not in src_canons:
                profile["source_ids"].append(peer_id)
                profile["source_titles"][str(peer_id)] = title
                save_state()

            await scan_account_dialogs(acc_id, force_refresh=True)
            await bot_client.edit_message(
                event.chat_id,
                status_msg.id,
                f"✅ <b>নতুন সোর্স গ্রুপ যুক্ত হয়েছে:</b> {h(title)} (<code>{peer_id}</code>)\n\n"
                f"📥 মোট সক্রিয় সোর্স: <code>{len(profile['source_ids'])}</code> টি\n"
                f"📤 মোট সক্রিয় টার্গেট: <code>{len(profile['target_ids'])}</code> টি",
                buttons=[
                    [Button.inline("🚀 সব ভিডিও সিঙ্ক করুন", f"sync:{prof_key}:0".encode())],
                    [Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())],
                ],
            )
        except Exception as e:
            logger.error(f"Link resolve error: {e}")
            await bot_client.edit_message(
                event.chat_id,
                status_msg.id,
                f"❌ লিংকটি রিড করা যায়নি: <code>{h(e)}</code>",
                buttons=[[Button.inline("📥 সোর্স গ্রুপ ম্যানেজার খুলুন", f"src_pg:{prof_key}:0".encode())]],
            )

# ==================== CLEANUP & HEALTH SERVER ====================

async def pending_login_reaper():
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        for uid in list(pending_logins.keys()):
            st = pending_logins.get(uid)
            if st and (now - st.get("created_at", now) > 300):
                try:
                    if "client" in st:
                        await st["client"].disconnect()
                except Exception:
                    pass
                pending_logins.pop(uid, None)
        for uid in list(pending_link_hunts.keys()):
            hst = pending_link_hunts.get(uid)
            if hst and (now - hst.get("created_at", now) > 600):
                pending_link_hunts.pop(uid, None)

async def handle_health(reader, writer):
    try:
        req = await reader.read(1024)
        is_head = req.startswith(b"HEAD ")
        body = b'{"status": "ok", "app": "Telegram Multi-Account Forwarder"}'
        headers = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n"
        )
        writer.write(headers if is_head else (headers + body))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

async def start_health_server():
    port = int(os.getenv("PORT", 8080))
    try:
        server = await asyncio.start_server(handle_health, "0.0.0.0", port)
        logger.info(f"🌐 Health server running on port {port}")
        async with server:
            await server.serve_forever()
    except Exception as e:
        logger.warning(f"Health server failed on port {port}: {e}")

# ==================== MAIN RUNNER ====================

async def main():
    cleanup_temp_dir()
    logger.info("=" * 60)
    logger.info("🚀 Telegram Multi-Account & Channel-Aware Forwarder Online")
    logger.info("=" * 60)

    asyncio.create_task(start_health_server())
    asyncio.create_task(video_forward_worker())
    asyncio.create_task(pending_login_reaper())

    # 1. Start default owner account from SESSION_STRING if provided
    if SESSION_STRING:
        try:
            uid, client = await start_user_account(SESSION_STRING)
            asyncio.create_task(run_client_forever(client, f"User:{uid}"))
        except Exception as e:
            logger.error(f"Failed starting primary SESSION_STRING client: {e}")

    # 2. Start any additional saved user accounts from state
    for uid_str, acc in list(app_state.get("accounts", {}).items()):
        uid_int = int(uid_str)
        if uid_int not in user_clients and acc.get("session_string"):
            try:
                uid, client = await start_user_account(acc["session_string"], acc.get("phone", ""))
                asyncio.create_task(run_client_forever(client, f"User:{uid}"))
            except Exception as e:
                logger.warning(f"Saved account {uid_str} failed to connect: {e}")

    # 3. Start Bot Client
    await bot_client.start(bot_token=BOT_TOKEN)
    me_bot = await bot_client.get_me()
    logger.info(f"✅ Bot Client Online: @{me_bot.username} (ID: {me_bot.id})")
    logger.info(f"👥 Connected Accounts: {len(user_clients)} | 🤖 Bot Known Chats: {len(app_state.get('bot_known_chats', {}))}")
    logger.info("=" * 60)

    await run_client_forever(bot_client, f"Bot:@{me_bot.username}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped.")
