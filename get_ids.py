"""
Telegram ID Finder Helper Script
================================
এই স্ক্রিপ্ট দিয়ে খুব সহজেই যেকোনো গ্রুপ বা চ্যানেলের ID বের করা যায়।
"""

import sys
import asyncio
from telethon import TelegramClient
from config import API_ID, API_HASH, BOT_TOKEN, validate_config, BASE_DIR

is_valid, err_msg = validate_config()
if not is_valid:
    print(f"❌ কনফিগারেশন ত্রুটি: {err_msg}")
    sys.exit(1)

client = TelegramClient(str(BASE_DIR / "helper_session"), API_ID, API_HASH)

async def main():
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print("=" * 60)
    print(f"🤖 বটের নাম: {me.first_name}")
    print(f"🔗 বটের ইউজারনেম: @{me.username}")
    print(f"🆔 বটের আইডি: {me.id}")
    print("=" * 60)
    print("\n📌 আপনার বটের ডায়ালগ এবং গ্রুপসমূহের তালিকা নিচে দেওয়া হলো:")
    print("-" * 60)
    
    count = 0
    async for dialog in client.iter_dialogs():
        count += 1
        chat_type = "গ্রুপ/চ্যানেল" if dialog.is_channel or dialog.is_group else "ইউজার"
        print(f"[{count}] {chat_type}: {dialog.name}")
        print(f"    👉 Chat ID: {dialog.id}")
        if getattr(dialog.entity, "username", None):
            print(f"    👉 Username: @{dialog.entity.username}")
        print("-" * 40)

    if count == 0:
        print("⚠️ বটটি এখনও কোনো গ্রুপে অ্যাড করা হয়নি।")
        print("💡 বটটিকে আপনার গ্রুপে অ্যাড করুন এবং Admin বানান। তারপর আবার এই স্ক্রিপ্ট চালান।")

    print("\n✅ সম্পন্ন।")

if __name__ == "__main__":
    asyncio.run(main())
