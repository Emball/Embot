import asyncio
import discord
import pytz
from datetime import datetime, timedelta
from typing import Optional
from _utils import _now, script_dir
from mod_core import (
    MODULE_NAME, ModContext,
    ERROR_NO_PERMISSION,
    ERROR_CANNOT_ACTION_SELF, ERROR_CANNOT_ACTION_BOT, ERROR_HIGHER_ROLE,
    has_elevated_role, has_owner_role, validate_reason, parse_duration,
    get_event_logger,
)
from mod_rules import RulesManager
from mod_appeals import BanAppealView
from mod_oversight import action_log, embed_track, action_resolve_pending


async def _do_ban(ctx: ModContext, ms, user: discord.User, reason: str = None,
                  delete_days: int = 0, fake: bool = False, rule_number: int = None):
    cfg = ms.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)

    rule_text = None
    if rule_number is not None:
        rules_mgr: Optional[RulesManager] = getattr(ctx.bot, "rules_manager", None)
        if rules_mgr:
            rule_text = rules_mgr.get_rule_text(rule_number)
        if not rule_text:
            return await ctx.error(f"Rule **{rule_number}** not found.")
        reason = f"{rule_text} | {reason.strip()}" if reason and reason.strip() else rule_text

    ok, err = validate_reason(reason, cfg.min_reason_length)
    if not ok:
        return await ctx.error(err)
    if user == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if user == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    member = ctx.guild.get_member(user.id)
    if member and member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.error(ERROR_HIGHER_ROLE)

    delete_days = max(0, min(7, delete_days))
    try:
        dm_reason_field = reason
        if rule_number is not None and rule_text:
            dm_reason_field = f"**Rule {rule_number} violation**\n{rule_text}"

        if not fake:
            try:
                dm_text = (
                    f"**You have been banned**\n\nYou have been banned from **{ctx.guild.name}**\n\n"
                    f"**Reason** {dm_reason_field}\n"
                    f"**Moderator** {ctx.author}\n"
                    f"**Appeal Process** If you believe this ban was unjustified, submit an "
                    f"appeal below. Staff will vote within 24 hours.\n"
                    f"-# Appeals are reviewed by server staff"
                )
                await user.send(content=dm_text, view=BanAppealView(ctx.guild.id))
            except discord.Forbidden:
                pass
            ms._bot_initiated_bans.add(user.id)
            await ctx.guild.ban(user, reason=f"{reason} - By {ctx.author}",
                                 delete_message_days=delete_days)

        parts = [f"# User Banned\n\n{user.mention} has been banned."]
        if rule_number is not None:
            parts.append(f"**Rule Violated** Rule {rule_number}")
            parts.append(f"**Rule Text** {rule_text}")
            extra = reason[len(rule_text):].lstrip("|").strip()
            if extra:
                parts.append(f"**Additional Note** {extra}")
        else:
            parts.append(f"**Reason** {reason}")
        parts.append(f"**Moderator** {ctx.author.mention}")
        parts.append(f"**Messages Deleted** {delete_days} days")
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n\n".join(parts)),
            accent_color=0x992d22
        ))
        inchat_msg_id = await ctx.reply(view=view)

        if not fake:
            el             = get_event_logger(ctx.bot)
            botlog_msg_id  = None
            if el:
                botlog_msg_id = await el.log_ban(
                    ctx.guild, user, ctx.author, reason, delete_days, ctx.channel)
            action_id = await action_log(ms, {
                'action': 'ban', 'moderator_id': ctx.author.id,
                'moderator': str(ctx.author), 'user_id': user.id, 'user': str(user),
                'reason': reason, 'guild_id': ctx.guild.id,
                'channel_id': ctx.channel.id,
                'message_id': ctx.message.id if ctx.message else None,
                'duration': None, 'additional': {'delete_days': delete_days},
            })
            if inchat_msg_id and action_id:
                embed_track(ms, inchat_msg_id, action_id, 'inchat')
            if botlog_msg_id and action_id:
                embed_track(ms, botlog_msg_id, action_id, 'botlog')

        ctx.bot.logger.log(MODULE_NAME,
            f"{'[FAKE] ' if fake else ''}{ctx.author} banned {user}")
    except discord.Forbidden:
        await ctx.error("I don't have permission to ban this user.")
    except Exception as e:
        await ctx.error("An error occurred while trying to ban the user.")
        ctx.bot.logger.error(MODULE_NAME, "Ban failed", e)


async def _do_unban(ctx: ModContext, ms, user_id: str, reason: str = "No reason provided",
                    fake: bool = False):
    cfg = ms.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    try:
        user = await ctx.bot.fetch_user(int(user_id))
        if not fake:
            await ctx.guild.unban(user, reason=f"{reason} - By {ctx.author}")
            action_resolve_pending(ms, user.id, 'ban')
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# User Unbanned\n\n{user.mention} has been unbanned.\n\n**Reason** {reason}\n**Moderator** {ctx.author.mention}"),
            accent_color=0x2ecc71
        ))
        await ctx.reply(view=view)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unbanned {user}")
    except ValueError:
        await ctx.error("Invalid user ID.")
    except discord.NotFound:
        await ctx.error("User not found or not banned.")
    except Exception as e:
        await ctx.error("An error occurred while trying to unban.")
        ctx.bot.logger.error(MODULE_NAME, "Unban failed", e)


async def _do_kick(ctx: ModContext, ms, member: discord.Member, reason: str, fake: bool = False):
    cfg = ms.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason, cfg.min_reason_length)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.error(ERROR_HIGHER_ROLE)
    try:
        try:
            kick_dm = discord.ui.LayoutView(timeout=None)
            kick_dm.add_item(discord.ui.Container(
                discord.ui.TextDisplay(f"# You have been kicked\n\nYou have been kicked from **{ctx.guild.name}**\n\n**Reason** {reason}\n**Moderator** {ctx.author}"),
                accent_color=0xe67e22
            ))
            kick_dm.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            kick_dm.add_item(discord.ui.TextDisplay("-# You can rejoin if you have an invite link"))
            await member.send(view=kick_dm)
        except discord.Forbidden:
            pass
        if not fake:
            await member.kick(reason=f"{reason} - By {ctx.author}")
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Member Kicked\n\n{member.mention} has been kicked.\n\n**Reason** {reason}\n**Moderator** {ctx.author.mention}"),
            accent_color=0xe67e22
        ))
        inchat_msg_id = await ctx.reply(view=view)
        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_kick(
                ctx.guild, member, ctx.author, reason, ctx.channel)
        action_id = await action_log(ms, {
            'action': 'kick', 'moderator_id': ctx.author.id,
            'moderator': str(ctx.author), 'user_id': member.id, 'user': str(member),
            'reason': reason, 'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
        })
        if inchat_msg_id and action_id:
            embed_track(ms, inchat_msg_id, action_id, 'inchat')
        if botlog_msg_id and action_id:
            embed_track(ms, botlog_msg_id, action_id, 'botlog')
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} kicked {member}")
    except Exception as e:
        await ctx.error("An error occurred while trying to kick the member.")
        ctx.bot.logger.error(MODULE_NAME, "Kick failed", e)


async def _do_timeout(ctx: ModContext, ms, member: discord.Member, duration: int,
                      reason: str, fake: bool = False):
    cfg = ms.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason, cfg.min_reason_length)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    if not (1 <= duration <= 40320):
        return await ctx.error("Duration must be between 1 and 40320 minutes.")
    try:
        if not fake:
            await member.timeout(
                _now() + timedelta(minutes=duration),
                reason=f"{reason} - By {ctx.author}")
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Member Timed Out\n\n{member.mention} timed out for **{duration}** minutes.\n\n**Reason** {reason}\n**Moderator** {ctx.author.mention}\n**Duration** {duration} minutes"),
            accent_color=0xe74c3c
        ))
        await ctx.reply(view=view)
        el = get_event_logger(ctx.bot)
        if el:
            await el.log_timeout(
                ctx.guild, member, ctx.author, reason, f"{duration} minutes", ctx.channel)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} timed out {member} for {duration}m")
    except Exception as e:
        await ctx.error("An error occurred while trying to timeout the member.")
        ctx.bot.logger.error(MODULE_NAME, "Timeout failed", e)


async def _do_untimeout(ctx: ModContext, ms, member: discord.Member, fake: bool = False):
    cfg = ms.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    try:
        if not fake:
            await member.timeout(None, reason=f"Timeout removed by {ctx.author}")
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Timeout Removed\n\n{member.mention}'s timeout has been removed.\n\n**Moderator** {ctx.author.mention}"),
            accent_color=0x2ecc71
        ))
        await ctx.reply(view=view)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} removed timeout from {member}")
    except Exception as e:
        await ctx.error("An error occurred while removing the timeout.")
        ctx.bot.logger.error(MODULE_NAME, "Untimeout failed", e)


async def _do_mute(ctx: ModContext, ms, member: discord.Member, reason: str = "No reason provided",
                   duration: Optional[str] = None, fake: bool = False):
    cfg = ms.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    duration_seconds, duration_str = parse_duration(duration or "")
    try:
        muted_role = discord.utils.get(ctx.guild.roles, name=cfg.muted_role_name)
        if not muted_role:
            muted_role = await ctx.guild.create_role(
                name=cfg.muted_role_name, color=discord.Color.dark_gray(),
                reason="Creating Muted role for moderation")
            for ch in ctx.guild.channels:
                try:
                    await ch.set_permissions(muted_role, send_messages=False, speak=False)
                except Exception:
                    pass
        if not fake:
            await member.add_roles(muted_role, reason=reason)
            ms.add_mute(ctx.guild.id, member.id, reason, ctx.author, duration_seconds)
        try:
            mute_dm = discord.ui.LayoutView(timeout=None)
            mute_dm.add_item(discord.ui.Container(
                discord.ui.TextDisplay(f"# You Have Been Muted\n\nYou have been muted in **{ctx.guild.name}**.\n\n**Reason** {reason}\n**Duration** {duration_str}\n**Moderator** {ctx.author}"),
                accent_color=0xf39c12
            ))
            await member.send(view=mute_dm)
        except discord.Forbidden:
            pass
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Member Muted\n\n{member.mention} has been muted.\n\n**Reason** {reason}\n**Duration** {duration_str}\n**Moderator** {ctx.author.mention}"),
            accent_color=0xf39c12
        ))
        await ctx.reply(view=view)
        el = get_event_logger(ctx.bot)
        if el:
            await el.log_mute(ctx.guild, member, ctx.author, reason, duration_str, ctx.channel)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} muted {member} for {duration_str}")
    except discord.Forbidden:
        await ctx.error("I don't have permission to mute this member.")
    except Exception as e:
        await ctx.error("An error occurred while trying to mute the member.")
        ctx.bot.logger.error(MODULE_NAME, "Mute failed", e)


async def _do_unmute(ctx: ModContext, ms, member: discord.Member, fake: bool = False):
    cfg = ms.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    muted_role = discord.utils.get(ctx.guild.roles, name=cfg.muted_role_name)
    if not muted_role or muted_role not in member.roles:
        return await ctx.error("This member is not muted.")
    try:
        if not fake:
            await member.remove_roles(muted_role, reason=f"Unmuted by {ctx.author}")
            ms.remove_mute(ctx.guild.id, member.id)
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Member Unmuted\n\n{member.mention} has been unmuted.\n\n**Moderator** {ctx.author.mention}"),
            accent_color=0x2ecc71
        ))
        await ctx.reply(view=view)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unmuted {member}")
    except Exception as e:
        await ctx.error("An error occurred while trying to unmute the member.")
        ctx.bot.logger.error(MODULE_NAME, "Unmute failed", e)


async def _do_softban(ctx: ModContext, ms, member: discord.Member, reason: str,
                      delete_days: int = 7, fake: bool = False):
    cfg = ms.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason, cfg.min_reason_length)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.error(ERROR_HIGHER_ROLE)
    delete_days = max(0, min(7, delete_days))
    try:
        if not fake:
            ms._bot_initiated_bans.add(member.id)
            await member.ban(reason=f"Softban: {reason} - By {ctx.author}",
                             delete_message_days=delete_days)
            await ctx.guild.unban(member, reason=f"Softban unban - By {ctx.author}")
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Member Softbanned\n\n{member.mention} softbanned (messages deleted, can rejoin).\n\n**Reason** {reason}\n**Moderator** {ctx.author.mention}\n**Messages Deleted** {delete_days} days"),
            accent_color=0x992d22
        ))
        inchat_msg_id = await ctx.reply(view=view)
        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_softban(
                ctx.guild, member, ctx.author, reason, delete_days, ctx.channel)
        action_id = await action_log(ms, {
            'action': 'softban', 'moderator_id': ctx.author.id,
            'moderator': str(ctx.author), 'user_id': member.id, 'user': str(member),
            'reason': reason, 'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
            'additional': {'delete_days': delete_days},
        })
        if action_id:
            if inchat_msg_id:
                embed_track(ms, inchat_msg_id, action_id, 'inchat')
            if botlog_msg_id:
                embed_track(ms, botlog_msg_id, action_id, 'botlog')
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} softbanned {member}")
    except Exception as e:
        await ctx.error("An error occurred while trying to softban the member.")
        ctx.bot.logger.error(MODULE_NAME, "Softban failed", e)


async def _do_warn(ctx: ModContext, ms, member: discord.Member, reason: str, fake: bool = False):
    cfg = ms.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason, cfg.min_reason_length)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    try:
        strike_count = (ms.get_strikes(member.id) + 1
                        if fake else ms.add_strike(member.id, reason))
        try:
            warn_dm = discord.ui.LayoutView(timeout=None)
            warn_dm.add_item(discord.ui.Container(
                discord.ui.TextDisplay(f"# Warning\n\nYou have been warned in **{ctx.guild.name}**\n\n**Reason** {reason}\n**Moderator** {ctx.author}\n**Total Warnings** {strike_count}"),
                accent_color=0xf39c12
            ))
            await member.send(view=warn_dm)
        except discord.Forbidden:
            pass
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Member Warned\n\n{member.mention} has been warned.\n\n**Reason** {reason}\n**Moderator** {ctx.author.mention}\n**Total Warnings** {strike_count}"),
            accent_color=0xf39c12
        ))
        await ctx.reply(view=view)
        el = get_event_logger(ctx.bot)
        if el:
            await el.log_warn(
                ctx.guild, member, ctx.author, reason, strike_count, ctx.channel)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} warned {member}")
    except Exception as e:
        await ctx.error("An error occurred while trying to warn the member.")
        ctx.bot.logger.error(MODULE_NAME, "Warn failed", e)


async def _do_warnings(ctx: ModContext, ms, member: discord.Member):
    if not has_elevated_role(ctx.author, ms.cfg):
        return await ctx.error("You don't have permission to view warnings.")
    strikes = ms.get_strike_details(member.id)
    if not strikes:
        return await ctx.reply(f"**{member}** has no warnings.", ephemeral=True)
    parts = [f"# Warnings for {member}", f"Total warnings: **{len(strikes)}**"]
    for i, s in enumerate(strikes[-10:], 1):
        ts = datetime.fromisoformat(s['timestamp']).strftime("%Y-%m-%d %H:%M UTC")
        parts.append(f"**Warning {i}**\n**Reason:** {s['reason']}\n**Date:** {ts}")
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay("\n\n".join(parts)),
        accent_color=0xf39c12
    ))
    if len(strikes) > 10:
        view.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        view.add_item(discord.ui.TextDisplay(f"-# Showing last 10 of {len(strikes)} warnings"))
    await ctx.reply(view=view, ephemeral=True)


async def _do_clearwarnings(ctx: ModContext, ms, member: discord.Member):
    if not has_owner_role(ctx.author, ms.cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    if ms.clear_strikes(member.id):
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Warnings Cleared\n\nAll warnings cleared for **{member}**.\n\n**Moderator** {ctx.author.mention}"),
            accent_color=0x2ecc71
        ))
        await ctx.reply(view=view)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} cleared warnings for {member}")
    else:
        await ctx.reply(f"**{member}** has no warnings to clear.", ephemeral=True)


async def _do_purge(ctx: ModContext, ms, amount: int, target: Optional[discord.Member] = None,
                    fake: bool = False):
    if not has_elevated_role(ctx.author, ms.cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    if not (1 <= amount <= 100):
        return await ctx.error("Amount must be between 1 and 100.")
    if not isinstance(ctx._source, discord.Interaction):
        try:
            await ctx._source.message.delete()
        except Exception:
            pass
    await ctx.defer()
    try:
        check   = (lambda m: m.author.id == target.id) if target else (lambda m: True)
        deleted = [] if fake else await ctx.channel.purge(limit=amount, check=check)
        if fake:
            deleted = []

        reason = f"Purged {len(deleted)} message(s)" + (
            f"from {target}" if target else "")
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Messages Purged\n\nDeleted **{len(deleted)}** messages{f' from {target.mention}' if target else ''}.\n\n**Moderator** {ctx.author.mention}\n**Channel** {ctx.channel.mention}"),
            accent_color=0x2ecc71
        ))
        inchat_msg_id = await ctx.followup(view=view)
        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_purge(
                ctx.guild, ctx.author, len(deleted), ctx.channel, target)
        action_id = await action_log(ms, {
            'action': 'purge', 'moderator_id': ctx.author.id,
            'moderator': str(ctx.author),
            'user_id': target.id if target else None,
            'user': str(target) if target else None,
            'reason': reason, 'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
            'additional': {'amount': len(deleted)},
        })
        if action_id:
            if inchat_msg_id:
                embed_track(ms, inchat_msg_id, action_id, 'inchat')
            if botlog_msg_id:
                embed_track(ms, botlog_msg_id, action_id, 'botlog')
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} purged {len(deleted)} messages")
    except Exception as e:
        await ctx.followup("An error occurred while trying to purge messages.",
                           ephemeral=True)
        ctx.bot.logger.error(MODULE_NAME, "Purge failed", e)


def _build_sweep_view(title: str, body: str, color: int) -> discord.ui.LayoutView:
    v = discord.ui.LayoutView(timeout=None)
    v.add_item(discord.ui.Container(discord.ui.TextDisplay(f"{title}\n\n{body}"), accent_color=color))
    return v


async def _do_sweep(ctx: ModContext, ms, users_raw: str, keywords_raw: str,
                    channels_raw: str = None, after_raw: str = None,
                    before_raw: str = None, fake: bool = False):
    if not has_elevated_role(ctx.author, ms.cfg):
        return await ctx.error(ERROR_NO_PERMISSION)

    user_ids = []
    for part in users_raw.split():
        stripped = part.strip("<@!> ")
        if stripped.isdigit():
            user_ids.append(int(stripped))
        else:
            name_lower = part.lower()
            match = discord.utils.find(
                lambda m: m.name.lower() == name_lower or (m.nick or "").lower() == name_lower,
                ctx.guild.members)
            if match:
                user_ids.append(match.id)
    user_ids = list(dict.fromkeys(user_ids))
    if not user_ids:
        return await ctx.error("No valid users provided.")
    ctx.bot.logger.log(MODULE_NAME, f"Sweep user_ids resolved: {user_ids}")

    keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]
    if not keywords:
        return await ctx.error("No valid keywords provided.")

    after_dt = before_dt = None
    try:
        if after_raw:
            after_dt = datetime.strptime(after_raw, "%Y-%m-%d").replace(tzinfo=pytz.utc)
        if before_raw:
            before_dt = datetime.strptime(before_raw, "%Y-%m-%d").replace(tzinfo=pytz.utc)
    except ValueError:
        return await ctx.error("Invalid date format. Use YYYY-MM-DD.")

    if not channels_raw or channels_raw.strip().lower() == "all":
        scan_channels = [c for c in ctx.guild.text_channels
                         if c.permissions_for(ctx.guild.me).read_message_history
                         and c.permissions_for(ctx.guild.me).manage_messages]
    else:
        scan_channels = []
        for part in channels_raw.split():
            cid = part.strip("<#> ")
            if cid.isdigit():
                ch = ctx.guild.get_channel(int(cid))
                if ch:
                    scan_channels.append(ch)
        if not scan_channels:
            return await ctx.error("No valid channels found.")

    user_mentions = " ".join(f"<@{uid}>" for uid in user_ids)
    kw_display = ", ".join(f"`{k}`" for k in keywords)
    ch_display = ("all channels" if (not channels_raw or channels_raw.strip().lower() == "all")
                  else " ".join(f"<#{c.id}>" for c in scan_channels))
    date_range = ""
    if after_dt:
        date_range += f" after {after_raw}"
    if before_dt:
        date_range += f" before {before_raw}"

    prefix = "[FAKE] " if fake else ""
    confirm_body = (
        f"**Users** {user_mentions}\n"
        f"**Keywords** {kw_display}\n"
        f"**Channels** {ch_display}\n"
        f"**Scope** Full message history{date_range}\n\n"
        f"This will scan **every message** in the target channels. May take a while. Confirm?"
    )
    confirm_view = _SweepConfirmView(ctx.author.id)
    confirm_content = f"**{prefix}Sweep Confirmation**\n\n{confirm_body}"
    interaction = ctx._source if isinstance(ctx._source, discord.Interaction) else None
    if interaction:
        await interaction.response.send_message(confirm_content, view=confirm_view, ephemeral=True)
        ctx._replied = True
    else:
        await ctx.reply(confirm_content, view=confirm_view, ephemeral=True)

    await confirm_view.wait()
    if not confirm_view.confirmed:
        return await ctx.followup("Sweep cancelled.", ephemeral=True)

    try:
        status_msg = await ctx.channel.send(
            view=_build_sweep_view(
                f"## {prefix}Sweep in Progress",
                f"**Users** {user_mentions}\n**Keywords** {kw_display}\n"
                f"**Status** Starting scan across {len(scan_channels)} channel(s)...",
                0x3498db),
            allowed_mentions=discord.AllowedMentions.none())

        _status_gone = False
        async def _update_status(body: str, color: int = 0x3498db, title: str = None):
            nonlocal _status_gone
            if _status_gone:
                return
            try:
                await status_msg.edit(
                    view=_build_sweep_view(title or f"## {prefix}Sweep in Progress", body, color),
                    allowed_mentions=discord.AllowedMentions.none())
            except discord.NotFound:
                _status_gone = True
            except Exception as e:
                ctx.bot.logger.error(MODULE_NAME, "Sweep status update failed", e)

        to_delete: list[discord.Message] = []
        channels_hit: dict[int, int] = {}
        total_scanned = 0

        async def _wait_for_session():
            for _ in range(60):
                try:
                    if not ctx.bot.http._HTTPClient__session.closed:
                        return True
                except Exception:
                    pass
                await asyncio.sleep(2)
            return False

        # Query VMS transcript DB for matching voice messages from target users
        vms_to_delete: list[tuple[int, int]] = []  # (message_id, channel_id)
        try:
            vms_db = script_dir() / "db" / "vms.db"
            if vms_db.exists():
                import sqlite3 as _sq
                with _sq.connect(str(vms_db)) as _vc:
                    placeholders = ",".join("?" * len(user_ids))
                    kw_clauses   = " OR ".join("LOWER(transcript) LIKE ?" for _ in keywords)
                    kw_params    = [f"%{kw}%" for kw in keywords]
                    rows = _vc.execute(
                        f"SELECT discord_message_id, discord_channel_id FROM vms "
                        f"WHERE CAST(guild_id AS INTEGER) IN ({placeholders}) "
                        f"AND discord_message_id IS NOT NULL "
                        f"AND deleted_at IS NULL "
                        f"AND transcript IS NOT NULL "
                        f"AND ({kw_clauses})",
                        [*user_ids, *kw_params]
                    ).fetchall()
                # Filter by user — VMS doesn't store author_id, so fetch message author
                for (msg_id_str, ch_id_str) in rows:
                    try:
                        ch = ctx.guild.get_channel(int(ch_id_str))
                        if not ch:
                            continue
                        msg = await ch.fetch_message(int(msg_id_str))
                        if msg.author.id in user_ids:
                            vms_to_delete.append((int(msg_id_str), int(ch_id_str)))
                    except Exception:
                        pass
                ctx.bot.logger.log(MODULE_NAME,
                    f"Sweep VMS: found {len(vms_to_delete)} matching voice message(s)")
        except Exception as e:
            ctx.bot.logger.error(MODULE_NAME, "Sweep VMS query failed", e)

        # Scan in 200-message chunks oldest_first with a cursor so we can resume on disconnect
        for idx, channel in enumerate(scan_channels, 1):
            count = 0
            scanned = 0
            cursor = after_dt
            await _update_status(
                f"**Users** {user_mentions}\n**Keywords** {kw_display}\n"
                f"**Scanning** {channel.mention} ({idx}/{len(scan_channels)}) "
                f"— {total_scanned:,} messages scanned so far...")
            try:
                while True:
                    chunk_count = 0
                    try:
                        async for message in channel.history(
                                limit=200, after=cursor, before=before_dt, oldest_first=True):
                            chunk_count += 1
                            scanned += 1
                            total_scanned += 1
                            cursor = message
                            if message.author.id in user_ids and any(
                                    kw in message.content.lower() for kw in keywords):
                                to_delete.append(message)
                                count += 1
                        if chunk_count < 200:
                            break  # got less than a full chunk — channel exhausted
                    except RuntimeError as e:
                        if "Session is closed" in str(e):
                            ctx.bot.logger.log(MODULE_NAME,
                                f"Sweep: session closed mid-scan #{channel.name}, waiting to reconnect...")
                            await _update_status(
                                f"**Users** {user_mentions}\n**Keywords** {kw_display}\n"
                                f"**Scanning** {channel.mention} ({idx}/{len(scan_channels)}) "
                                f"— ⚠ Reconnecting... ({total_scanned:,} scanned, {len(to_delete)} matches)")
                            if not await _wait_for_session():
                                raise RuntimeError("Bot session did not recover in time")
                            await asyncio.sleep(3)
                            ctx.bot.logger.log(MODULE_NAME, f"Sweep: session recovered, resuming #{channel.name}")
                            continue
                        raise
                    if chunk_count > 0:
                        await _update_status(
                            f"**Users** {user_mentions}\n**Keywords** {kw_display}\n"
                            f"**Scanning** {channel.mention} ({idx}/{len(scan_channels)}) "
                            f"— {total_scanned:,} messages scanned, {len(to_delete)} matches...")
                    await asyncio.sleep(0.1)
            except discord.Forbidden:
                ctx.bot.logger.log(MODULE_NAME, f"Sweep: no access to #{channel.name}")
            except Exception as e:
                ctx.bot.logger.error(MODULE_NAME, f"Sweep scan error in #{channel.name}", e)
            ctx.bot.logger.log(MODULE_NAME,
                f"Sweep #{channel.name}: scanned {scanned:,}, matched {count}")
            if count:
                channels_hit[channel.id] = count

        ctx.bot.logger.log(MODULE_NAME,
            f"Sweep scan complete — {total_scanned:,} messages scanned, {len(to_delete)} matches, "
            f"{len(vms_to_delete)} VM(s)")

        if not to_delete and not vms_to_delete:
            await _update_status(
                f"**Users** {user_mentions}\n**Keywords** {kw_display}\n"
                f"**Scanned** {total_scanned:,} messages\n**Result** No matching messages found.",
                color=0x2ecc71, title=f"## {prefix}Sweep Complete")
            return

        vms_deleted = 0
        vms_failed  = 0
        if not fake and vms_to_delete:
            await _update_status(
                f"**Users** {user_mentions}\n**Keywords** {kw_display}\n"
                f"**Scanned** {total_scanned:,} messages\n"
                f"**Deleting** {len(vms_to_delete)} voice message(s)...")
            for (vm_msg_id, vm_ch_id) in vms_to_delete:
                try:
                    ch  = ctx.guild.get_channel(vm_ch_id)
                    msg = await ch.fetch_message(vm_msg_id)
                    await msg.delete()
                    vms_deleted += 1
                except discord.NotFound:
                    vms_deleted += 1
                except Exception as e:
                    vms_failed += 1
                    ctx.bot.logger.error(MODULE_NAME, f"Sweep VM delete failed {vm_msg_id}", e)
                await asyncio.sleep(0.75)
        elif fake:
            vms_deleted = len(vms_to_delete)

        total = len(to_delete)
        deleted = 0
        failed = 0
        cutoff = _now() - timedelta(days=14)

        by_channel: dict[int, list[discord.Message]] = {}
        for m in to_delete:
            by_channel.setdefault(m.channel.id, []).append(m)

        def _status_body(extra: str = "") -> str:
            vm_part = f" · {vms_deleted} VM(s)" if vms_deleted else ""
            return (
                f"**Users** {user_mentions}\n**Keywords** {kw_display}\n"
                f"**Scanned** {total_scanned:,} messages\n"
                f"**Progress** {deleted}/{total} deleted{vm_part}"
                + (f" · {failed} failed" if failed else "")
                + (f"\n{extra}" if extra else "")
            )

        if not fake:
            for channel in scan_channels:
                msgs = by_channel.get(channel.id, [])
                if not msgs:
                    continue
                bulk = [m for m in msgs if m.created_at.replace(tzinfo=pytz.utc) > cutoff]
                old  = [m for m in msgs if m.created_at.replace(tzinfo=pytz.utc) <= cutoff]
                # Bulk delete recent messages (up to 100 at a time, Discord limit)
                for i in range(0, len(bulk), 100):
                    chunk = bulk[i:i+100]
                    try:
                        await channel.delete_messages(chunk)
                        deleted += len(chunk)
                    except Exception as e:
                        failed += len(chunk)
                        ctx.bot.logger.error(MODULE_NAME, f"Sweep bulk delete error in #{channel.name}", e)
                    await _update_status(_status_body(f"**Channel** {channel.mention}"))
                    await asyncio.sleep(1.0)
                # Old messages must be deleted one at a time — throttle to avoid rate limits
                for m in old:
                    try:
                        await m.delete()
                        deleted += 1
                    except discord.NotFound:
                        pass
                    except Exception:
                        failed += 1
                    await asyncio.sleep(0.75)
                    if deleted % 10 == 0:
                        await _update_status(_status_body(f"**Channel** {channel.mention}"))
                await _update_status(_status_body(f"**Channel** {channel.mention} ✓"))
        else:
            deleted = total

        ctx.bot.logger.log(MODULE_NAME,
            f"{'[FAKE] ' if fake else ''}{ctx.author} swept {deleted} msgs "
            f"from {len(user_ids)} user(s) across {len(channels_hit)} channel(s) "
            f"({total_scanned:,} scanned)")

        el = get_event_logger(ctx.bot)
        if el:
            await el.log_sweep(
                ctx.guild, ctx.author, user_ids, keywords, deleted,
                channels_hit, ctx.guild, fake=fake)

        result_body = (
            f"**Deleted** {deleted} message(s){' (simulated)' if fake else ''}"
            + (f" + {vms_deleted} VM(s)" if vms_deleted else "") + "\n"
            f"**Users** {user_mentions}\n"
            f"**Keywords** {kw_display}\n"
            f"**Scanned** {total_scanned:,} messages total\n"
            f"**Channels Hit** {len(channels_hit)}"
            + (f"\n**Failed** {failed + vms_failed} (permissions or age)" if failed + vms_failed else "")
        )
        await _update_status(result_body, color=0x2ecc71, title=f"## {prefix}Sweep Complete")

    except Exception as e:
        ctx.bot.logger.error(MODULE_NAME, "Sweep execution failed", e)
        await ctx.followup("Sweep failed — check bot logs.", ephemeral=True)


class _SweepConfirmView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.confirmed = False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your sweep.", ephemeral=True)
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your sweep.", ephemeral=True)
        await interaction.response.defer()
        self.stop()


async def _do_slowmode(ctx: ModContext, ms, seconds: int,
                       channel: Optional[discord.TextChannel] = None):
    cfg = ms.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    if not (0 <= seconds <= 21600):
        return await ctx.error("Slowmode must be between 0 and 21600 seconds.")
    target = channel or ctx.channel
    try:
        await target.edit(slowmode_delay=seconds,
                          reason=f"Slowmode set by {ctx.author}")
        if seconds == 0:
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(discord.ui.Container(
                discord.ui.TextDisplay(f"# Slowmode Disabled\n\nSlowmode disabled in {target.mention}.\n\n**Moderator** {ctx.author.mention}"),
                accent_color=0x2ecc71
            ))
        else:
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(discord.ui.Container(
                discord.ui.TextDisplay(f"# Slowmode Enabled\n\nSlowmode set to **{seconds}s** in {target.mention}.\n\n**Moderator** {ctx.author.mention}"),
                accent_color=0x3498db
            ))
        await ctx.reply(view=view)
        ctx.bot.logger.log(
            MODULE_NAME, f"{ctx.author} set slowmode to {seconds}s in {target.name}")
    except Exception as e:
        await ctx.error("An error occurred while trying to set slowmode.")
        ctx.bot.logger.error(MODULE_NAME, "Slowmode failed", e)


async def _do_lock(ctx: ModContext, ms, reason: str,
                   channel: Optional[discord.TextChannel] = None, fake: bool = False):
    cfg = ms.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason, cfg.min_reason_length)
    if not ok:
        return await ctx.error(err)
    target = channel or ctx.channel
    try:
        if not fake:
            await target.set_permissions(
                ctx.guild.default_role, send_messages=False,
                reason=f"{reason} - By {ctx.author}")
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Channel Locked\n\n{target.mention} has been locked.\n\n**Reason** {reason}\n**Moderator** {ctx.author.mention}"),
            accent_color=0xe74c3c
        ))
        inchat_msg_id = await ctx.reply(view=view)
        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_lock(ctx.guild, ctx.author, reason, target)
        action_id = await action_log(ms, {
            'action': 'lock', 'moderator_id': ctx.author.id,
            'moderator': str(ctx.author), 'user_id': None, 'user': None,
            'reason': reason, 'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
            'additional': {'channel': target.id},
        })
        if action_id:
            if inchat_msg_id:
                embed_track(ms, inchat_msg_id, action_id, 'inchat')
            if botlog_msg_id:
                embed_track(ms, botlog_msg_id, action_id, 'botlog')
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} locked {target.name}")
    except Exception as e:
        await ctx.error("An error occurred while trying to lock the channel.")
        ctx.bot.logger.error(MODULE_NAME, "Lock failed", e)


async def _do_unlock(ctx: ModContext, ms, channel: Optional[discord.TextChannel] = None):
    cfg = ms.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    target = channel or ctx.channel
    try:
        await target.set_permissions(
            ctx.guild.default_role, send_messages=None,
            reason=f"Unlocked by {ctx.author}")
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Channel Unlocked\n\n{target.mention} has been unlocked.\n\n**Moderator** {ctx.author.mention}"),
            accent_color=0x2ecc71
        ))
        await ctx.reply(view=view)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unlocked {target.name}")
    except Exception as e:
        await ctx.error("An error occurred while trying to unlock the channel.")
        ctx.bot.logger.error(MODULE_NAME, "Unlock failed", e)


def setup(bot):
    bot.logger.log(MODULE_NAME, "Mod actions loaded")
