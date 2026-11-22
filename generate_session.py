"""
Telegram User Session Generator
===============================
Pre-filled for +8801410064491
"""

import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from config import API_ID, API_HASH, ENV_PATH

PHONE_NUMBER = "+8801410064491"

print("=" * 60)
print("🔑 Telegram User Session Generator")
print("=" * 60)
print(f"📱 ফোন নম্বর: {PHONE_NUMBER}")
print("টেলিগ্রাম থেকে পাঠানো ৫ ডিজিটের লগইন কোডটি চাওয়া হবে।\n")

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    # Telethon will automatically prompt for code on this phone
    client.start(phone=PHONE_NUMBER)
    
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

    print("\n💾 Session String সফলভাবে .env ফাইলে সেভ হয়েছে!")
    print("এখন auto_forwarder.py সরাসরি কোনো ঝামেলা ছাড়াই চলতে পারবে।")
