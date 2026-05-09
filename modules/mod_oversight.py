import discord
from discord import ui
import json
import io
from datetime import datetime, timedelta
from typing import Optional, Dict
from collections import deque
from _utils import _now
from mod_core import (
    MODULE_NAME, ModConfig, _db_exec, _db_one, _db_all,
    has_elevated_role, get_event_logger,
)


class ActionReviewView(ui.View):
    def __init__(self, moderation_system, action_id: str, action: Dict):
        super().__init__(timeout=None)
        self.moderation = moderation_system
        self.action_id  = action_id
        self.action     = action
        self.approve_btn.custom_id  = f"action_approve:{action_id}"
        self.revert_btn.custom_id   = f"action_revert:{action_id}"
        self.view_chat_btn.custom_id = f"action_viewchat:{action_id}"

    @ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="\u2705")
    async def approve_btn(self, interaction: discord.Interaction, button: ui.Button):
        action_id = button.custom_id.split(":", 1)[1]
        success = await approve_action(self.moderation, action_id)
        if success:
            await interaction.response.send_message(
                "Action approved and removed from pending.", ephemeral=True)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message(
                "Failed to approve action.", ephemeral=True)

    @ui.button(label="Revert", style=discord.ButtonStyle.red, emoji="\u21a9")
    async def revert_btn(self, interaction: discord.Interaction, button: ui.Button):
        action_id = button.custom_id.split(":", 1)[1]
        action    = action_get_pending(self.moderation, action_id)
        if not action:
            await interaction.response.send_message("Action not found.", ephemeral=True)
            return
        guild = self.moderation.bot.get_guild(action['guild_id'])
        if not guild:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return
        success = await action_revert(self.moderation, action_id, guild)
        if success:
            await interaction.response.send_message(
                "\u21a9 Action reverted successfully.", ephemeral=True)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message(
                "Failed to revert action.", ephemeral=True)

    @ui.button(label="View Chat", style=discord.ButtonStyle.gray, emoji="\uD83D\uDCAC")
    async def view_chat_btn(self, interaction: discord.Interaction, button: ui.Button):
        action_id = button.custom_id.split(":", 1)[1]
        action    = action_get_pending(self.moderation, action_id)
        if not action or not action.get('channel_id') or not action.get('message_id'):
            await interaction.response.send_message(
                "No chat link available.", ephemeral=True)
            return
        guild_id   = action['guild_id']
        channel_id = action['channel_id']
        message_id = action['message_id']
        jump_link  = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
        await interaction.response.send_message(
            f"[Jump to message]({jump_link})", ephemeral=True)


def bot_logs_channel(ms, guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch_id = ms.cfg.bot_logs_channel_id
    if ch_id:
        return guild.get_channel(ch_id)
    logger = get_event_logger(ms.bot)
    if logger:
        return logger.get_bot_logs_channel(guild)
    return None


def log_bot_register(ms, message_id: int, log_id: str, embed: discord.Embed,
                     files_data: list = None, is_warning: bool = False,
                     warning_for_log_id: str = None):
    record = {
        'log_id':             log_id,
        'message_id':         message_id,
        'embed': {
            'title':       embed.title,
            'description': embed.description,
            'color':       embed.color.value if embed.color else 0,
            'fields':      [{'name': f.name, 'value': f.value, 'inline': f.inline}
                             for f in embed.fields],
            'footer':      embed.footer.text if embed.footer else None,
            'image_url':   embed.image.url if embed.image else None,
            'author_name': embed.author.name if embed.author else None,
            'author_icon': embed.author.icon_url if embed.author else None,
        },
        'files_data':         files_data or [],
        'is_warning':         is_warning,
        'warning_for_log_id': warning_for_log_id,
        'timestamp':          _now().isoformat(),
    }
    ms._bot_log_cache[message_id] = record
    ms._bot_log_order.append(message_id)
    while len(ms._bot_log_order) > ms._bot_log_cache_size:
        ms._bot_log_cache.pop(ms._bot_log_order.popleft(), None)


async def send_bot_log(ms, guild: discord.Guild, embed: discord.Embed,
                       files_data: list = None, log_id: str = None) -> Optional[int]:
    ch = bot_logs_channel(ms, guild)
    if not ch:
        return None
    if log_id is None:
        log_id = f"LOG-{int(_now().timestamp() * 1000)}"

    try:
        if files_data:
            discord_files = [
                discord.File(fp=io.BytesIO(f['data']), filename=f['filename'])
                for f in files_data
            ]
            msg = await ch.send(embed=embed, files=discord_files)
        else:
            msg = await ch.send(embed=embed)
        log_bot_register(ms, msg.id, log_id, embed, files_data=files_data)
        return msg.id
    except Exception as e:
        ms.bot.logger.error(MODULE_NAME, f"Failed to send bot log: {e}")
        return None


async def send_cached_media_to_logs(ms, guild: discord.Message, message_id: int,
                                    author_str: str, reason: str, extra_content: str = None):
    ch = bot_logs_channel(ms, guild)
    if not ch:
        return
    cached = ms.media_cache.get(message_id)
    if not cached or not cached['files']:
        return
    files = []
    for f in cached['files']:
        try:
            data = ms._decrypt_from_disk(f['path'])
            files.append(discord.File(fp=io.BytesIO(data), filename=f['filename']))
        except Exception as e:
            ms.bot.logger.log(
                MODULE_NAME,
                f"Failed to decrypt cached file {f['filename']}: {e}", "WARNING")
    if not files:
        return
    embed = discord.Embed(title=reason, color=discord.Color.orange(), timestamp=_now())
    embed.add_field(name="User",       value=author_str,           inline=True)
    embed.add_field(name="Message ID", value=str(message_id),      inline=True)
    if extra_content:
        embed.add_field(name="Message Content",
                        value=extra_content[:1024] or "*empty*", inline=False)
    embed.set_footer(text=f"{len(files)} attachment(s) re-hosted below")
    await send_bot_log(ms, guild, embed, files_data=cached['files'])


def embed_track(ms, message_id: int, action_id: str, embed_type: str):
    ms.tracked_embeds[message_id] = {'action_id': action_id, 'type': embed_type}
    col_map = {'inchat': 'embed_id_inchat', 'botlog': 'embed_id_botlog'}
    col = col_map.get(embed_type)
    if col is None:
        ms.bot.logger.log(MODULE_NAME, f"embed_track: unknown embed_type {embed_type!r}", "WARNING")
        return
    queries = {
        'embed_id_inchat': "UPDATE mod_pending_actions SET embed_id_inchat=? WHERE action_id=?",
        'embed_id_botlog': "UPDATE mod_pending_actions SET embed_id_botlog=? WHERE action_id=?",
    }
    _db_exec(ms._db, queries[col], (message_id, action_id))


async def embed_handle_deletion(ms, message_id: int):
    if message_id not in ms.tracked_embeds:
        return
    info      = ms.tracked_embeds.pop(message_id)
    action_id = info['action_id']
    embed_type = info['type']

    row = _db_one(ms._db,
                  "SELECT flags FROM mod_pending_actions WHERE action_id=?", (action_id,))
    if not row:
        return
    flags = json.loads(row["flags"] or "[]")

    if embed_type == 'inchat':
        if 'inchat_deleted' not in flags:
            flags.append('inchat_deleted')
    else:
        if 'botlog_deleted' not in flags:
            flags.append('botlog_deleted')

    inchat_deleted = 'inchat_deleted' in flags
    botlog_deleted = 'botlog_deleted' in flags
    if inchat_deleted and botlog_deleted:
        if 'red_flag' not in flags:
            flags.append('red_flag')
            ms.bot.logger.log(
                MODULE_NAME,
                f"RED FLAG: Both embeds deleted for action {action_id}", "WARNING")
    elif inchat_deleted or botlog_deleted:
        if 'yellow_flag' not in flags:
            flags.append('yellow_flag')
            ms.bot.logger.log(
                MODULE_NAME,
                f"YELLOW FLAG: Embed deleted for action {action_id}", "WARNING")

    _db_exec(ms._db,
             "UPDATE mod_pending_actions SET flags=? WHERE action_id=?",
             (json.dumps(flags), action_id))


async def action_log(ms, action_data: Dict) -> Optional[str]:
    if action_data['action'] in ['mute', 'warn', 'timeout']:
        return None
    action_id = (f"{action_data['guild_id']}_{action_data['action']}_"
                 f"{int(_now().timestamp())}_{__import__('uuid').uuid4().hex[:8]}")
    context_messages = []
    if 'message_id' in action_data and 'channel_id' in action_data:
        context_messages = ms.get_context_messages(
            action_data['guild_id'],
            action_data['channel_id'],
            action_data['message_id'],
        )
    _db_exec(ms._db,
        "INSERT OR REPLACE INTO mod_pending_actions "
        "(action_id, action, moderator_id, moderator, user_id, user_name, reason, "
        "guild_id, channel_id, message_id, timestamp, context_messages, duration, "
        "additional, flags, embed_id_inchat, embed_id_botlog, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,'pending')",
        (
            action_id,
            action_data['action'],
            action_data['moderator_id'],
            action_data['moderator'],
            action_data.get('user_id'),
            action_data.get('user'),
            action_data['reason'],
            action_data['guild_id'],
            action_data.get('channel_id'),
            action_data.get('message_id'),
            _now().isoformat(),
            json.dumps(context_messages),
            action_data.get('duration'),
            json.dumps(action_data.get('additional', {})),
            json.dumps([]),
        ),
    )
    ms.bot.logger.log(
        MODULE_NAME,
        f"Logged mod action: {action_id} by {action_data['moderator']}")
    return action_id


def action_get_pending(ms, action_id: str) -> Optional[Dict]:
    row = _db_one(ms._db,
                  "SELECT * FROM mod_pending_actions WHERE action_id=?", (action_id,))
    if not row:
        return None
    return action_row_to_dict(row)


def action_row_to_dict(row) -> Dict:
    return {
        'id':               row["action_id"],
        'action':           row["action"],
        'moderator_id':     row["moderator_id"],
        'moderator':        row["moderator"],
        'user_id':          row["user_id"],
        'user':             row["user_name"],
        'reason':           row["reason"],
        'guild_id':         row["guild_id"],
        'channel_id':       row["channel_id"],
        'message_id':       row["message_id"],
        'timestamp':        row["timestamp"],
        'context_messages': json.loads(row["context_messages"] or "[]"),
        'duration':         row["duration"],
        'additional':       json.loads(row["additional"] or "{}"),
        'flags':            json.loads(row["flags"] or "[]"),
        'embed_ids': {
            'inchat': row["embed_id_inchat"],
            'botlog': row["embed_id_botlog"],
        },
        'status': row["status"],
    }


def action_resolve_pending(ms, user_id: int, action_type: str):
    row = _db_one(ms._db,
        "SELECT action_id FROM mod_pending_actions "
        "WHERE user_id=? AND action=? AND status='pending' "
        "ORDER BY timestamp DESC LIMIT 1",
        (user_id, action_type),
    )
    if row:
        _db_exec(ms._db,
                 "DELETE FROM mod_pending_actions WHERE action_id=?",
                 (row["action_id"],))


async def approve_action(ms, action_id: str) -> bool:
    row = _db_one(ms._db,
                  "SELECT 1 FROM mod_pending_actions WHERE action_id=?", (action_id,))
    if not row:
        return False
    _db_exec(ms._db,
             "DELETE FROM mod_pending_actions WHERE action_id=?", (action_id,))
    ms.bot.logger.log(MODULE_NAME, f"Action {action_id} approved")
    return True


async def action_revert(ms, action_id: str, guild: discord.Guild) -> bool:
    action = action_get_pending(ms, action_id)
    if not action:
        return False
    if action['action'] == 'ban':
        return await _revert_ban(ms, action, guild)
    elif action['action'] == 'mute':
        return await _revert_mute(ms, action, guild)
    else:
        _db_exec(ms._db,
                 "DELETE FROM mod_pending_actions WHERE action_id=?", (action_id,))
        return True


async def _revert_ban(ms, action: Dict, guild: discord.Guild) -> bool:
    try:
        user_id = action['user_id']
        user    = await ms.bot.fetch_user(user_id)
        await guild.unban(user, reason="Ban reverted after review")
        invite_link = await _create_ban_reversal_invite(ms, guild, user_id)
        try:
            embed = discord.Embed(
                title="Ban Reverted",
                description=f"After reviewing your case, we've decided to revert "
                            f"your ban from **{guild.name}**.",
                color=0x2ecc71, timestamp=_now())
            embed.add_field(name="Rejoin Server",
                            value=f"You can rejoin using this invite:\n{invite_link}",
                            inline=False)
            embed.set_footer(text="This invite is for you only and will not expire")
            await user.send(embed=embed)
        except discord.Forbidden:
            pass
        await _revert_botlog(ms, action, guild,
            title="Ban Reverted (Review System)",
            description=f"**{user}** ({user_id}) has been unbanned after review.")
        return True
    except Exception as e:
        ms.bot.logger.error(MODULE_NAME, f"Failed to revert ban {action['id']}", e)
        return False


async def _revert_mute(ms, action: Dict, guild: discord.Guild) -> bool:
    try:
        user_id    = action['user_id']
        member     = guild.get_member(user_id)
        if not member:
            return False
        muted_role = discord.utils.get(guild.roles, name=ms.cfg.muted_role_name)
        if muted_role and muted_role in member.roles:
            await member.remove_roles(muted_role, reason="Mute reverted after review")
        try:
            embed = discord.Embed(
                title="Mute Reverted",
                description=f"After reviewing your case, your mute in "
                            f"**{guild.name}** has been reverted.",
                color=0x2ecc71, timestamp=_now())
            await member.send(embed=embed)
        except discord.Forbidden:
            pass
        await _revert_botlog(ms, action, guild,
            title="Mute Reverted (Review System)",
            description=f"**{member}** has been unmuted after review.")
        return True
    except Exception as e:
        ms.bot.logger.error(MODULE_NAME, f"Failed to revert mute {action['id']}", e)
        return False


async def _revert_botlog(ms, action: Dict, guild: discord.Guild, title: str, description: str):
    ch = bot_logs_channel(ms, guild)
    if ch:
        log_embed = discord.Embed(
            title=title, description=description,
            color=0x2ecc71, timestamp=_now())
        log_embed.add_field(name="Original Reason", value=action['reason'], inline=False)
        log_embed.add_field(name="Original Moderator", value=action['moderator'], inline=True)
        await send_bot_log(ms, guild, log_embed)
    _db_exec(ms._db,
             "DELETE FROM mod_pending_actions WHERE action_id=?", (action['id'],))


async def _create_ban_reversal_invite(ms, guild: discord.Guild, user_id: int) -> str:
    try:
        channel = next(
            (ch for ch in guild.text_channels
             if ch.permissions_for(guild.me).create_instant_invite),
            None,
        )
        if not channel:
            return "Could not create invite - no suitable channel"
        invite = await channel.create_invite(
            max_uses=1, max_age=0, unique=True,
            reason=f"Ban reversal for user {user_id}")
        key = f"{guild.id}_{user_id}"
        _db_exec(ms._db,
            "INSERT INTO mod_invites (invite_key, code, user_id, guild_id, created_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(invite_key) DO UPDATE SET "
            "code=excluded.code, created_at=excluded.created_at",
            (key, invite.code, user_id, guild.id, _now().isoformat()),
        )
        return invite.url
    except Exception as e:
        ms.bot.logger.error(MODULE_NAME, "Failed to create ban reversal invite", e)
        return "Error creating invite"


async def handle_bot_log_deletion(ms, message_id: int, deleter: discord.Member, guild: discord.Guild):
    record = ms._bot_log_cache.get(message_id)
    if not record:
        return

    log_id             = record['log_id']
    original_embed_data = record['embed']
    timestamp           = _now()

    _db_exec(ms._db,
        "INSERT INTO mod_deletion_attempts "
        "(log_id, deleter, deleter_id, timestamp, original_title, is_warning) "
        "VALUES (?,?,?,?,?,?)",
        (
            log_id, str(deleter), deleter.id,
            timestamp.isoformat(),
            original_embed_data.get('title') or '(no title)',
            int(record.get('is_warning', False)),
        ),
    )
    ms.bot.logger.log(
        MODULE_NAME,
        f"Bot-log deletion attempted by {deleter} (ID: {deleter.id}) "
        f"for log {log_id}", "WARNING")

    embed = discord.Embed(
        title=original_embed_data.get('title'),
        description=original_embed_data.get('description'),
        color=0xff0000, timestamp=timestamp)
    for field in original_embed_data.get('fields', []):
        embed.add_field(
            name=field['name'], value=field['value'], inline=field['inline'])
    if original_embed_data.get('author_name'):
        embed.set_author(
            name=original_embed_data['author_name'],
            icon_url=original_embed_data.get('author_icon') or None)
    if original_embed_data.get('image_url'):
        embed.set_image(url=original_embed_data['image_url'])
    embed.add_field(
        name="Deletion Attempted By",
        value=f"{deleter.mention} (`{deleter}` | `{deleter.id}`)", inline=False)
    original_footer = original_embed_data.get('footer') or ''
    embed.set_footer(
        text=f"{original_footer + ' - ' if original_footer else ''}"
             f"Log ID: {log_id} - Deleting this will cause it to repost")

    new_msg_id = await send_bot_log(
        ms, guild, embed, files_data=record.get('files_data'), log_id=log_id)
    if new_msg_id:
        ms._deletion_warnings[new_msg_id] = log_id


async def send_action_review(ms, owner: discord.User, action_id: str, action: Dict):
    embed = discord.Embed(
        title=f"{action['action'].upper()} Action Review",
        color=(0xe74c3c if 'red_flag' in action['flags'] else
               0xf39c12 if 'yellow_flag' in action['flags'] else 0x5865f2),
        timestamp=datetime.fromisoformat(action['timestamp']),
    )
    if action['flags']:
        flags_text = []
        if 'red_flag' in action['flags']:
            flags_text.append("**RED FLAG** - Both embeds deleted")
        elif 'yellow_flag' in action['flags']:
            flags_text.append("**YELLOW FLAG** - Embed deleted")
        if 'inchat_deleted' in action['flags']:
            flags_text.append("In-chat embed deleted")
        if 'botlog_deleted' in action['flags']:
            flags_text.append("Bot-log embed deleted")
        embed.add_field(name="Flags", value="\n".join(flags_text), inline=False)
    embed.add_field(
        name="Moderator",
        value=f"{action['moderator']} (ID: {action['moderator_id']})", inline=True)
    if action.get('user'):
        embed.add_field(
            name="User",
            value=f"{action['user']} (ID: {action['user_id']})", inline=True)
    embed.add_field(name="Reason", value=action['reason'], inline=False)
    if action.get('duration'):
        embed.add_field(name="Duration", value=action['duration'], inline=True)
    if action['context_messages']:
        embed.add_field(
            name="Context",
            value=f"{len(action['context_messages'])} messages logged", inline=True)
    view = ActionReviewView(ms, action_id, action)
    await owner.send(embed=embed, view=view)
    if action['context_messages']:
        lines = []
        for msg in action['context_messages']:
            ts = msg.get('timestamp', '')
            author = msg.get('author', 'unknown')
            content = (msg.get('content') or '').strip()
            att = "[Attachment]" if msg.get('attachments') else ""
            emb = "[Embed]" if msg.get('embeds', 0) > 0 else ""
            body = content or att or emb or "[Empty]"
            lines.append(f"[{ts}] {author}: {body}")
        text_dump = "\n".join(lines)
        await owner.send(file=discord.File(io.BytesIO(text_dump.encode('utf-8')), "context.txt"))


async def generate_daily_report(ms):
    try:
        owner = await ms.bot.fetch_user(ms.cfg.owner_id)
        if not owner:
            return

        attempt_rows = _db_all(ms._db,
                               "SELECT * FROM mod_deletion_attempts ORDER BY id")
        _db_exec(ms._db, "DELETE FROM mod_deletion_attempts")

        red_flags    = []
        yellow_flags = []
        for row in _db_all(ms._db,
            "SELECT * FROM mod_pending_actions WHERE flags != '[]' AND flags != 'null'"
        ):
            action = action_row_to_dict(row)
            if 'red_flag' in action['flags']:
                red_flags.append((action['id'], action))
            elif 'yellow_flag' in action['flags']:
                yellow_flags.append((action['id'], action))

        total_issues = len(attempt_rows) + len(red_flags) + len(yellow_flags)

        if total_issues == 0:
            embed = discord.Embed(
                title="Daily Integrity Report",
                description="No deletion attempts or mod-action flags in the last 24 hours.",
                color=0x2ecc71, timestamp=_now())
            await owner.send(embed=embed)
            return

        embed = discord.Embed(
            title="Daily Integrity Report",
            description=(
                f"**{len(attempt_rows)}** bot-log deletion attempt(s)\n"
                f"**{len(red_flags)}**  red-flag mod action(s)\n"
                f"**{len(yellow_flags)}**  yellow-flag mod action(s)"
            ),
            color=0xff4500, timestamp=_now())
        await owner.send(embed=embed)

        if attempt_rows:
            detail = discord.Embed(
                title="Bot-Log Deletion Attempts",
                color=0xff0000, timestamp=_now())
            for attempt in attempt_rows[:20]:
                detail.add_field(
                    name=f"Log `{attempt['log_id']}`",
                    value=(
                        f"**By:** {attempt['deleter']} (`{attempt['deleter_id']}`)\n"
                        f"**Original:** {attempt['original_title']}\n"
                        f"**At:** {attempt['timestamp'][:19].replace('T', '')} UTC"
                    ),
                    inline=False)
            if len(attempt_rows) > 20:
                detail.set_footer(text=f"...and {len(attempt_rows) - 20} more.")
            await owner.send(embed=detail)

        for action_id, action in (red_flags + yellow_flags)[:10]:
            await send_action_review(ms, owner, action_id, action)

        ms.bot.logger.log(MODULE_NAME, "Daily integrity report sent to owner")
    except Exception as e:
        ms.bot.logger.error(MODULE_NAME, "Failed to generate daily report", e)
