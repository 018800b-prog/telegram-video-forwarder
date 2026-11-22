"""
Telegram Userbot Forwarder (For Protected/Restricted Channels)
==============================================================
এটি ইউজার একাউন্ট দিয়ে কাজ করে। তাই যে চ্যানেলগুলোতে Forward Restricted বা 
Save Content অফ করা থাকে, সেখান থেকেও ভিডিও ডাউনলোড করে আপনার গ্রুপে আপলোড দিতে পারে।
"""

import os
import sys
import asyncio
from pathlib import Path
from telethon import TelegramClient, events
from config import API_ID, API_HASH, runtime_settings, update_runtime_setting, BASE_DIR
from utils import format_size, generate_caption, parse_telegram_link, is_video_message

TEMP_DIR = BASE_DIR / "temp_media"
TEMP_DIR.mkdir(exist_ok=True)

if not API_ID or not API_HASH:
    print("❌ API_ID বা API_HASH দেওয়া হয়নি। অনুগ্রহ করে .env ফাইলে সেট করুন।")
    sys.exit(1)

SESSION_NAME = str(BASE_DIR / "user_session")
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

@client.on(events.NewMessage(pattern=r"^\.setgroup(?:\s+(.+))?$"))
async def user_set_group(event):
    if not event.out:
        return
    target = event.pattern_match.group(1)
    if not target:
        curr = runtime_settings.get("target_chat_id", "সেট করা নেই")
        await event.edit(f"📌 বর্তমান টার্গেট গ্রুপ: `{curr}`\nব্যবহার: `.setgroup <ID বা @username>`")
        return

    target = target.strip()
    try:
        chat_id = int(target) if (target.startswith("-") or target.isdigit()) else target
        entity = await client.get_entity(chat_id)
        title = getattr(entity, 'title', str(chat_id))
        update_runtime_setting("target_chat_id", chat_id)
        await event.edit(f"✅ টার্গেট গ্রুপ সেট হয়েছে: **{title}** (`{chat_id}`)")
    except Exception as e:
        await event.edit(f"❌ গ্রুপ ভেরিফাই হয়নি: `{e}`")

@client.on(events.NewMessage(pattern=r"^\.upload(?:\s+(.+))?$"))
async def link_upload_handler(event):
    """Download video from any link (even restricted) and upload to group"""
    if not event.out:
        return
    url = event.pattern_match.group(1)
    if not url:
        await event.edit("ব্যবহারবিধি: `.upload https://t.me/channel/123`")
        return

    parsed = parse_telegram_link(url)
    if not parsed:
        await event.edit("❌ সঠিক টেলিগ্রাম লিংক পাওয়া যায়নি।")
        return

    target_chat = runtime_settings.get("target_chat_id")
    if not target_chat:
        await event.edit("⚠️ টার্গেট গ্রুপ সেট করা নেই। `.setgroup <ID>` ব্যবহার করুন।")
        return

    await event.edit("🔍 মেসেজ খোঁজা হচ্ছে...")
    try:
        msg = await client.get_messages(parsed["channel"], ids=parsed["msg_id"])
        if not msg or not is_video_message(msg):
            await event.edit("⚠️ এই মেসেজে কোনো ভিডিও পাওয়া যায়নি।")
            return

        await event.edit("📥 ভিডিও ডাউনলোড হচ্ছে (Protected Content Support)...")
        temp_path = TEMP_DIR / f"protected_{msg.id}.mp4"

        async def d_progress(current, total):
            if total > 0:
                percent = (current / total) * 100
                if int(percent) % 25 == 0:
                    try:
                        await event.edit(f"📥 ডাউনলোড হচ্ছে: {percent:.1f}% ({format_size(current)} / {format_size(total)})")
                    except Exception:
                        pass

        downloaded = await client.download_media(msg, file=str(temp_path), progress_callback=d_progress)
        
        await event.edit("📤 গ্রুপে ফ্রেশ আপলোড হচ্ছে...")
        final_caption = generate_caption(msg.text)

        async def u_progress(current, total):
            if total > 0:
                percent = (current / total) * 100
                if int(percent) % 25 == 0:
                    try:
                        await event.edit(f"📤 আপলোড হচ্ছে: {percent:.1f}%")
                    except Exception:
                        pass

        await client.send_file(
            entity=target_chat,
            file=downloaded,
            caption=final_caption,
            supports_streaming=True,
            progress_callback=u_progress
        )

        try:
            if os.path.exists(downloaded):
                os.remove(downloaded)
        except Exception:
            pass

        await event.edit("✅ ভিডিও সফলভাবে আপনার গ্রুপে আপলোড করা হয়েছে!")
    except Exception as e:
        await event.edit(f"❌ সমস্যা হয়েছে: `{e}`")

# Listen to Saved Messages: If user forwards a video to their own Saved Messages, upload it!
@client.on(events.NewMessage(chats="me"))
async def saved_messages_handler(event):
    if not is_video_message(event.message):
        return

    target_chat = runtime_settings.get("target_chat_id")
    if not target_chat:
        return

    status_msg = await event.reply("🎬 ভিডিও সনাক্ত হয়েছে! গ্রুপে পাঠানোর প্রস্তুতি চলছে...")
    
    final_caption = generate_caption(event.message.text)
    try:
        # First try direct cloud send
        await client.send_file(
            entity=target_chat,
            file=event.message.media,
            caption=final_caption,
            supports_streaming=True
        )
        await status_msg.edit("✅ আপনার গ্রুপে ফ্রেশ আপলোড সম্পন্ন হয়েছে!")
    except Exception as e:
        # Protected content fallback: download and upload
        await status_msg.edit(f"⏳ ডাউনলোড ও রিলোড হচ্ছে ({e})...")
        temp_file = TEMP_DIR / f"saved_{event.message.id}.mp4"
        downloaded = await client.download_media(event.message, file=str(temp_file))
        await client.send_file(entity=target_chat, file=downloaded, caption=final_caption, supports_streaming=True)
        try:
            if os.path.exists(downloaded):
                os.remove(downloaded)
        except Exception:
            pass
        await status_msg.edit("✅ সুরক্ষিত ভিডিও সফলভাবে গ্রুপে আপলোড হয়েছে!")

async def main():
    print("=" * 60)
    print("🚀 Telegram Userbot চালু হচ্ছে...")
    print("=" * 60)
    await client.start()
    me = await client.get_me()
    print(f"✅ লগিন সম্পন্ন হয়েছে: {me.first_name} (@{me.username})")
    print("=" * 60)
    print("💡 টিপস:")
    print("১. যেকোনো ভিডিও আপনার নিজের 'Saved Messages'-এ ফরোয়ার্ড করলেই সরাসরি গ্রুপে চলে যাবে।")
    print("২. অথবা যেকোনো চ্যাটে লিখুন: .upload <post_link>")
    print("=" * 60)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
