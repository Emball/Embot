import discord
from discord import ui
import json
import io
from datetime import datetime
from typing import Optional, Dict
from _utils import _now
from mod_core import (
    MODULE_NAME, _db_exec, _db_one, _db_all,
    get_event_logger,
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


def log_bot_register(ms, message_id: int, log_id: str, text: str = "", color: int = 0,
                     footer: str = None, files_data: list = None, is_warning: bool = False,
                     warning_for_log_id: str = None):
    record = {
        'log_id':             log_id,
        'message_id':         message_id,
        'text':               text,
        'color':              color,
        'footer':             footer,
        'files_data':         files_data or [],
        'is_warning':         is_warning,
        'warning_for_log_id': warning_for_log_id,
        'timestamp':          _now().isoformat(),
    }
    ms._bot_log_cache[message_id] = record
    ms._bot_log_order.append(message_id)
    while len(ms._bot_log_order) > ms._bot_log_cache_size:
        ms._bot_log_cache.pop(ms._bot_log_order.popleft(), None)


async def send_bot_log(ms, guild: discord.Guild, *,
                       text: str = "", title: str = None, color: int = 0,
                       footer: str = None, files_data: list = None,
                       log_id: str = None) -> Optional[int]:
    ch = bot_logs_channel(ms, guild)
    if not ch:
        return None
    if log_id is None:
        log_id = f"LOG-{int(_now().timestamp() * 1000)}"

    view = discord.ui.LayoutView(timeout=None)
    if title or text:
        content = f"# {title}\n\n{text}" if title and text else (title or text)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(content), accent_color=color or None
        ))
    if footer:
        view.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        view.add_item(discord.ui.TextDisplay(f"-# {footer}"))

    try:
        if files_data:
            discord_files = [
                discord.File(fp=io.BytesIO(f['data']), filename=f['filename'])
                for f in files_data
            ]
            msg = await ch.send(view=view, files=discord_files)
        else:
            msg = await ch.send(view=view)
        log_bot_register(ms, msg.id, log_id, text, color, footer=footer,
                         files_data=files_data)
        return msg.id
    except Exception as e:
        ms.bot.logger.error(MODULE_NAME, f"Failed to send bot log: {e}")
        return None



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
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(discord.ui.Container(
                discord.ui.TextDisplay(f"# Ban Reverted\n\nAfter reviewing your case, we've decided to revert your ban from **{guild.name}**.\n\n**Rejoin Server**\nYou can rejoin using this invite:\n{invite_link}"),
                accent_color=0x2ecc71
            ))
            view.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            view.add_item(discord.ui.TextDisplay("-# This invite is for you only and will not expire"))
            await user.send(view=view)
        except discord.Forbidden:
            pass
        await _revert_botlog(ms, action, guild,
            title="Ban Reverted (Review System)",
            text=f"**{user}** ({user_id}) has been unbanned after review.")
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
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(discord.ui.Container(
                discord.ui.TextDisplay(f"# Mute Reverted\n\nAfter reviewing your case, your mute in **{guild.name}** has been reverted."),
                accent_color=0x2ecc71
            ))
            await member.send(view=view)
        except discord.Forbidden:
            pass
        await _revert_botlog(ms, action, guild,
            title="Mute Reverted (Review System)",
            text=f"**{member}** has been unmuted after review.")
        return True
    except Exception as e:
        ms.bot.logger.error(MODULE_NAME, f"Failed to revert mute {action['id']}", e)
        return False


async def _revert_botlog(ms, action: Dict, guild: discord.Guild, title: str, text: str):
    ch = bot_logs_channel(ms, guild)
    if ch:
        log_text = f"{text}\n\n**Original Reason** {action['reason']}\n**Original Moderator** {action['moderator']}"
        await send_bot_log(ms, guild, text=log_text, title=title, color=0x2ecc71)
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

    log_id     = record['log_id']
    orig_text  = record.get('text', '')
    orig_color = record.get('color', 0)
    orig_footer = record.get('footer', '')
    timestamp  = _now()

    _db_exec(ms._db,
        "INSERT INTO mod_deletion_attempts "
        "(log_id, deleter, deleter_id, timestamp, original_title, is_warning) "
        "VALUES (?,?,?,?,?,?)",
        (
            log_id, str(deleter), deleter.id,
            timestamp.isoformat(),
            (orig_text[:100] if orig_text else '(no title)'),
            int(record.get('is_warning', False)),
        ),
    )
    ms.bot.logger.log(
        MODULE_NAME,
        f"Bot-log deletion attempted by {deleter} (ID: {deleter.id}) "
        f"for log {log_id}", "WARNING")

    deletion_note = f"**Deletion Attempted By** {deleter.mention} (`{deleter}` | `{deleter.id}`)"
    footer = f"{orig_footer} - " if orig_footer else ""
    footer += f"Log ID: {log_id} - Deleting this will cause it to repost"
    repost_text = f"{deletion_note}\n\n{orig_text}"

    new_msg_id = await send_bot_log(
        ms, guild, text=repost_text, color=0xff0000, footer=footer,
        files_data=record.get('files_data'), log_id=log_id)
    if new_msg_id:
        ms._deletion_warnings[new_msg_id] = log_id


async def send_action_review(ms, owner: discord.User, action_id: str, action: Dict):
    parts = [f"**{action['action'].upper()} Action Review**"]
    if action['flags']:
        flags_text = []
        if 'red_flag' in action['flags']:
            flags_text.append("RED FLAG - Both embeds deleted")
        elif 'yellow_flag' in action['flags']:
            flags_text.append("YELLOW FLAG - Embed deleted")
        if 'inchat_deleted' in action['flags']:
            flags_text.append("In-chat embed deleted")
        if 'botlog_deleted' in action['flags']:
            flags_text.append("Bot-log embed deleted")
        parts.append("Flags: " + ", ".join(flags_text))
    parts.append(f"Moderator: {action['moderator']} (ID: {action['moderator_id']})")
    if action.get('user'):
        parts.append(f"User: {action['user']} (ID: {action['user_id']})")
    parts.append(f"Reason: {action['reason']}")
    if action.get('duration'):
        parts.append(f"Duration: {action['duration']}")
    if action['context_messages']:
        parts.append(f"Context: {len(action['context_messages'])} messages logged")
    view = ActionReviewView(ms, action_id, action)
    await owner.send(content="\n".join(parts), view=view)
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
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(discord.ui.Container(
                discord.ui.TextDisplay("No deletion attempts or mod-action flags in the last 24 hours."),
                accent_color=0x2ecc71
            ))
            await owner.send(view=view)
            return

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Daily Integrity Report\n\n**{len(attempt_rows)}** bot-log deletion attempt(s)\n**{len(red_flags)}** red-flag mod action(s)\n**{len(yellow_flags)}** yellow-flag mod action(s)"),
            accent_color=0xff4500
        ))
        await owner.send(view=view)

        if attempt_rows:
            detail_parts = ["# Bot-Log Deletion Attempts"]
            for attempt in attempt_rows[:20]:
                detail_parts.append(f"**Log** `{attempt['log_id']}`\n**By:** {attempt['deleter']} (`{attempt['deleter_id']}`)\n**Original:** {attempt['original_title']}\n**At:** {attempt['timestamp'][:19].replace('T', '')} UTC")
            detail_view = discord.ui.LayoutView(timeout=None)
            detail_view.add_item(discord.ui.Container(
                discord.ui.TextDisplay("\n\n".join(detail_parts)),
                accent_color=0xff0000
            ))
            if len(attempt_rows) > 20:
                detail_view.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
                detail_view.add_item(discord.ui.TextDisplay(f"-# ...and {len(attempt_rows) - 20} more."))
            await owner.send(view=detail_view)

        for action_id, action in (red_flags + yellow_flags)[:10]:
            await send_action_review(ms, owner, action_id, action)

        _db_exec(ms._db, "DELETE FROM mod_deletion_attempts")
        ms.bot.logger.log(MODULE_NAME, "Daily integrity report sent to owner")
    except Exception as e:
        ms.bot.logger.error(MODULE_NAME, "Failed to generate daily report", e)


def setup(bot):
    bot.logger.log(MODULE_NAME, "Mod oversight loaded")
