"""
Telegram User Session Generator
===============================
এই স্ক্রিপ্ট দিয়ে আপনার টেলিগ্রাম একাউন্টে লগইন করে একটি নিরাপদ Session String তৈরি করা হবে।
"""

import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from config import API_ID, API_HASH, ENV_PATH

print("=" * 60)
print("🔑 Telegram User Session Generator")
print("=" * 60)
print("আপনার টেলিগ্রাম একাউন্টে লগইন করতে হবে।")
print("লগইন কোডটি সরাসরি আপনার টেলিগ্রাম অ্যাপে আসবে।\n")

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    session_str = client.session.save()
    me = client.get_me()
    print("\n" + "=" * 60)
    print(f"✅ সফলভাবে লগইন হয়েছে: {me.first_name} (@{me.username})")
    print(f"🆔 আপনার আইডি: {me.id}")
    print("=" * 60)

    # Save to .env
    lines = []
    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if not line.startswith("SESSION_STRING="):
                    lines.append(line.rstrip())
    
    lines.append(f"SESSION_STRING={session_str}")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("\n💾 Session String সফলভাবে .env ফাইলে সেভ করা হয়েছে!")
    print("এখন স্ক্রিপ্টটি Render এবং আপনার পিসিতে সরাসরি কাজ করবে।")
