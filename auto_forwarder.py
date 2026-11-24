"""
Telegram Master Auto-Forwarder (Hybrid Dual-Engine)
===================================================
Engine 1: User Client (Telethon with SESSION_STRING)
          - Can access all private groups, invite links, restricted content
          - Listens to active source groups in real-time
          - Copies videos to target group (🫦🫦🫦) without forward tags

Engine 2: Bot Client (@autforwbdbot)
          - Provides an interactive visual menu with buttons directly in Telegram
          - Lets you select source groups with 1 click from your joined dialogs
          - Syncs past videos on demand with progress updates
          - Sends instant notifications when videos are auto-forwarded
"""

import os
import sys
import json
import asyncio
import logging
from pathlib import Path
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, PeerChannel, PeerChat
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.errors import UserAlreadyParticipantError, FloodWaitError

from config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    BASE_DIR,
    LOG_FORMAT
)
from utils import format_size, generate_caption, is_video_message, clean_chat_input

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("MasterForwarder")

TEMP_DIR = BASE_DIR / "temp_media"
TEMP_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = BASE_DIR / "settings.json"

# Default fallback settings
DEFAULT_SETTINGS = {
    "target_id": -1004402901248,  # 🫦🫦🫦
    "target_title": "🫦🫦🫦",
    "source_id": -1004495534028,  # 🍑🍑🍑🍑
    "source_title": "🍑🍑🍑🍑",
    "source_ids": [-1004495534028],
    "caption_mode": "original",    # "original" | "none"
    "media_filter": "video_only",  # "video_only" | "all_media"
    "auto_forward_enabled": True,  # True | False
    "forwarded_count": 1,
    "owner_id": 8721309931
}

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**DEFAULT_SETTINGS, **data}
        except Exception as e:
            logger.error(f"Error loading settings: {e}")
    return DEFAULT_SETTINGS.copy()

def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving settings: {e}")

settings = load_settings()

def to_canonical_id(chat_id) -> int:
    """Normalize any chat ID format (-100xxxx, -xxxx, xxxx) to plain integer"""
    if not chat_id:
        return 0
    s = str(chat_id).strip()
    if s.startswith("-100"):
        return int(s[4:])
    if s.startswith("-"):
        return int(s[1:])
    return int(s)

def get_active_source_canonical_ids() -> set[int]:
    ids = set()
    s_id = settings.get("source_id")
    if s_id:
        ids.add(to_canonical_id(s_id))
    for sid in settings.get("source_ids", []):
        ids.add(to_canonical_id(sid))
    return ids

# Initialize Clients
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
if not SESSION_STRING:
    # Try reading from .env directly
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
    SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

if not SESSION_STRING:
    logger.error("SESSION_STRING is missing! Please login first via login.bat")
    sys.exit(1)

BOT_SESSION_STRING = os.getenv(
    "BOT_SESSION_STRING",
    "1BVtsOHEBu4SgSeGUhFYOYVSIjlZB2m_DrLe39cVmVULZcfRMcyoNjWq6BnomwbtrDY8crG4_kFj9nmE3zgOMlESW5zJoop1d6SppNKYR4Odjnf6xVGGM1086cPuVNeSvYPW0yT6h2tpBkv0uNsYJtwlp1qC8ob-0mKruMwX6y2vY_LPHSop8wXzuvP58VYpbnYjZqcyi2AKzuwrEIb926-ravhUQ_BsB34cLjWoz7xt4wkaRLlhmb2YsaMTVSsxpqZQ2QnsJ4jfCAxelHXXx7Wme3d_dZnNAsXGAlEplRHYII2VCRenyVU1XydwTI7S0CxWuIPVPU6hKGZLdDr8UgNYg8dEP9to="
).strip()

user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
bot_client = TelegramClient(StringSession(BOT_SESSION_STRING), API_ID, API_HASH)

# Cached Entities
cached_target = {"entity": None}

async def get_target_entity():
    """Ensure target entity is resolved and valid"""
    if cached_target["entity"]:
        return cached_target["entity"]
    try:
        target_id = settings.get("target_id", -1004402901248)
        entity = await user_client.get_entity(target_id)
        cached_target["entity"] = entity
        return entity
    except Exception as e:
        logger.error(f"Failed to resolve target entity: {e}")
        return None

async def forward_video_core(message, source_name="Source") -> bool:
    """Core function that uploads video to target cleanly with FloodWait auto-recovery"""
    target = await get_target_entity()
    if not target:
        logger.error("Target entity is not ready!")
        return False

    file_size = getattr(message.file, "size", 0) if message.file else 0
    caption_mode = settings.get("caption_mode", "original")
    final_caption = message.text if caption_mode == "original" else None

    # Retry loop for Telegram rate limits
    for attempt in range(3):
        try:
            # Strategy 1: Instant Cloud Forward (0 bandwidth)
            logger.info(f"📤 Direct Cloud Uploading video ({format_size(file_size)}) to target...")
            await user_client.send_file(
                entity=target,
                file=message.media,
                caption=final_caption,
                supports_streaming=True
            )
            settings["forwarded_count"] = settings.get("forwarded_count", 0) + 1
            save_settings(settings)
            logger.info("✅ Video forwarded successfully via cloud!")
            return True

        except FloodWaitError as flood:
            wait_sec = flood.seconds + 2
            logger.warning(f"Telegram FloodWait encountered: sleeping for {wait_sec}s...")
            await asyncio.sleep(wait_sec)
        except Exception as e:
            logger.warning(f"Cloud forward failed ({e}). Attempting download & upload fallback...")
            try:
                temp_path = TEMP_DIR / f"fwd_{message.id}.mp4"
                downloaded = await user_client.download_media(message, file=str(temp_path))
                if not downloaded or not os.path.exists(downloaded):
                    raise Exception("Download failed")

                await user_client.send_file(
                    entity=target,
                    file=downloaded,
                    caption=final_caption,
                    supports_streaming=True
                )
                try:
                    if os.path.exists(downloaded):
                        os.remove(downloaded)
                except Exception:
                    pass

                settings["forwarded_count"] = settings.get("forwarded_count", 0) + 1
                save_settings(settings)
                logger.info("✅ Video forwarded successfully via fallback!")
                return True
            except FloodWaitError as flood_fb:
                wait_sec = flood_fb.seconds + 2
                logger.warning(f"FloodWait on fallback: sleeping for {wait_sec}s...")
                await asyncio.sleep(wait_sec)
            except Exception as err:
                logger.error(f"Fallback forward error: {err}")
                return False
    return False

def is_eligible_media(message) -> bool:
    """Checks if message matches the active media filter"""
    m_filter = settings.get("media_filter", "video_only")
    if m_filter == "all_media":
        return bool(message and message.media)
    return is_video_message(message)

# ==================== REAL-TIME LISTENER (USER CLIENT) ====================

@user_client.on(events.NewMessage)
async def on_user_new_message(event):
    if not event.chat_id:
        return

    # Check if live auto-forwarding is enabled
    if not settings.get("auto_forward_enabled", True):
        return

    canonical_chat_id = to_canonical_id(event.chat_id)
    active_sources = get_active_source_canonical_ids()

    if canonical_chat_id in active_sources:
        if is_eligible_media(event.message):
            source_title = settings.get("source_title", "Source Group")
            file_size = getattr(event.message.file, "size", 0) if event.message.file else 0
            logger.info(f"🚨 New video in [{source_title}] ({format_size(file_size)})! Auto-forwarding...")

            success = await forward_video_core(event.message, source_title)
            
            # Send notification to Owner via Bot
            if success:
                owner_id = settings.get("owner_id", 8721309931)
                try:
                    await bot_client.send_message(
                        owner_id,
                        f"🎬 **নতুন ভিডিও সফলভাবে ফরোয়ার্ড হয়েছে!**\n\n"
                        f"📥 **সোর্স:** {source_title}\n"
                        f"📤 **টার্গেট:** {settings.get('target_title', '🫦🫦🫦')}\n"
                        f"📁 **সাইজ:** {format_size(file_size)}\n"
                        f"📊 **সর্বমোট ফরোয়ার্ড:** {settings.get('forwarded_count', 0)} টি"
                    )
                except Exception as notif_err:
                    logger.warning(f"Could not send bot notification: {notif_err}")

# ==================== INTERACTIVE BOT UI (100% BUTTON DRIVEN) ====================

def get_main_menu_text() -> str:
    s_title = settings.get("source_title", "কোনোটি নির্বাচিত নেই")
    t_title = settings.get("target_title", "🫦🫦🫦")
    f_count = settings.get("forwarded_count", 0)
    c_mode = "আসল ক্যাপশন" if settings.get("caption_mode") == "original" else "ক্লিন (ক্যাপশন ছাড়া)"
    m_filter = "শুধুমাত্র ভিডিও" if settings.get("media_filter") == "video_only" else "সব মিডিয়া (ভিডিও+ফটো)"
    auto_status = "🟢 চালু আছে" if settings.get("auto_forward_enabled", True) else "🔴 বন্ধ আছে"

    return (
        "🤖 **ভিডিও অটো-ফরোয়ার্ডার কন্ট্রোল প্যানেল** 🚀\n\n"
        f"📥 **সোর্স গ্রুপ:** **{s_title}**\n"
        f"📤 **টার্গেট গ্রুপ:** **{t_title}**\n"
        f"⚡ **লাইভ অটো-ফরোয়ার্ড:** {auto_status}\n"
        f"📝 **ক্যাপশন স্টাইল:** {c_mode}\n"
        f"🎥 **মিডিয়া ফিল্টার:** {m_filter}\n"
        f"📊 **সর্বমোট ফরোয়ার্ড ভিডিও:** `{f_count}` টি\n\n"
        "👇 **কোনো কিছু টাইপ করতে হবে না! নিচের যেকোনো বোতামে চাপুন:**"
    )

def get_main_menu_buttons():
    if active_sync["running"]:
        return [
            [Button.inline(f"🛑 সিঙ্ক চলছে ({active_sync['copied']}টি কপি)... থামাতে চাপুন", b"stop_sync")],
            [Button.inline("🔄 প্রগ্রেস রিফ্রেশ করুন", b"refresh_status")]
        ]
    return [
        [Button.inline("📋 সোর্স গ্রুপ নির্বাচন করুন (My Groups)", b"list_groups_0")],
        [Button.inline("🚀 সোর্স থেকে সব ভিডিও ফরোয়ার্ড করুন (All 1000+)", b"sync_all")],
        [
            Button.inline("🔄 ৫০টি কপি", b"sync_50"),
            Button.inline("🔄 ২০০টি কপি", b"sync_200"),
            Button.inline("🔄 ৫০০টি কপি", b"sync_500")
        ],
        [
            Button.inline("📤 টার্গেট গ্রুপ বাছুন", b"list_targets_0"),
            Button.inline("⚙️ সেটিংস ও ফিল্টার", b"open_settings")
        ],
        [Button.inline("🔄 স্ট্যাটাস রিফ্রেশ", b"refresh_status")]
    ]

def get_source_hub_text(title: str, chat_id: int) -> str:
    t_title = settings.get("target_title", "🫦🫦🫦")
    live_status = "🟢 চালু (নতুন ভিডিও আসামাত্রই যাবে)" if settings.get("auto_forward_enabled", True) else "🔴 বন্ধ"
    return (
        f"🎉 **সোর্স গ্রুপ সফলভাবে নির্বাচন করা হয়েছে!**\n\n"
        f"👉 নির্বাচন করেছেন: **【 {title} 】** (`{chat_id}`)\n"
        f"📤 ভিডিও জমা হবে: **{t_title}**\n"
        f"⚡ লাইভ অটো-ফরোয়ার্ড: {live_status}\n\n"
        "👇 **এখনই পুরোনো ভিডিও নিয়ে আসতে নিচের বোতামে চাপুন:**"
    )

def get_source_hub_buttons(title: str):
    return [
        [Button.inline(f"🚀 সব ভিডিও ফরোয়ার্ড করুন (All 1000+)", b"sync_all")],
        [
            Button.inline("🔄 ৫০টি কপি", b"sync_50"),
            Button.inline("🔄 ২০০টি কপি", b"sync_200"),
            Button.inline("🔄 ৫০০টি কপি", b"sync_500")
        ],
        [
            Button.inline("📋 অন্য সোর্স গ্রুপ বাছুন", b"list_groups_0"),
            Button.inline("⚙️ সেটিংস পরিবর্তন", b"open_settings")
        ],
        [Button.inline("🔙 মূল ড্যাশবোর্ড", b"refresh_status")]
    ]

def get_settings_text() -> str:
    c_mode = "আসল ক্যাপশন রাখা হবে" if settings.get("caption_mode") == "original" else "ক্লিন (ক্যাপশন মুছে ফেলা হবে)"
    m_filter = "শুধুমাত্র ভিডিও" if settings.get("media_filter") == "video_only" else "সব মিডিয়া (ভিডিও + ফটো)"
    auto_status = "🟢 চালু (সোর্স গ্রুপে নতুন ভিডিও এলেই যাবে)" if settings.get("auto_forward_enabled", True) else "🔴 বন্ধ"

    return (
        "⚙️ **অটো-ফরোয়ার্ডার সেটিংস ও ফিল্টার কন্ট্রোল**\n\n"
        f"⚡ **লাইভ অটো-ফরোয়ার্ড:** {auto_status}\n"
        f"📝 **ক্যাপশন স্টাইল:** {c_mode}\n"
        f"🎥 **মিডিয়া ফিল্টার:** {m_filter}\n\n"
        "👇 পরিবর্তন করতে নিচের বোতামগুলোতে চাপুন:"
    )

def get_settings_buttons():
    auto_btn = "🔴 অটো-ফরোয়ার্ড বন্ধ করুন" if settings.get("auto_forward_enabled", True) else "🟢 অটো-ফরোয়ার্ড চালু করুন"
    cap_btn = "🧹 ক্লিন মোড (ক্যাপশন মুছুন)" if settings.get("caption_mode") == "original" else "📄 আসল ক্যাপশন রাখুন"
    flt_btn = "📁 সব মিডিয়া পাঠান (ভিডিও+ফটো)" if settings.get("media_filter") == "video_only" else "🎥 শুধুমাত্র ভিডিও পাঠান"

    return [
        [Button.inline(auto_btn, b"toggle_auto_fwd")],
        [Button.inline(cap_btn, b"toggle_caption")],
        [Button.inline(flt_btn, b"toggle_media_filter")],
        [Button.inline("🔙 মূল মেন্যুতে ফিরে যান", b"refresh_status")]
    ]

active_sync = {
    "running": False,
    "should_stop": False,
    "source_id": None,
    "source_title": "",
    "copied": 0,
    "chat_id": None,
    "msg_id": None
}

async def run_bulk_sync(source_id, source_title, limit_count, reply_chat_id, status_msg_id):
    """Background Worker that syncs 100 to 3,000+ videos continuously"""
    active_sync["running"] = True
    active_sync["should_stop"] = False
    active_sync["source_id"] = source_id
    active_sync["source_title"] = source_title
    active_sync["copied"] = 0
    active_sync["chat_id"] = reply_chat_id
    active_sync["msg_id"] = status_msg_id

    try:
        source_entity = await user_client.get_entity(source_id)
        target_title = settings.get("target_title", "🫦🫦🫦")
        
        limit_desc = f"{limit_count}টি" if limit_count else "সবগুলো (All 1000+)"
        logger.info(f"🚀 Starting background bulk sync from [{source_title}] (Limit: {limit_desc})...")

        copied = 0
        found = 0
        last_edit_time = 0

        # If limit_count is None or >= 1000, we iterate all messages in the channel!
        # reverse=True fetches from oldest to newest so they appear in original chronological order
        iter_limit = limit_count if (limit_count and limit_count < 1000) else None

        async for msg in user_client.iter_messages(source_entity, limit=iter_limit, reverse=True):
            if active_sync["should_stop"]:
                logger.info("🛑 Bulk sync cancelled by user.")
                break

            if is_video_message(msg):
                found += 1
                ok = await forward_video_core(msg, source_title)
                if ok:
                    copied += 1
                    active_sync["copied"] = copied
                    await asyncio.sleep(1.2)  # Safe delay between uploads

                # Update progress in Telegram every 5 videos or every 8 seconds
                now = asyncio.get_event_loop().time()
                if (copied % 5 == 0 or now - last_edit_time > 8) and status_msg_id:
                    last_edit_time = now
                    try:
                        progress_text = (
                            f"🔄 **বাল্ক সিঙ্ক পুরোদমে চলছে (Bulk Sync Active)...**\n\n"
                            f"📥 **সোর্স:** {source_title}\n"
                            f"📤 **টার্গেট:** {target_title}\n"
                            f"📊 **কপি সম্পন্ন:** `{copied}` টি ভিডিও\n"
                            f"⏳ ক্লাউড সার্ভারে একা একাই চলছে (পিসি বন্ধ থাকলেও চলবে)\n\n"
                            f"যেকোনো সময় থামাতে নিচের বাটনে চাপুন:"
                        )
                        await bot_client.edit_message(
                            reply_chat_id,
                            status_msg_id,
                            progress_text,
                            buttons=[[Button.inline("🛑 সিঙ্ক থামান (Stop Sync)", b"stop_sync")]]
                        )
                    except Exception:
                        pass

                if limit_count and copied >= limit_count:
                    break

        final_status = "🛑 **সিঙ্ক ব্যবহারকারী দ্বারা থামানো হয়েছে!**" if active_sync["should_stop"] else "🎉 **বাল্ক সিঙ্ক সফলভাবে সম্পূর্ণ হয়েছে!**"
        final_text = (
            f"{final_status}\n\n"
            f"📥 **সোর্স:** {source_title}\n"
            f"📤 **টার্গেট:** {target_title}\n"
            f"✅ **মোট কপি করা হয়েছে:** `{copied}` টি ভিডিও\n"
            f"📊 **সর্বমোট ফরোয়ার্ড সংখ্যা:** `{settings.get('forwarded_count', 0)}` টি"
        )
        await bot_client.edit_message(
            reply_chat_id,
            status_msg_id,
            final_text,
            buttons=[[Button.inline("🔙 মূল মেন্যু", b"refresh_status")]]
        )

    except Exception as e:
        logger.error(f"Bulk sync error: {e}")
        try:
            await bot_client.edit_message(
                reply_chat_id,
                status_msg_id,
                f"❌ সিঙ্ক চলাকালীন সমস্যা হয়েছে: `{e}`\n(কপি হয়েছিল: {copied} টি)",
                buttons=[[Button.inline("🔙 মূল মেন্যু", b"refresh_status")]]
            )
        except Exception:
            pass
    finally:
        active_sync["running"] = False

@bot_client.on(events.NewMessage(pattern=r"^/(start|menu)$"))
async def bot_start_handler(event):
    if not event.is_private:
        return
    await event.reply(get_main_menu_text(), buttons=get_main_menu_buttons())

@bot_client.on(events.CallbackQuery)
async def bot_callback_handler(event):
    data = event.data

    # Stop active sync
    if data == b"stop_sync":
        if active_sync["running"]:
            active_sync["should_stop"] = True
            await event.answer("🛑 সিঙ্ক থামানো হচ্ছে... অপেক্ষা করুন!", alert=True)
        else:
            await event.answer("কোনো সিঙ্ক চালু নেই।", alert=True)
            await event.edit(get_main_menu_text(), buttons=get_main_menu_buttons())
        return

    # Refresh main dashboard
    if data == b"refresh_status":
        await event.edit(get_main_menu_text(), buttons=get_main_menu_buttons())
        return

    # Open Settings Submenu
    if data == b"open_settings":
        await event.edit(get_settings_text(), buttons=get_settings_buttons())
        return

    # Toggle live auto-forwarding
    if data == b"toggle_auto_fwd":
        curr = settings.get("auto_forward_enabled", True)
        settings["auto_forward_enabled"] = not curr
        save_settings(settings)
        await event.edit(get_settings_text(), buttons=get_settings_buttons())
        return

    # Toggle Caption Mode
    if data == b"toggle_caption":
        curr = settings.get("caption_mode", "original")
        settings["caption_mode"] = "none" if curr == "original" else "original"
        save_settings(settings)
        await event.edit(get_settings_text(), buttons=get_settings_buttons())
        return

    # Toggle Media Filter (Video only vs All media)
    if data == b"toggle_media_filter":
        curr = settings.get("media_filter", "video_only")
        settings["media_filter"] = "all_media" if curr == "video_only" else "video_only"
        save_settings(settings)
        await event.edit(get_settings_text(), buttons=get_settings_buttons())
        return

    # List Source Groups with Pagination
    if data.startswith(b"list_groups_"):
        page = int(data.decode().replace("list_groups_", ""))
        await event.edit(f"🔍 আপনার গ্রুপ ও চ্যানেলসমূহ লোড হচ্ছে (পেজ {page + 1})...")
        
        all_dialogs = []
        try:
            target_id = settings.get("target_id")
            async for d in user_client.iter_dialogs():
                if (d.is_channel or d.is_group) and d.id != target_id:
                    all_dialogs.append((d.name[:24], d.id))

            # 8 groups per page
            per_page = 8
            total_pages = max(1, (len(all_dialogs) + per_page - 1) // per_page)
            page = max(0, min(page, total_pages - 1))
            current_slice = all_dialogs[page * per_page : (page + 1) * per_page]

            buttons = []
            for name, cid in current_slice:
                buttons.append([Button.inline(f"👉 {name}", f"set_src_{cid}".encode())])

            # Pagination row
            nav_row = []
            if page > 0:
                nav_row.append(Button.inline("⬅️ আগের পেজ", f"list_groups_{page - 1}".encode()))
            nav_row.append(Button.inline(f"📄 {page + 1}/{total_pages}", b"refresh_status"))
            if page < total_pages - 1:
                nav_row.append(Button.inline("পরের পেজ ➡️", f"list_groups_{page + 1}".encode()))
            
            if nav_row:
                buttons.append(nav_row)

            buttons.append([Button.inline("🔙 মূল ড্যাশবোর্ড", b"refresh_status")])

            await event.edit(
                "📋 **যে গ্রুপ থেকে ভিডিও ফরোয়ার্ড করতে চান, সেটির ওপর চাপুন:**",
                buttons=buttons
            )
        except Exception as e:
            logger.error(f"Error listing dialogs: {e}")
            await event.edit(f"❌ গ্রুপ লোড করতে সমস্যা: `{e}`", buttons=[[Button.inline("🔙 ফিরে যান", b"refresh_status")]])
        return

    # Set Source Group -> Immediately shows Action Hub with Big "Forward All" Button!
    if data.startswith(b"set_src_"):
        chat_id_str = data.decode().replace("set_src_", "")
        chat_id = int(chat_id_str)
        
        try:
            entity = await user_client.get_entity(chat_id)
            title = getattr(entity, 'title', str(chat_id))
            
            settings["source_id"] = chat_id
            settings["source_title"] = title
            settings["source_ids"] = [chat_id]
            save_settings(settings)

            # Show the immediate Source Action Hub with the Big "Forward All" button
            await event.edit(
                get_source_hub_text(title, chat_id),
                buttons=get_source_hub_buttons(title)
            )
        except Exception as e:
            logger.error(f"Error selecting source: {e}")
            await event.edit(f"❌ সোর্স সেট করতে সমস্যা: `{e}`", buttons=[[Button.inline("🔙 ফিরে যান", b"refresh_status")]])
        return

    # List Target Groups with Pagination
    if data.startswith(b"list_targets_"):
        page = int(data.decode().replace("list_targets_", ""))
        await event.edit(f"🔍 আপনার গ্রুপসমূহ লোড হচ্ছে (পেজ {page + 1})...")

        all_dialogs = []
        try:
            async for d in user_client.iter_dialogs():
                if d.is_channel or d.is_group:
                    all_dialogs.append((d.name[:24], d.id))

            per_page = 8
            total_pages = max(1, (len(all_dialogs) + per_page - 1) // per_page)
            page = max(0, min(page, total_pages - 1))
            current_slice = all_dialogs[page * per_page : (page + 1) * per_page]

            buttons = []
            for name, cid in current_slice:
                buttons.append([Button.inline(f"🎯 {name}", f"set_tgt_{cid}".encode())])

            nav_row = []
            if page > 0:
                nav_row.append(Button.inline("⬅️ আগের পেজ", f"list_targets_{page - 1}".encode()))
            nav_row.append(Button.inline(f"📄 {page + 1}/{total_pages}", b"refresh_status"))
            if page < total_pages - 1:
                nav_row.append(Button.inline("পরের পেজ ➡️", f"list_targets_{page + 1}".encode()))
            
            if nav_row:
                buttons.append(nav_row)

            buttons.append([Button.inline("🔙 মূল ড্যাশবোর্ড", b"refresh_status")])

            await event.edit(
                "📤 **যে গ্রুপে ভিডিওগুলো জমা হবে (টার্গেট গ্রুপ), সেটির ওপর চাপুন:**",
                buttons=buttons
            )
        except Exception as e:
            logger.error(f"Error listing target dialogs: {e}")
            await event.edit(f"❌ লোড করতে সমস্যা: `{e}`", buttons=[[Button.inline("🔙 ফিরে যান", b"refresh_status")]])
        return

    # Set Target Group
    if data.startswith(b"set_tgt_"):
        chat_id_str = data.decode().replace("set_tgt_", "")
        chat_id = int(chat_id_str)
        try:
            entity = await user_client.get_entity(chat_id)
            title = getattr(entity, 'title', str(chat_id))
            
            settings["target_id"] = chat_id
            settings["target_title"] = title
            cached_target["entity"] = None
            save_settings(settings)

            await event.edit(
                f"✅ **টার্গেট গ্রুপ সফলভাবে পরিবর্তিত হয়েছে!**\n\n"
                f"📤 নতুন টার্গেট: **【 {title} 】** (`{chat_id}`)\n"
                f"এখন থেকে সব ভিডিও এখানে জমা হবে।",
                buttons=[[Button.inline("🔙 মূল ড্যাশবোর্ড", b"refresh_status")]]
            )
        except Exception as e:
            logger.error(f"Error setting target: {e}")
            await event.edit(f"❌ টার্গেট সেট করতে সমস্যা: `{e}`", buttons=[[Button.inline("🔙 ফিরে যান", b"refresh_status")]])
        return

    # Sync past videos
    if data.startswith(b"sync_"):
        if active_sync["running"]:
            await event.answer("⚠️ অলরেডি একটি সিঙ্ক চলছে! আগে সেটি শেষ করুন বা বন্ধ করুন।", alert=True)
            return

        sync_type = data.decode().replace("sync_", "")
        limit = None if sync_type == "all" else int(sync_type)

        source_id = settings.get("source_id")
        source_title = settings.get("source_title", "Source")

        if not source_id:
            await event.answer("⚠️ আগে কোনো সোর্স গ্রুপ সিলেক্ট করুন!", alert=True)
            return

        limit_desc = "সবগুলো (All 1000+)" if limit is None else f"{limit}টি"
        status_msg = await event.edit(
            f"🚀 **{source_title}** থেকে {limit_desc} ভিডিও সিঙ্ক শুরু হচ্ছে...\n"
            f"সার্ভারে ব্যাকগ্রাউন্ড টাস্ক প্রস্তুত করা হচ্ছে...",
            buttons=[[Button.inline("🛑 সিঙ্ক বন্ধ করুন (Stop)", b"stop_sync")]]
        )

        # Launch continuous background task so Telegram callback never times out
        asyncio.create_task(
            run_bulk_sync(source_id, source_title, limit, event.chat_id, status_msg.id)
        )
        return

# Link or Direct Forward Handler in Bot Chat
@bot_client.on(events.NewMessage)
async def bot_message_handler(event):
    if not event.is_private:
        return
    if event.raw_text and event.raw_text.startswith("/"):
        return

    user_text = event.raw_text.strip() if event.raw_text else ""

    # Direct Video uploaded to Bot
    if is_video_message(event.message):
        status_msg = await event.reply("🎬 ভিডিও সনাক্ত হয়েছে! টার্গেট গ্রুপে আপলোড করা হচ্ছে...")
        ok = await forward_video_core(event.message, "Direct")
        if ok:
            await status_msg.edit(f"✅ ভিডিও সফলভাবে **{settings.get('target_title', '🫦🫦🫦')}** গ্রুপে আপলোড হয়েছে!")
        else:
            await status_msg.edit("❌ ভিডিও আপলোড করতে সমস্যা হয়েছে।")
        return

    # Link pasted by user
    if user_text and ("t.me/" in user_text or user_text.startswith("@")):
        status_msg = await event.reply("🔍 লিংকটি যাচাই করা হচ্ছে...")
        try:
            clean = clean_chat_input(user_text)
            entity = await user_client.get_entity(clean)
            title = getattr(entity, 'title', getattr(entity, 'username', str(entity.id)))
            
            settings["source_id"] = entity.id
            settings["source_title"] = title
            settings["source_ids"] = [entity.id]
            save_settings(settings)

            await status_msg.edit(
                f"✅ **নতুন সোর্স গ্রুপ যুক্ত হয়েছে:** **{title}** (`{entity.id}`)\n\n"
                f"📤 **টার্গেট:** {settings.get('target_title', '🫦🫦🫦')}\n"
                f"এখন থেকে এই গ্রুপের সব নতুন ভিডিও স্বয়ংক্রিয়ভাবে আপনার গ্রুপে চলে যাবে!",
                buttons=[
                    [Button.inline(f"🔄 {title} থেকে শেষ ১০টি ভিডিও কপি করুন", b"sync_10")],
                    [Button.inline("🔙 মূল মেন্যু", b"refresh_status")]
                ]
            )
        except Exception as e:
            logger.error(f"Error resolving pasted link: {e}")
            await status_msg.edit(
                f"❌ লিংকটি রিড করা যায়নি: `{e}`\n\n"
                "💡 **টিপস:** সহজে সিলেক্ট করতে নিচের বাটনে ক্লিক করুন:",
                buttons=[[Button.inline("📋 আমার গ্রুপ তালিকা থেকে বেছে নিন", b"list_groups")]]
            )
        return

# ==================== CLOUD HEALTH SERVER ====================

async def handle_health(reader, writer):
    try:
        await reader.read(1024)
        body = b'{"status": "ok", "app": "Telegram Master Hybrid Forwarder"}'
        headers = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n"
        )
        writer.write(headers + body)
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
    print("=" * 60)
    print("🚀 Telegram Master Auto-Forwarder (Dual-Engine Live)")
    print("=" * 60)

    # 1. Start HTTP health check for Render & UptimeRobot
    asyncio.create_task(start_health_server())

    # 2. Start User Client
    print("Connecting User Account Client...")
    await user_client.start()
    me_user = await user_client.get_me()
    print(f"✅ User Client Online: {me_user.first_name} (ID: {me_user.id})")

    # 3. Start Bot Client
    print("Connecting Bot Client...")
    await bot_client.start(bot_token=BOT_TOKEN)
    me_bot = await bot_client.get_me()
    print(f"✅ Bot Client Online: @{me_bot.username} (ID: {me_bot.id})")

    # 4. Resolve Target Entity
    target = await get_target_entity()
    if target:
        print(f"🎯 Target Group Ready: {settings.get('target_title')} (ID: {settings.get('target_id')})")
    else:
        print("⚠️ Warning: Target Group could not be resolved!")

    print(f"📥 Active Source Group: {settings.get('source_title')} (ID: {settings.get('source_id')})")
    print("=" * 60)
    print("✨ All Systems Operational! Forwarder is Active 24/7.")
    print("=" * 60)

    # Keep both clients running
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped.")
