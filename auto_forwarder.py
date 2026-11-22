"""
Telegram Automated Video Forwarder (Userbot Mode)
=================================================
এটি আপনার টেলিগ্রাম একাউন্ট দিয়ে স্বয়ংক্রিয়ভাবে সোর্স গ্রুপের সব নতুন ভিডিও 
টার্গেট গ্রুপে (কোনো ফরওয়ার্ড ট্যাগ ছাড়া) আপলোড করে দেয়।

টার্গেট গ্রুপ: https://t.me/+5sUPS6kwiSE3ZGE1
সোর্স গ্রুপ: ইচ্ছামতো পরিবর্তন করা যায় (.source কমান্ড দিয়ে)
"""

import os
import re
import sys
import asyncio
import logging
from pathlib import Path
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, PeerChannel, PeerChat
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.errors import UserAlreadyParticipantError

from config import (
    API_ID,
    API_HASH,
    BASE_DIR,
    LOG_FORMAT,
    runtime_settings,
    update_runtime_setting
)
from utils import (
    format_size,
    generate_caption,
    is_video_message,
    clean_chat_input
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("AutoForwarder")

TEMP_DIR = BASE_DIR / "temp_media"
TEMP_DIR.mkdir(exist_ok=True)

# Default Target Group specified by user
DEFAULT_TARGET_LINK = "https://t.me/+5sUPS6kwiSE3ZGE1"

# Load SESSION_STRING from env if available
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

if SESSION_STRING:
    session = StringSession(SESSION_STRING)
else:
    session = str(BASE_DIR / "user_session")

client = TelegramClient(session, API_ID, API_HASH)

# Active state
state = {
    "target_entity": None,
    "target_id": None,
    "target_title": "Loading...",
    "source_ids": set(),      # Set of chat_ids being monitored
    "source_titles": {},      # chat_id -> title
    "me_id": None
}

def extract_invite_hash(link: str) -> str:
    """Extracts hash from t.me/+hash or t.me/joinchat/hash"""
    match = re.search(r'(?:https?://)?(?:www\.)?t\.me/(?:\+|joinchat/)([a-zA-Z0-9_-]+)', link.strip())
    if match:
        return match.group(1)
    return ""

async def resolve_or_join(chat_input):
    """Resolves an entity, joining via invite link if necessary"""
    s = str(chat_input).strip()
    invite_hash = extract_invite_hash(s)
    
    if invite_hash:
        try:
            # Check or Import invite
            try:
                updates = await client(ImportChatInviteRequest(invite_hash))
                if hasattr(updates, 'chats') and updates.chats:
                    chat = updates.chats[0]
                    return chat, chat.title, chat.id
            except UserAlreadyParticipantError:
                # Already in chat, resolve via CheckChatInviteRequest
                check = await client(CheckChatInviteRequest(invite_hash))
                if hasattr(check, 'chat'):
                    return check.chat, check.chat.title, check.chat.id
        except Exception as e:
            logger.warning(f"Invite join error for {s}: {e}")

    # Standard username or integer ID
    clean = clean_chat_input(s)
    try:
        entity = await client.get_entity(clean)
        title = getattr(entity, 'title', getattr(entity, 'username', str(entity.id)))
        return entity, title, entity.id
    except Exception as e:
        logger.error(f"Failed to resolve {s}: {e}")
        return None, None, None

async def forward_video(message, source_title="Source"):
    """Copies and uploads video to target cleanly without forward tag"""
    if not state["target_entity"]:
        logger.warning("Target group not set yet!")
        return False

    final_caption = generate_caption(message.text)
    file_size = getattr(message.file, "size", 0) if message.file else 0

    try:
        # Strategy 1: Instant Cloud Forward (Zero bandwidth)
        logger.info(f"Forwarding video ({format_size(file_size)}) to {state['target_title']}...")
        await client.send_file(
            entity=state["target_entity"],
            file=message.media,
            caption=final_caption,
            supports_streaming=True
        )
        logger.info("✅ Video uploaded cleanly via cloud!")
        return True

    except Exception as e:
        logger.warning(f"Direct cloud upload failed ({e}). Attempting download & upload fallback...")
        try:
            temp_path = TEMP_DIR / f"fwd_{message.id}.mp4"
            downloaded = await client.download_media(message, file=str(temp_path))
            
            if not downloaded or not os.path.exists(downloaded):
                raise Exception("Download failed")

            await client.send_file(
                entity=state["target_entity"],
                file=downloaded,
                caption=final_caption,
                supports_streaming=True
            )

            try:
                if os.path.exists(downloaded):
                    os.remove(downloaded)
            except Exception:
                pass

            logger.info("✅ Video uploaded via fallback!")
            return True
        except Exception as err:
            logger.error(f"Fallback upload error: {err}")
            return False

# ==================== COMMAND HANDLERS (Saved Messages / Private) ====================

@client.on(events.NewMessage(outgoing=True, pattern=r"^\.(help|start)$"))
async def cmd_help(event):
    help_text = (
        "🤖 **অটো-ফরোয়ার্ডার কমান্ডসমূহ (Saved Messages-এ লিখুন):**\n\n"
        "• `.source <গ্রুপের_লিংক>` - নতুন সোর্স গ্রুপ সেট বা যুক্ত করুন\n"
        "• `.target <গ্রুপের_লিংক>` - টার্গেট গ্রুপ পরিবর্তন করুন\n"
        "• `.sources` - বর্তমানে কোন কোন গ্রুপ থেকে ভিডিও আসছে তার তালিকা\n"
        "• `.removesource <গ্রুপের_লিংক বা ID>` - কোনো সোর্স গ্রুপ বাদ দিন\n"
        "• `.sync <সংখ্যা>` - সোর্স গ্রুপের আগের ভিডিওগুলো এখনই কপি করতে (যেমন: `.sync 10`)\n"
        "• `.status` - বর্তমান টার্গেট ও সোর্স গ্রুপের স্ট্যাটাস দেখুন\n\n"
        f"🎯 **বর্তমান টার্গেট:** {state['target_title']}"
    )
    await event.reply(help_text)

@client.on(events.NewMessage(outgoing=True, pattern=r"^\.target(?:\s+(.+))?$"))
async def cmd_set_target(event):
    link = event.pattern_match.group(1)
    if not link:
        await event.reply(f"📌 বর্তমান টার্গেট গ্রুপ: **{state['target_title']}** (`{state['target_id']}`)")
        return

    status_msg = await event.reply("🔍 টার্গেট গ্রুপ কানেক্ট করা হচ্ছে...")
    entity, title, chat_id = await resolve_or_join(link)
    if entity:
        state["target_entity"] = entity
        state["target_id"] = chat_id
        state["target_title"] = title
        update_runtime_setting("target_chat_id", chat_id)
        await status_msg.edit(f"✅ **নতুন টার্গেট গ্রুপ সেট হয়েছে!**\n🏷️ **নাম:** {title}\n🆔 **ID:** `{chat_id}`")
    else:
        await status_msg.edit("❌ টার্গেট গ্রুপ কানেক্ট করা যায়নি। লিংকটি সঠিক কিনা যাচাই করুন।")

@client.on(events.NewMessage(outgoing=True, pattern=r"^\.source(?:\s+(.+))?$"))
async def cmd_add_source(event):
    link = event.pattern_match.group(1)
    if not link:
        await event.reply("ব্যবহারবিধি: `.source <সোর্স_গ্রুপের_লিংক>`\n(উদাহরণ: `.source https://t.me/+xxxx` অথবা `.source @channel`)")
        return

    status_msg = await event.reply("🔍 সোর্স গ্রুপ ভেরিফাই করা হচ্ছে...")
    entity, title, chat_id = await resolve_or_join(link)
    
    if entity:
        state["source_ids"].add(chat_id)
        state["source_titles"][chat_id] = title
        update_runtime_setting("source_chat_id", chat_id)
        await status_msg.edit(
            f"🎉 **সোর্স গ্রুপ সফলভাবে যুক্ত হয়েছে!**\n\n"
            f"📥 **সোর্স:** {title} (`{chat_id}`)\n"
            f"📤 **ভিডিও জমা হবে (টার্গেট):** {state['target_title']}\n\n"
            f"⚡ এখন থেকে এই সোর্স গ্রুপে কোনো নতুন ভিডিও আসলে কোনো মানুষের হাত দেওয়া ছাড়াই "
            f"অটোমেটিক আপনার টার্গেট গ্রুপে আপলোড হয়ে যাবে!"
        )
    else:
        await status_msg.edit("❌ সোর্স গ্রুপটিতে কানেক্ট করা যায়নি। লিংকটি সঠিক কিনা চেক করুন।")

@client.on(events.NewMessage(outgoing=True, pattern=r"^\.sources$"))
async def cmd_list_sources(event):
    if not state["source_ids"]:
        await event.reply("⚠️ বর্তমানে কোনো সোর্স গ্রুপ যুক্ত নেই।\nযুক্ত করতে লিখুন: `.source <লিংক>`")
        return

    text = "📋 **বর্তমানে সক্রিয় সোর্স গ্রুপসমূহ (Auto-Monitoring):**\n\n"
    for idx, sid in enumerate(state["source_ids"], 1):
        name = state["source_titles"].get(sid, str(sid))
        text += f"{idx}. **{name}** (`{sid}`)\n"
    
    text += f"\n📤 **ভিডিও যাচ্ছে:** {state['target_title']}"
    await event.reply(text)

@client.on(events.NewMessage(outgoing=True, pattern=r"^\.sync(?:\s+(\d+))?$"))
async def cmd_sync(event):
    """Sync past videos from the latest source group"""
    if not state["source_ids"]:
        await event.reply("⚠️ কোনো সোর্স গ্রুপ নেই। আগে `.source <লিংক>` দিন।")
        return

    limit_str = event.pattern_match.group(1)
    limit = int(limit_str) if limit_str else 10
    limit = min(limit, 50)

    # Pick the latest added source
    source_id = list(state["source_ids"])[-1]
    source_name = state["source_titles"].get(source_id, str(source_id))

    status_msg = await event.reply(f"🔄 **{source_name}** থেকে শেষ {limit}টি ভিডিও খোঁজা হচ্ছে...")

    found = 0
    copied = 0
    try:
        async for msg in client.iter_messages(source_id, limit=100):
            if is_video_message(msg):
                found += 1
                await status_msg.edit(f"⏳ ভিডিও পাওয়া গেছে ({found}/{limit})! কপি করে টার্গেট গ্রুপে আপলোড হচ্ছে...")
                ok = await forward_video(msg, source_name)
                if ok:
                    copied += 1
                    await asyncio.sleep(1.5)
                if found >= limit:
                    break

        await status_msg.edit(
            f"🎉 **সিঙ্ক সম্পন্ন হয়েছে!**\n\n"
            f"• মোট পাওয়া ভিডিও: {found} টি\n"
            f"• সফলভাবে আপলোড হয়েছে: {copied} টি\n"
            f"• টার্গেট গ্রুপ: {state['target_title']}"
        )
    except Exception as e:
        logger.error(f"Sync error: {e}")
        await status_msg.edit(f"❌ সমস্যা হয়েছে: `{e}`")

# ==================== REAL-TIME LISTENER ====================

@client.on(events.NewMessage)
async def live_video_listener(event):
    """Listens for new video messages in all monitored source groups"""
    if not event.chat_id:
        return

    # Check if incoming message is from any of our monitored source groups
    # Support both regular chat_id and -100 format matching
    matched_id = None
    if event.chat_id in state["source_ids"]:
        matched_id = event.chat_id
    elif int(str(event.chat_id).replace("-100", "-")) in state["source_ids"]:
        matched_id = event.chat_id

    if matched_id:
        if is_video_message(event.message):
            src_name = state["source_titles"].get(matched_id, "Source Group")
            logger.info(f"🚨 New video in source group [{src_name}]! Auto-forwarding to {state['target_title']}...")
            await forward_video(event.message, src_name)

# ==================== CLOUD HEALTH CHECK ====================

async def handle_health(reader, writer):
    try:
        await reader.read(1024)
        body = b'{"status": "ok", "app": "Telegram Auto Forwarder Userbot"}'
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

# ==================== MAIN ====================

async def main():
    print("=" * 60)
    print("🚀 Telegram Automated Video Forwarder (Userbot Mode)")
    print("=" * 60)

    # Start health server for Render/UptimeRobot
    asyncio.create_task(start_health_server())

    await client.start()
    me = await client.get_me()
    state["me_id"] = me.id
    print(f"✅ লগিন সফল হয়েছে: {me.first_name} (@{me.username})")

    # Connect default target group
    print(f"🔍 টার্গেট গ্রুপ কানেক্ট করা হচ্ছে: {DEFAULT_TARGET_LINK} ...")
    t_entity, t_title, t_id = await resolve_or_join(DEFAULT_TARGET_LINK)
    if t_entity:
        state["target_entity"] = t_entity
        state["target_title"] = t_title
        state["target_id"] = t_id
        print(f"🎯 টার্গেট গ্রুপ সক্রিয়: {t_title} (ID: {t_id})")
    else:
        print("⚠️ ডিফল্ট টার্গেট গ্রুপ এখনো কানেক্ট হয়নি। Telegram-এ .target <লিংক> দিয়ে সেট করতে পারেন।")

    # If any previous source_chat_id in settings, add it
    saved_source = runtime_settings.get("source_chat_id")
    if saved_source:
        try:
            s_entity, s_title, s_id = await resolve_or_join(saved_source)
            if s_entity:
                state["source_ids"].add(s_id)
                state["source_titles"][s_id] = s_title
                print(f"📥 পূর্বের সোর্স গ্রুপ লোড হয়েছে: {s_title} ({s_id})")
        except Exception:
            pass

    print("=" * 60)
    print("⚡ অটো-ফরোয়ার্ডার প্রস্তুত!")
    print("💡 টেলিগ্রামের যেকোনো চ্যাট বা 'Saved Messages'-এ লিখুন:")
    print("   .source <সোর্স_গ্রুপের_লিংক>  -> নতুন সোর্স গ্রুপ অ্যাড করতে")
    print("   .sync 10                      -> সোর্স থেকে আগের ১০টি ভিডিও কপি করতে")
    print("   .help                         -> সব কমান্ড দেখতে")
    print("=" * 60)

    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 বন্ধ করা হয়েছে।")
