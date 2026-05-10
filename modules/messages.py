import discord
from cryptography.fernet import Fernet
from collections import deque
from typing import Optional, Dict, List
from _utils import _now

_fernet = Fernet(Fernet.generate_key())

# text cache: {guild_id: {channel_id: [msg_data, ...]}}
message_cache: Dict[str, Dict[str, list]] = {}
_msg_cache_max_channels = 200
_msg_cache_max_per_channel = 100

# media cache: {message_id: {files, author_id, guild_id, cached_at}}
media_cache: Dict[int, Dict] = {}
_media_cache_ttl = 3600


def encrypt(data: bytes) -> bytes:
    return _fernet.encrypt(data)

def decrypt(data: bytes) -> bytes:
    return _fernet.decrypt(data)

def delete_media(message_id: int):
    media_cache.pop(message_id, None)

def evict_media_ttl() -> int:
    cutoff = _now().timestamp() - _media_cache_ttl
    expired = [mid for mid, e in list(media_cache.items()) if e.get('cached_at', 0) < cutoff]
    for mid in expired:
        delete_media(mid)
    return len(expired)

async def cache_message(message: discord.Message):
    if message.guild is None or message.author.bot:
        return
    guild_id   = str(message.guild.id)
    channel_id = str(message.channel.id)
    guild_cache = message_cache.setdefault(guild_id, {})

    if channel_id not in guild_cache and len(guild_cache) >= _msg_cache_max_channels:
        evict_ch = next(iter(guild_cache))
        for m in guild_cache.pop(evict_ch, []):
            if m.get('id'):
                delete_media(m['id'])

    guild_cache.setdefault(channel_id, [])

    downloaded = []
    for att in message.attachments:
        try:
            data = await att.read()
            downloaded.append({
                'filename':     att.filename,
                'data':         encrypt(data),
                'content_type': att.content_type or 'application/octet-stream',
                'url':          att.url,
            })
        except Exception:
            pass

    if downloaded:
        media_cache[message.id] = {
            'files':     downloaded,
            'author_id': message.author.id,
            'guild_id':  message.guild.id,
            'cached_at': _now().timestamp(),
        }

    cache_list = guild_cache[channel_id]
    cache_list.append({
        'id':          message.id,
        'author':      str(message.author),
        'author_id':   message.author.id,
        'content':     message.content,
        'timestamp':   message.created_at.isoformat(),
        'attachments': [att.url for att in message.attachments],
        'embeds':      len(message.embeds),
    })
    if len(cache_list) > _msg_cache_max_per_channel:
        evicted = cache_list.pop(0)
        if evicted.get('id'):
            delete_media(evicted['id'])

def get_context_messages(
    guild_id: int, channel_id: int,
    around_message_id: int, count: int = 10
) -> List[Dict]:
    msgs = message_cache.get(str(guild_id), {}).get(str(channel_id), [])
    idx = next((i for i, m in enumerate(msgs) if m['id'] == around_message_id), None)
    if idx is None:
        return msgs[-count:]
    half = count // 2
    return msgs[max(0, idx - half):min(len(msgs), idx + half + 1)]

def get_recent_messages(guild_id: str, channel_id: str, limit: int = 20) -> List[str]:
    msgs = message_cache.get(guild_id, {}).get(channel_id, [])
    return [m.get('content', '') for m in msgs[-limit:] if m.get('content')]
