import re
from typing import Optional, Union
from config import runtime_settings

def format_size(size_bytes: int) -> str:
    """Format bytes to human-readable string (KB, MB, GB)"""
    if not size_bytes:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"

def generate_caption(original_text: Optional[str]) -> Optional[str]:
    mode = runtime_settings.get("caption_mode", "original").lower()
    custom = runtime_settings.get("custom_caption", "")
    watermark = runtime_settings.get("watermark_text", "")
    text = (original_text or "").strip()

    if mode == "none":
        return None
    elif mode == "custom":
        return custom if custom else None
    elif mode == "append":
        if text and watermark:
            return f"{text}\n\n{watermark}"
        elif watermark:
            return watermark
        return text if text else None
    else:  # "original"
        return text if text else None

def parse_telegram_link(url: str) -> Optional[dict]:
    url = url.strip()
    
    # Private post link: t.me/c/<channel_id>/<message_id>
    private_match = re.search(r'(?:https?://)?(?:www\.)?t\.me/c/(\d+)/(\d+)', url)
    if private_match:
        channel_id = int(f"-100{private_match.group(1)}")
        msg_id = int(private_match.group(2))
        return {"channel": channel_id, "msg_id": msg_id, "is_private": True}

    # Public post link: t.me/<username>/<message_id>
    public_match = re.search(r'(?:https?://)?(?:www\.)?t\.me/([a-zA-Z0-9_]+)/(\d+)', url)
    if public_match:
        channel = public_match.group(1)
        if channel.lower() not in ["joinchat", "addstickers", "share", "c"]:
            msg_id = int(public_match.group(2))
            return {"channel": channel, "msg_id": msg_id, "is_private": False}

    return None

def clean_chat_input(input_str: str) -> Union[int, str]:
    """
    Cleans various formats of chat identifiers into an entity key:
    - https://t.me/mygroup -> mygroup
    - https://t.me/+AbCdEf -> +AbCdEf
    - @mygroup -> mygroup
    - -1001234567890 -> -1001234567890 (int)
    """
    s = input_str.strip()
    
    # Direct integer ID
    if (s.startswith("-") and s[1:].isdigit()) or s.isdigit():
        return int(s)

    # Standard link: https://t.me/username
    match_user = re.search(r'(?:https?://)?(?:www\.)?t\.me/([a-zA-Z0-9_]+)/?$', s)
    if match_user:
        uname = match_user.group(1)
        if uname.lower() not in ["joinchat", "c", "addstickers"]:
            return uname

    # Invite link: https://t.me/+hash or https://t.me/joinchat/hash
    match_inv = re.search(r'(?:https?://)?(?:www\.)?t\.me/(?:\+|joinchat/)([a-zA-Z0-9_-]+)', s)
    if match_inv:
        return s  # Return full link for client to resolve invite or entity

    # Username with @
    if s.startswith("@"):
        return s[1:]

    return s

def is_video_message(message) -> bool:
    if not message or not message.media:
        return False
    
    if getattr(message, "video", None):
        return True
    
    if getattr(message, "document", None):
        mime = getattr(message.document, "mime_type", "")
        if mime and mime.startswith("video/"):
            return True
        for attr in getattr(message.document, "attributes", []):
            file_name = getattr(attr, "file_name", "")
            if file_name and any(file_name.lower().endswith(ext) for ext in [".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".ts"]):
                return True

    return False
