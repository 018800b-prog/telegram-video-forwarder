"""
Telegram User Session Generator
===============================
Direct Code Prompt for +8801410064491
"""

import sys
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from config import API_ID, API_HASH, ENV_PATH

PHONE_NUMBER = "+8801410064491"

print("=" * 60)
print("🔑 Telegram User Session Generator")
print("=" * 60)
print(f"📱 ফোন নম্বর: {PHONE_NUMBER}")
print("টেলিগ্রাম সার্ভারের সাথে সংযোগ স্থাপন করা হচ্ছে...\n")

client = TelegramClient(StringSession(), API_ID, API_HASH)
client.connect()

if not client.is_user_authorized():
    print(f"📩 আপনার টেলিগ্রাম একাউন্টে ({PHONE_NUMBER}) লগইন কোড পাঠানো হচ্ছে...")
    phone_code_hash = client.send_code_request(PHONE_NUMBER).phone_code_hash
    print("✅ কোড পাঠানো হয়েছে! আপনার টেলিগ্রাম অ্যাপ বা ব্রাউজার চ্যাট চেক করুন।\n")
    
    code = input("👉 টেলিগ্রামে আসা ৫ ডিজিটের কোডটি এখানে লিখুন: ").strip()
    
    try:
        client.sign_in(phone=PHONE_NUMBER, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        print("\n🔐 আপনার একাউন্টে Two-Step Verification (পাসওয়ার্ড) অন আছে।")
        pwd = input("👉 আপনার টেলিগ্রাম টু-স্টেপ পাসওয়ার্ডটি লিখুন: ").strip()
        client.sign_in(password=pwd)

session_str = client.session.save()
me = client.get_me()

print("\n" + "=" * 60)
print(f"🎉 অভিনন্দন! সফলভাবে লগইন হয়েছে: {me.first_name} (@{me.username})")
print(f"🆔 আপনার Telegram ID: {me.id}")
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

print("\n💾 Session String সফলভাবে সেভ করা হয়েছে!")
print("এখন auto_forwarder.py নিজে নিজেই সোর্স গ্রুপ থেকে আপনার গ্রুপে ভিডিও আপলোড করতে পারবে।")
input("\nসমাপ্ত করতে কীবোর্ডের যেকোনো বাটন চাপুন...")
