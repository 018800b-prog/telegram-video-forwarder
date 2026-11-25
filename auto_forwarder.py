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
import sys
import html
import json
import time
import shutil
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient, events, Button, utils, functions, types
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
from utils import format_size, is_video_message, clean_chat_input, parse_telegram_link

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
# Per-profile active bulk sync tracking
active_syncs: dict[str, dict] = {}
# Deduplicate dual delivery when both Userbot & Bot see the same source post
recent_dispatched: dict[tuple[str, int, int], float] = {}

def get_active_sync(prof_key: str) -> dict:
    if prof_key not in active_syncs:
        active_syncs[prof_key] = {"running": False, "should_stop": False, "copied": 0, "scanned": 0}
    return active_syncs[prof_key]

# ==================== MEDIA FILTER & FINGERPRINT ====================

def is_eligible_media(message, profile: dict) -> bool:
    if not message or not message.media:
        return False
    m_filter = profile.get("media_filter", "video_only")
    if m_filter == "all_media":
        return True
    # Filter tiny animated reaction GIFs (<300 KB) even if mime is video/mp4
    if getattr(message, "gif", None):
        fsize = getattr(message.file, "size", 0) if message.file else 0
        if fsize and fsize < 300_000:
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

async def upload_file_with_retry(
    upload_client: TelegramClient,
    target_entity,
    file_obj,
    caption,
    entities,
    attributes=None,
    thumb=None,
):
    """Send file with FloodWait retry loop, returns sent message or raises"""
    for attempt in range(3):
        try:
            kwargs = {
                "entity": target_entity,
                "file": file_obj,
                "caption": caption,
                "formatting_entities": entities,
                "supports_streaming": True,
            }
            if attributes:
                kwargs["attributes"] = attributes
            if thumb and os.path.exists(thumb):
                kwargs["thumb"] = thumb
            return await upload_client.send_file(**kwargs)
        except FloodWaitError as flood:
            wait_sec = min(flood.seconds + 2, 120)
            logger.warning(f"FloodWait ({flood.seconds}s): sleeping {wait_sec}s before retry...")
            await asyncio.sleep(wait_sec)
            if attempt == 2:
                raise
    return None

async def distribute_video(
    source_client: TelegramClient,
    message,
    source_chat_id: int,
    source_title: str,
    prof_key: str,
    send_notification: bool = True,
) -> list[str]:
    """
    Distributes eligible video to ALL target groups cleanly:
    - Skips self-loop (source == target)
    - Checks duplicate fingerprint BEFORE uploading, saves ONLY AFTER success
    - Downloads protected videos ONCE and cloud-forwards sent_msg.media to remaining targets
    - Falls back to bot_client if userbot lacks permissions in a bot-admin target
    """
    profile = get_profile(prof_key)
    if not message or not is_eligible_media(message, profile):
        return []

    fp = get_media_fingerprint(message)
    if profile.get("duplicate_protection", True) and fp:
        seen_set = set(profile.get("seen_fingerprints", []))
        if fp in seen_set:
            logger.info(f"🛡️ [{prof_key}] Duplicate detected ({fp}). Skipping!")
            return []

    src_canon = to_canonical_id(source_chat_id)
    raw_targets = profile.get("target_ids", [])
    target_ids = [tid for tid in raw_targets if to_canonical_id(tid) != src_canon]

    if not target_ids:
        logger.warning(f"[{prof_key}] No valid target groups configured (or all match source)!")
        return []

    caption_text, caption_entities = safe_caption_and_entities(
        message, profile.get("caption_mode", "original")
    )

    acc_id = profile.get("active_account_id")
    primary_client = user_clients.get(acc_id) if (acc_id and acc_id in user_clients) else source_client
    source_key = "bot" if source_client is bot_client else f"u_{acc_id or 'src'}"

    # Preserve video metadata + thumbnail if fallback download becomes needed
    video_attrs = None
    if getattr(message, "document", None) and getattr(message.document, "attributes", None):
        video_attrs = [
            a for a in message.document.attributes
            if isinstance(a, (types.DocumentAttributeVideo, types.DocumentAttributeFilename))
        ]

    file_size = getattr(message.file, "size", 0) if message.file else 0
    ext = (getattr(message.file, "ext", None) or ".mp4") if message.file else ".mp4"
    temp_video_path = TEMP_DIR / f"fwd_{src_canon}_{message.id}{ext}"
    temp_thumb_path = TEMP_DIR / f"thumb_{src_canon}_{message.id}.jpg"

    downloaded_path = None
    thumb_path = None
    reusable_cloud_media = None
    reusable_cloud_client = None
    successful_targets = []

    try:
        for tgt_id in target_ids:
            sent_msg = None
            target_title = profile.get("target_titles", {}).get(str(tgt_id), str(tgt_id))

            # Pick preferred sending client (Userbot first unless target is bot-only)
            candidate_clients: list[tuple[TelegramClient, str]] = []
            if primary_client is not None:
                candidate_clients.append((primary_client, f"u_{acc_id or 'prim'}"))
            if bot_client not in [c for c, _ in candidate_clients] and (file_size <= 50_000_000 or not file_size):
                candidate_clients.append((bot_client, "bot"))

            for u_client, u_key in candidate_clients:
                tgt_ent = await get_entity_safe(u_client, tgt_id, u_key)
                if not tgt_ent:
                    continue
                target_title = getattr(tgt_ent, "title", None) or target_title

                # Fast path A: Reuse already-uploaded media from Target #1 on the same client!
                if reusable_cloud_media is not None and reusable_cloud_client is u_client:
                    try:
                        sent_msg = await upload_file_with_retry(
                            u_client, tgt_ent, reusable_cloud_media,
                            caption_text, caption_entities
                        )
                        if sent_msg:
                            break
                    except Exception as e:
                        logger.debug(f"Cloud media reuse failed on {target_title}: {e}")

                # Fast path B: Direct cloud copy if sending client == source client (zero bandwidth)
                if u_client is source_client:
                    try:
                        sent_msg = await upload_file_with_retry(
                            u_client, tgt_ent, message.media,
                            caption_text, caption_entities
                        )
                        if sent_msg:
                            reusable_cloud_media = sent_msg.media
                            reusable_cloud_client = u_client
                            break
                    except (ChatForwardsRestrictedError, ChatWriteForbiddenError, ChannelPrivateError, PeerIdInvalidError) as e:
                        logger.info(f"Direct cloud forward unavailable ({type(e).__name__}) -> checking fallback")
                    except Exception as e:
                        err_s = str(e).lower()
                        if "protected chat" in err_s or "forwards" in err_s or "file reference" in err_s:
                            logger.info(f"Protected/restricted media -> using single download fallback")
                        elif "peer" in err_s or "write" in err_s or "admin" in err_s:
                            continue  # Try next client (e.g., bot_client)
                        else:
                            logger.warning(f"Direct forward error on {u_key}: {e}")

                # Fallback path C: Download ONCE using source_client, then upload
                if downloaded_path is None:
                    try:
                        logger.info(f"📥 Downloading media ({format_size(file_size)}) once for distribution...")
                        dl = await source_client.download_media(message, file=str(temp_video_path))
                        if dl and os.path.exists(dl):
                            downloaded_path = dl
                        if getattr(message, "document", None) and getattr(message.document, "thumbs", None):
                            try:
                                th = await source_client.download_media(message, file=str(temp_thumb_path), thumb=-1)
                                if th and os.path.exists(th):
                                    thumb_path = th
                            except Exception:
                                pass
                    except Exception as dl_err:
                        logger.error(f"Source download failed: {dl_err}")
                        break

                if downloaded_path and os.path.exists(downloaded_path):
                    try:
                        sent_msg = await upload_file_with_retry(
                            u_client, tgt_ent, downloaded_path,
                            caption_text, caption_entities,
                            attributes=video_attrs, thumb=thumb_path
                        )
                        if sent_msg:
                            if reusable_cloud_media is None:
                                reusable_cloud_media = sent_msg.media
                                reusable_cloud_client = u_client
                            break
                    except (ChatWriteForbiddenError, ChannelPrivateError, PeerIdInvalidError) as perm_err:
                        logger.warning(f"{u_key} cannot write to {target_title} ({type(perm_err).__name__}), trying next client...")
                        continue
                    except Exception as up_err:
                        logger.error(f"Fallback upload error to {target_title} on {u_key}: {up_err}")

            if sent_msg:
                successful_targets.append(target_title)
                await asyncio.sleep(0.5)

    finally:
        for fp_del in (downloaded_path, thumb_path, temp_video_path, temp_thumb_path):
            try:
                if fp_del and os.path.exists(fp_del):
                    os.remove(fp_del)
            except Exception:
                pass

    if successful_targets:
        # Mark duplicate fingerprint ONLY after at least 1 upload succeeded!
        if profile.get("duplicate_protection", True) and fp:
            seen_list = profile.get("seen_fingerprints", [])
            if fp not in seen_list:
                seen_list.append(fp)
                if len(seen_list) > 6000:
                    seen_list = seen_list[-5000:]
                profile["seen_fingerprints"] = seen_list

        profile["forwarded_count"] = profile.get("forwarded_count", 0) + 1
        save_state()

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

    # Evict old entries > 120s
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

        # Dispatch to every profile powered by this connected account
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
        except Exception as e:
            logger.error(f"⚠️ {label} connection error ({e}). Reconnecting in {backoff}s...")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)

async def start_user_account(session_str: str, phone: str = "") -> tuple[int, TelegramClient]:
    """Boots a Userbot TelegramClient from StringSession and registers it live"""
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
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

    # Pre-warm dialog cache in background
    asyncio.create_task(scan_account_dialogs(uid, force_refresh=True))
    logger.info(f"✅ Userbot Online: {full_name} (ID: {uid})")
    return uid, client

# ==================== BULK SYNC WORKER ====================

async def run_multi_bulk_sync(prof_key: str, limit_count: int | None, reply_chat_id: int, status_msg_id: int):
    sync_state = get_active_sync(prof_key)
    sync_state["running"] = True
    sync_state["should_stop"] = False
    sync_state["copied"] = 0
    sync_state["scanned"] = 0

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
        return

    if not source_ids or not target_ids:
        await bot_client.edit_message(
            reply_chat_id, status_msg_id,
            "⚠️ সোর্স এবং টার্গেট উভয় গ্রুপ সিলেক্ট থাকতে হবে!",
            buttons=[[Button.inline("🔙 মূল মেন্যু", f"home:{prof_key}".encode())]]
        )
        sync_state["running"] = False
        return

    copied = 0
    scanned = 0
    last_edit = 0.0

    try:
        for sid in source_ids:
            if sync_state["should_stop"]:
                break

            s_entity = await get_entity_safe(src_client, sid, f"u_{acc_id}")
            if not s_entity:
                continue

            s_title = profile.get("source_titles", {}).get(str(sid), getattr(s_entity, "title", str(sid)))

            # Iterate ALL messages (never cut off at 50 messages!) until we copy `limit_count` eligible videos
            async for msg in src_client.iter_messages(s_entity, limit=None, reverse=True):
                if sync_state["should_stop"]:
                    break
                scanned += 1
                sync_state["scanned"] = scanned

                if is_eligible_media(msg, profile):
                    res = await distribute_video(
                        src_client, msg, sid, s_title, prof_key, send_notification=False
                    )
                    if res:
                        copied += 1
                        sync_state["copied"] = copied
                        await asyncio.sleep(0.8)

                    if limit_count and copied >= limit_count:
                        break

                now = time.monotonic()
                if (copied % 5 == 0 or scanned % 40 == 0) and (now - last_edit > 6) and status_msg_id:
                    last_edit = now
                    try:
                        await bot_client.edit_message(
                            reply_chat_id,
                            status_msg_id,
                            f"🔄 <b>মাল্টি-সিঙ্ক চলছে (Multi-Sync Active)...</b>\n\n"
                            f"📥 সোর্স: <code>{len(source_ids)}</code> টি | 📤 টার্গেট: <code>{len(target_ids)}</code> টি\n"
                            f"🎬 বর্তমান চ্যাট: <b>{h(s_title)}</b>\n"
                            f"🔎 স্ক্যান করা মেসেজ: <code>{scanned}</code> টি\n"
                            f"✅ নতুন ফরোয়ার্ড: <code>{copied}</code> টি ভিডিও\n\n"
                            f"⏳ ক্লাউডে ব্যাকগ্রাউন্ডে চলছে...",
                            buttons=[[Button.inline("🛑 সিঙ্ক থামান (Stop)", f"stop:{prof_key}".encode())]]
                        )
                    except MessageNotModifiedError:
                        pass
                    except Exception:
                        pass

            if limit_count and copied >= limit_count:
                break

        status_hdr = "🛑 <b>সিঙ্ক বন্ধ করা হয়েছে!</b>" if sync_state["should_stop"] else "🎉 <b>মাল্টি-সিঙ্ক সফলভাবে সম্পন্ন হয়েছে!</b>"
        await bot_client.edit_message(
            reply_chat_id,
            status_msg_id,
            f"{status_hdr}\n\n"
            f"🔎 মোট স্ক্যান: <code>{scanned}</code> টি মেসেজ\n"
            f"✅ নতুন ফরোয়ার্ড হয়েছে: <code>{copied}</code> টি ভিডিও\n"
            f"📊 সর্বমোট ফরোয়ার্ড: <code>{profile.get('forwarded_count', 0)}</code> টি",
            buttons=[[Button.inline("🔙 মূল ড্যাশবোর্ড", f"home:{prof_key}".encode())]]
        )
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
    finally:
        sync_state["running"] = False

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

# ==================== ACCOUNT MANAGER & IN-BOT LOGIN NUMPAD ====================

def build_numpad_text(code_digits: str, phone: str) -> str:
    padded = (code_digits + "_____")[:5]
    spaced = " ".join(list(padded))
    return (
        f"🔐 <b>টেলিগ্রাম ভেরিফিকেশন কোড দিন</b>\n\n"
        f"📱 নম্বর: <code>{h(phone)}</code>\n"
        f"আপনার টেলিগ্রাম অ্যাপে ৫ ডিজিটের একটি কোড পাঠানো হয়েছে।\n\n"
        f"⚠️ <i>নিরাপত্তার জন্য কোডটি সরাসরি চ্যাটে না লিখে নিচের বাটনে চাপ দিয়ে বসান:</i>\n\n"
        f"📝 কোড: <code>[ {spaced} ]</code>"
    )

def build_numpad_buttons(code_digits: str):
    return [
        [Button.inline("1️⃣", b"num:1"), Button.inline("2️⃣", b"num:2"), Button.inline("3️⃣", b"num:3")],
        [Button.inline("4️⃣", b"num:4"), Button.inline("5️⃣", b"num:5"), Button.inline("6️⃣", b"num:6")],
        [Button.inline("7️⃣", b"num:7"), Button.inline("8️⃣", b"num:8"), Button.inline("9️⃣", b"num:9")],
        [Button.inline("⌫ মুছুন", b"num:del"), Button.inline("0️⃣", b"num:0"), Button.inline("✅ কনফার্ম", b"num:ok")],
        [Button.inline("❌ বাতিল করুন", b"num:cancel")],
    ]

async def start_inbot_login_prompt(reply_fn, sender_id: int, prof_key: str):
    pending_logins[sender_id] = {
        "step": "WAIT_PHONE",
        "prof_key": prof_key,
        "created_at": time.monotonic(),
        "digits": "",
    }
    await reply_fn(
        "📲 <b>আপনার টেলিগ্রাম অ্যাকাউন্ট কানেক্ট করুন</b>\n\n"
        "এই অ্যাকাউন্টের সকল প্রাইভেট ও পাবলিক গ্রুপ স্ক্যান করতে আপনার ফোন নম্বর দিন।\n\n"
        "👉 নিচের <b>📱 আমার ফোন নম্বর দিন</b> বাটনে চাপুন অথবা দেশ কোডসহ নম্বর লিখুন (যেমন: <code>+88017xxxxxxxx</code> বা <code>017xxxxxxxx</code>):",
        buttons=[
            [Button.request_phone("📱 আমার ফোন নম্বর দিন", resize=True, single_use=True)],
            [Button.text("❌ বাতিল করুন", resize=True)],
        ]
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

    # Keep client live in user_clients pool!
    if new_uid not in user_clients:
        user_clients[new_uid] = tmp_client
        attach_userbot_listeners(tmp_client, new_uid)
        asyncio.create_task(run_client_forever(tmp_client, f"User:{new_uid}"))
    else:
        await tmp_client.disconnect()

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

# ==================== BOT CALLBACK HANDLER ====================

@bot_client.on(events.CallbackQuery)
async def bot_callback_handler(event):
    if not await is_operator_authorized(event):
        await event.answer("⚠️ শুধুমাত্র অ্যাডমিন বা অনুমোদিত ব্যবহারকারী এটি নিয়ন্ত্রণ করতে পারবেন!", alert=True)
        return

    raw_data = event.data.decode("utf-8", errors="ignore")

    # 1. Handle Inline Login Numpad first
    if raw_data.startswith("num:"):
        uid = event.sender_id
        st = pending_logins.get(uid)
        if not st or st.get("step") != "WAIT_OTP":
            await event.answer("⚠️ লগইন সেশন শেষ হয়ে গেছে। আবার /login দিন।", alert=True)
            return

        action = raw_data.split(":", 1)[1]
        if action == "cancel":
            try:
                await st["client"].disconnect()
            except Exception:
                pass
            pending_logins.pop(uid, None)
            await event.answer("❌ বাতিল করা হয়েছে", alert=False)
            await safe_edit_message(event, "❌ অ্যাকাউন্ট লগইন বাতিল করা হয়েছে।", buttons=[[Button.inline("🔙 ড্যাশবোর্ড", f"home:{st['prof_key']}".encode())]])
            return

        if action == "del":
            st["digits"] = st["digits"][:-1]
            await event.answer()
            await safe_edit_message(event, build_numpad_text(st["digits"], st["phone"]), buttons=build_numpad_buttons(st["digits"]))
            return

        if action.isdigit():
            if len(st["digits"]) < 5:
                st["digits"] += action
                await event.answer()
                await safe_edit_message(event, build_numpad_text(st["digits"], st["phone"]), buttons=build_numpad_buttons(st["digits"]))
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
                await safe_edit_message(event, "❌ <b>কোডটি ভুল ছিল!</b> আবার সঠিক কোড চাপুন:\n\n" + build_numpad_text("", st["phone"]), buttons=build_numpad_buttons(""))
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

    if raw_data == "noop":
        await event.answer()
        return

    parts = raw_data.split(":")
    cmd = parts[0]
    prof_key = parts[1] if len(parts) > 1 else str(event.chat_id)
    profile = get_profile(prof_key)

    if cmd == "home":
        await event.answer()
        await safe_edit_message(event, get_main_menu_text(prof_key), buttons=get_main_menu_buttons(prof_key, is_private=event.is_private))
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
        sync_state = get_active_sync(prof_key)
        if sync_state["running"]:
            sync_state["should_stop"] = True
            await event.answer("🛑 সিঙ্ক থামানো হচ্ছে...", alert=True)
        else:
            await event.answer("কোনো সিঙ্ক চলছে না।", alert=True)
            await safe_edit_message(event, get_main_menu_text(prof_key), buttons=get_main_menu_buttons(prof_key, is_private=event.is_private))
        return

    if cmd == "sync":
        sync_state = get_active_sync(prof_key)
        if sync_state["running"]:
            await event.answer("⚠️ ইতিমধ্যেই একটি সিঙ্ক চলছে!", alert=True)
            return
        limit_val = int(parts[2]) if len(parts) > 2 else 0
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
        asyncio.create_task(run_multi_bulk_sync(prof_key, limit_count, event.chat_id, msg_id))
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
                await event.reply("⚠️ সঠিক ফোন নম্বর দিন (যেমন: <code>+88017xxxxxxxx</code> অথবা <code>017xxxxxxxx</code>):")
                return

            await event.reply("✅ নম্বর গ্রহণ করা হয়েছে! কোড পাঠানো হচ্ছে...", buttons=Button.clear())
            wait_msg = await bot_client.send_message(event.chat_id, "⏳ টেলিগ্রাম সার্ভারে রিকোয়েস্ট পাঠানো হচ্ছে...")
            tmp_client = TelegramClient(StringSession(), API_ID, API_HASH)
            try:
                await tmp_client.connect()
                sent = await tmp_client.send_code_request(phone)
                st["step"] = "WAIT_OTP"
                st["phone"] = phone
                st["client"] = tmp_client
                st["hash"] = sent.phone_code_hash
                st["digits"] = ""
                await bot_client.edit_message(
                    event.chat_id,
                    wait_msg.id,
                    build_numpad_text("", phone),
                    buttons=build_numpad_buttons("")
                )
            except Exception as e:
                await tmp_client.disconnect()
                pending_logins.pop(event.sender_id, None)
                await bot_client.edit_message(event.chat_id, wait_msg.id, f"❌ কোড পাঠানো যায়নি: <code>{h(e)}</code>")
            return

        if st["step"] == "WAIT_OTP":
            # Allow typed code with separators/spaces like "1 2 3 4 5" or "12-345" as backup to numpad
            digits = re.sub(r"\D", "", raw_text)
            if len(digits) == 5:
                tmp = st["client"]
                try:
                    await tmp.sign_in(phone=st["phone"], code=digits, phone_code_hash=st["hash"])
                    await complete_inbot_login(event.sender_id, st, event.chat_id)
                except SessionPasswordNeededError:
                    st["step"] = "WAIT_2FA"
                    await event.reply("🔒 আপনার অ্যাকাউন্টের <b>2FA পাসওয়ার্ড</b> লিখুন (পড়ার পর মেসেজটি ডিলিট করে দেওয়া হবে):")
                except Exception as e:
                    await event.reply(f"❌ কোড সঠিক নয়: <code>{h(e)}</code>\nনিচের বাটনে কোডটি চাপুন।")
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
                    s_title = profile.get("source_titles", {}).get(str(event.chat_id), "Channel")
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
    print("=" * 60)
    print("🚀 Telegram Multi-Account & Channel-Aware Forwarder Online")
    print("=" * 60)

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
    print(f"✅ Bot Client Online: @{me_bot.username} (ID: {me_bot.id})")
    print(f"👥 Connected Accounts: {len(user_clients)} | 🤖 Bot Known Chats: {len(app_state.get('bot_known_chats', {}))}")
    print("=" * 60)

    await run_client_forever(bot_client, f"Bot:@{me_bot.username}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped.")
