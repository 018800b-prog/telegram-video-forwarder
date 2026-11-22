import os
import sys
import asyncio
from pathlib import Path
from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, User

from config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    OWNER_ID,
    runtime_settings,
    update_runtime_setting,
    validate_config,
    logger,
    BASE_DIR
)
from utils import (
    format_size,
    generate_caption,
    parse_telegram_link,
    clean_chat_input,
    is_video_message
)

# Temporary directory for fallback downloads if needed
TEMP_DIR = BASE_DIR / "temp_media"
TEMP_DIR.mkdir(exist_ok=True)

# State tracking for setup wizard: user_id -> "WAITING_TARGET" | "WAITING_SOURCE" | "READY"
user_setup_state = {}

# Validate config before startup
is_valid, err_msg = validate_config()
if not is_valid:
    logger.error(f"Config Validation Error: {err_msg}")
    sys.exit(1)

SESSION_NAME = str(BASE_DIR / "video_forwarder_bot")
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

def is_authorized(user_id: int) -> bool:
    owner = runtime_settings.get("owner_id", OWNER_ID)
    if not owner or owner == 0:
        update_runtime_setting("owner_id", user_id)
        return True
    return user_id == owner

async def resolve_chat(identifier):
    """Safely get chat entity and title"""
    try:
        clean = clean_chat_input(str(identifier))
        entity = await client.get_entity(clean)
        title = getattr(entity, 'title', getattr(entity, 'username', str(entity.id)))
        return entity, title, entity.id
    except Exception as e:
        logger.error(f"Error resolving chat {identifier}: {e}")
        return None, None, None

async def upload_video_to_target(source_msg, status_msg=None, reply_event=None):
    """Core logic to send video to target group cleanly without forward tags"""
    target_chat = runtime_settings.get("target_chat_id")
    if not target_chat:
        msg = "⚠️ টার্গেট গ্রুপ সেট করা নেই! অনুগ্রহ করে `/start` দিয়ে গ্রুপ লিংক দিন।"
        if status_msg:
            await status_msg.edit(msg)
        elif reply_event:
            await reply_event.reply(msg)
        return False

    final_caption = generate_caption(source_msg.text)
    file_size = getattr(source_msg.file, "size", 0) if source_msg.file else 0

    try:
        # Strategy 1: Instant Cloud Forward
        sent_msg = await client.send_file(
            entity=target_chat,
            file=source_msg.media,
            caption=final_caption,
            supports_streaming=True
        )

        size_str = format_size(file_size)
        if status_msg:
            await status_msg.edit(
                f"✅ **ভিডিও সফলভাবে আপনার টার্গেট গ্রুপে আপলোড হয়েছে!**\n\n"
                f"📁 **সাইজ:** {size_str}\n"
                f"🚀 **মোড:** Cloud Fast Upload (Zero Bandwidth)"
            )
        return True

    except Exception as e:
        logger.warning(f"Direct cloud send failed ({e}). Attempting download & upload fallback...")
        try:
            if status_msg:
                await status_msg.edit("📥 ভিডিওটি ক্লাউড থেকে সরাসরি পাঠানো যায়নি। ডাউনলোড করে আপলোড করা হচ্ছে...")

            temp_path = TEMP_DIR / f"video_{source_msg.id}.mp4"
            downloaded = await client.download_media(source_msg, file=str(temp_path))
            
            if not downloaded or not os.path.exists(downloaded):
                raise Exception("ভিডিও ডাউনলোড সম্পন্ন হয়নি।")

            await client.send_file(
                entity=target_chat,
                file=downloaded,
                caption=final_caption,
                supports_streaming=True
            )

            try:
                if os.path.exists(downloaded):
                    os.remove(downloaded)
            except Exception:
                pass

            if status_msg:
                await status_msg.edit(
                    f"✅ **ভিডিও সফলভাবে আপলোড সম্পন্ন হয়েছে!**\n\n"
                    f"📁 **সাইজ:** {format_size(file_size)}\n"
                    f"🚀 **মোড:** Fallback Re-upload"
                )
            return True

        except Exception as fallback_err:
            logger.error(f"Fallback upload error: {fallback_err}")
            if status_msg:
                await status_msg.edit(f"❌ ভিডিও আপলোড করতে সমস্যা হয়েছে:\n`{fallback_err}`")
            return False

# ==================== COMMAND HANDLERS ====================

@client.on(events.NewMessage(pattern=r"^/(start|setup)$"))
async def start_wizard_handler(event):
    if not event.is_private:
        return

    user_id = event.sender_id
    if not is_authorized(user_id):
        await event.reply("⚠️ এই বটটি শুধুমাত্র অনুমোদিত অ্যাডমিনের জন্য।")
        return

    # Start 2-Step Interactive Wizard
    user_setup_state[user_id] = "WAITING_TARGET"

    welcome_text = (
        "👋 **স্বাগতম! ভিডিও অটো-ফরোয়ার্ডিং সেটআপ উইজার্ড:**\n\n"
        "১️⃣ **প্রথমে আপনার নিজস্ব গ্রুপের লিংক দিন** (যে গ্রুপে ভিডিওগুলো আপলোড করতে চান):\n\n"
        "👉 উদাহরণ হিসেবে গ্রুপের লিংক বা ইউজারনেম পাঠান:\n"
        "`https://t.me/my_group` অথবা `@my_group`\n\n"
        "*(নোট: লিংক দেওয়ার আগে বটটিকে আপনার ঐ গ্রুপে যোগ করে **Admin** বানিয়ে নিন যাতে বট ভিডিও পোস্ট করতে পারে)*"
    )
    await event.reply(welcome_text)

@client.on(events.NewMessage(pattern=r"^/id$"))
async def id_handler(event):
    chat = await event.get_chat()
    sender = await event.get_sender()
    
    reply = f"🆔 **চ্যাট তথ্য:**\n\n• **বর্তমান Chat ID:** `{chat.id}`\n"
    if sender:
        reply += f"• **আপনার Sender ID:** `{sender.id}`\n"
    if hasattr(chat, "title"):
        reply += f"• **চ্যাট নাম:** {chat.title}\n"
    if hasattr(chat, "username") and chat.username:
        reply += f"• **ইউজারনেম:** @{chat.username}\n"
    await event.reply(reply)

@client.on(events.NewMessage(pattern=r"^/status$"))
async def status_handler(event):
    if not event.is_private or not is_authorized(event.sender_id):
        return

    target = runtime_settings.get("target_chat_id", "সেট করা নেই")
    source = runtime_settings.get("source_chat_id", "সেট করা নেই")
    caption_m = runtime_settings.get("caption_mode", "original")

    status_text = (
        "📊 **বটের বর্তমান স্ট্যাটাস:**\n\n"
        f"📤 **টার্গেট গ্রুপ (যেখানে আপলোড হবে):** `{target}`\n"
        f"📥 **সোর্স গ্রুপ (যেখান থেকে আসবে):** `{source}`\n"
        f"📝 **ক্যাপশন মোড:** `{caption_m}`\n\n"
        "নতুন করে লিংক সেট করতে লিখুন: `/start`\n"
        "পুরোনো ভিডিও কপি করতে লিখুন: `/sync 10`"
    )
    await event.reply(status_text)

@client.on(events.NewMessage(pattern=r"^/sync(?:\s+(\d+))?$"))
async def sync_history_handler(event):
    if not event.is_private or not is_authorized(event.sender_id):
        return

    source_chat = runtime_settings.get("source_chat_id")
    target_chat = runtime_settings.get("target_chat_id")

    if not source_chat or not target_chat:
        await event.reply("⚠️ সোর্স ও টার্গেট গ্রুপ উভয়টি সেট করা থাকতে হবে। প্রথমে `/start` দিয়ে সেটআপ করুন।")
        return

    limit_str = event.pattern_match.group(1)
    limit = int(limit_str) if limit_str else 10
    limit = min(limit, 50)  # Max 50 per batch to avoid Telegram flood limits

    status_msg = await event.reply(f"🔄 সোর্স গ্রুপ থেকে শেষ {limit}টি ভিডিও খোঁজা হচ্ছে...")

    found = 0
    copied = 0
    try:
        async for msg in client.iter_messages(source_chat, limit=100):
            if is_video_message(msg):
                found += 1
                await status_msg.edit(f"⏳ ভিডিও পাওয়া গেছে ({found}/{limit})! কপি করে টার্গেট গ্রুপে আপলোড হচ্ছে...")
                success = await upload_video_to_target(msg)
                if success:
                    copied += 1
                    await asyncio.sleep(1.5)  # Safe delay between uploads
                if found >= limit:
                    break

        await status_msg.edit(
            f"🎉 **সিঙ্ক সম্পন্ন হয়েছে!**\n\n"
            f"• মোট পাওয়া ভিডিও: {found} টি\n"
            f"• সফলভাবে আপলোড হয়েছে: {copied} টি"
        )
    except Exception as e:
        logger.error(f"Sync error: {e}")
        await status_msg.edit(f"❌ সিঙ্ক করার সময় সমস্যা হয়েছে:\n`{e}`")

# ==================== TEXT & LINK WIZARD HANDLER ====================

@client.on(events.NewMessage)
async def message_dispatcher(event):
    # Only process private messages for setup & commands
    if not event.is_private:
        # Check if this is a live message from source_chat in a group/channel
        source_chat = runtime_settings.get("source_chat_id")
        if source_chat and event.chat_id == source_chat:
            if is_video_message(event.message):
                logger.info(f"Live new video detected in source chat {event.chat_id}! Auto-forwarding...")
                await upload_video_to_target(event.message)
        return

    # Ignore slash commands handled above
    if event.raw_text and event.raw_text.startswith("/"):
        return

    user_id = event.sender_id
    if not is_authorized(user_id):
        await event.reply("⚠️ আপনি এই বটটি ব্যবহার করার জন্য অনুমোদিত নন।")
        return

    user_text = event.raw_text.strip() if event.raw_text else ""
    current_state = user_setup_state.get(user_id)

    # ------------------ STEP 1: WAITING TARGET GROUP LINK ------------------
    if current_state == "WAITING_TARGET" and user_text:
        status_msg = await event.reply("🔍 আপনার টার্গেট গ্রুপটি যাচাই করা হচ্ছে...")
        clean = clean_chat_input(user_text)
        
        try:
            entity = await client.get_entity(clean)
            title = getattr(entity, 'title', getattr(entity, 'username', str(entity.id)))
            target_id = entity.id
            if isinstance(entity, Channel):
                target_id = int(f"-100{entity.id}")
            elif isinstance(entity, Chat):
                target_id = int(f"-{entity.id}")

            update_runtime_setting("target_chat_id", target_id)
            user_setup_state[user_id] = "WAITING_SOURCE"

            await status_msg.edit(
                f"✅ **১ম ধাপ সফল! আপনার টার্গেট গ্রুপ সেট হয়েছে:**\n"
                f"🏷️ **নাম:** {title}\n"
                f"🆔 **ID:** `{target_id}`\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"২️⃣ **এবার যে গ্রুপ থেকে ভিডিও পাঠাতে চান, সেই সোর্স গ্রুপের লিংক দিন:**\n\n"
                f"👉 উদাহরণ: `https://t.me/source_group` অথবা `@source_group`"
            )
            return
        except Exception as e:
            logger.error(f"Target resolution error: {e}")
            await status_msg.edit(
                f"❌ **টার্গেট গ্রুপ কানেক্ট করা সম্ভব হয়নি!**\n\n"
                f"কারণ: `{e}`\n\n"
                f"⚠️ **নিশ্চিত করুন:**\n"
                f"১. বটটিকে (@autforwbdbot) ঐ গ্রুপে মেম্বার হিসেবে অ্যাড করেছেন।\n"
                f"২. বটটিকে **Admin** অধিকার দিয়েছেন (মিডিয়া পাঠানোর পারমিশন)।\n\n"
                f"অনুগ্রহ করে অ্যাডমিন বানিয়ে আবার লিংক বা আইডিটি পাঠান।"
            )
            return

    # ------------------ STEP 2: WAITING SOURCE GROUP LINK ------------------
    if current_state == "WAITING_SOURCE" and user_text:
        status_msg = await event.reply("🔍 সোর্স গ্রুপটি যাচাই করা হচ্ছে...")
        clean = clean_chat_input(user_text)

        try:
            entity = await client.get_entity(clean)
            title = getattr(entity, 'title', getattr(entity, 'username', str(entity.id)))
            source_id = entity.id
            if isinstance(entity, Channel):
                source_id = int(f"-100{entity.id}")
            elif isinstance(entity, Chat):
                source_id = int(f"-{entity.id}")

            update_runtime_setting("source_chat_id", source_id)
            user_setup_state[user_id] = "READY"

            target_id = runtime_settings.get("target_chat_id")

            await status_msg.edit(
                f"🎉 **অভিনন্দন! অটোমেশন সম্পূর্ণ সক্রিয় হয়েছে!**\n\n"
                f"📥 **সোর্স গ্রুপ (ভিডিও আসবে):** {title} (`{source_id}`)\n"
                f"📤 **টার্গেট গ্রুপ (ভিডিও যাবে):** `{target_id}`\n\n"
                f"✨ **এখন কী হবে?**\n"
                f"• সোর্স গ্রুপে যখনই কোনো নতুন ভিডিও পোস্ট হবে, বট সাথে সাথে কোনো ফরোয়ার্ড ট্যাগ ছাড়া আপনার টার্গেট গ্রুপে আপলোড করে দেবে!\n"
                f"• আপনি নিজেও যেকোনো ভিডিও বা পোস্টের লিংক এখানে ফরোয়ার্ড করলে সাথে সাথে আপলোড হয়ে যাবে।\n\n"
                f"💡 **টিপস:** সোর্স গ্রুপের আগের পুরোনো ভিডিওগুলো এখনই কপি করতে চাইলে লিখুন:\n"
                f"`/sync 10` (শেষ ১০টি ভিডিও কপি করতে)"
            )
            return
        except Exception as e:
            logger.error(f"Source resolution error: {e}")
            # If it's a private group that bot cannot see
            await status_msg.edit(
                f"⚠️ **সোর্স গ্রুপটি সরাসরি অ্যাক্সেস করা যায়নি!**\n\n"
                f"কারণ: `{e}`\n\n"
                f"📌 **টেলিগ্রামের নিয়মাবলী:**\n"
                f"• সোর্স গ্রুপটি যদি **পাবলিক** হয়, তাহলে সঠিক ইউজারনেম (যেমন: `@groupname`) দিন।\n"
                f"• আর যদি এটি সম্পূর্ণ **প্রাইভেট গ্রুপ** হয়, তবে সাধারণ বট বাইরে থেকে ঢুকতে পারে না। সেক্ষেত্রে:\n"
                f"  👉 ঐ গ্রুপে ভিডিও এলে ভিডিওটির ওপর ক্লিক করে **Forward** চাপুন এবং এই বটে সেন্ড করে দিন—বট কোনো ফরওয়ার্ড ট্যাগ ছাড়া আপনার গ্রুপে পোস্ট করে দেবে!"
            )
            return

    # ------------------ DIRECT VIDEO / LINK FORWARDING ------------------
    if is_video_message(event.message):
        status_msg = await event.reply("🎬 ভিডিও সনাক্ত করা হয়েছে। আপনার টার্গেট গ্রুপে আপলোড হচ্ছে...")
        await upload_video_to_target(event.message, status_msg)
        return

    # Check for Single Post Link
    if user_text:
        parsed = parse_telegram_link(user_text)
        if parsed:
            status_msg = await event.reply("🔗 পোস্টের লিংক সনাক্ত হয়েছে। ভিডিও আনা হচ্ছে...")
            try:
                fetched = await client.get_messages(parsed["channel"], ids=parsed["msg_id"])
                if not fetched or not is_video_message(fetched):
                    await status_msg.edit("⚠️ এই লিংকে কোনো ভিডিও পাওয়া যায়নি।")
                    return
                await upload_video_to_target(fetched, status_msg)
                return
            except Exception as e:
                await status_msg.edit(f"❌ লিংক থেকে ভিডিও আনতে সমস্যা হয়েছে: `{e}`")
                return

    # Default fallback guide
    await event.reply(
        "❓ নতুন করে অটো-ফরোয়ার্ডার সেটআপ করতে চাইলে লিখুন: `/start`\n"
        "অথবা সরাসরি যেকোনো ভিডিও এই চ্যাটে ফরোয়ার্ড করুন!"
    )

async def handle_health_check(reader, writer):
    """Simple HTTP health-check for cloud hosts like Render.com"""
    try:
        data = await reader.read(1024)
        body = b'{"status": "ok", "service": "Telegram Video Forwarder Bot"}'
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
    """Runs a minimal HTTP server on PORT for Render/Koyeb/Heroku free tier"""
    port = int(os.getenv("PORT", 8080))
    try:
        server = await asyncio.start_server(handle_health_check, "0.0.0.0", port)
        logger.info(f"🌐 Health check HTTP server listening on port {port}")
        async with server:
            await server.serve_forever()
    except Exception as e:
        logger.warning(f"Health server could not start on port {port}: {e}")

async def main():
    print("=" * 60)
    print("🤖 Telegram Video Forwarder Bot (Interactive Wizard)")
    print("=" * 60)
    
    # Start cloud health server in background
    asyncio.create_task(start_health_server())

    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f"✅ বট সফলভাবে চালু হয়েছে: @{me.username} (ID: {me.id})")
    print("=" * 60)
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 বট বন্ধ করা হয়েছে।")
