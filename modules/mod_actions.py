import discord
import re
from datetime import datetime, timedelta
from typing import Optional
from _utils import _now
from mod_core import (
    MODULE_NAME, ModContext, ModConfig,
    ERROR_NO_PERMISSION, ERROR_REASON_REQUIRED,
    ERROR_CANNOT_ACTION_SELF, ERROR_CANNOT_ACTION_BOT, ERROR_HIGHER_ROLE,
    has_elevated_role, has_owner_role, validate_reason, parse_duration,
    _parse_fake_suffix, get_event_logger,
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
                dm = discord.Embed(
                    title="You have been banned",
                    description=f"You have been banned from **{ctx.guild.name}**",
                    color=0x992d22, timestamp=_now())
                dm.add_field(name="Reason",    value=dm_reason_field,    inline=False)
                dm.add_field(name="Moderator", value=str(ctx.author),    inline=True)
                dm.add_field(name="Appeal Process",
                             value="If you believe this ban was unjustified, submit an "
                                   "appeal below. Staff will vote within 24 hours.",
                             inline=False)
                dm.set_footer(text="Appeals are reviewed by server staff")
                await user.send(embed=dm, view=BanAppealView(ctx.guild.id))
            except discord.Forbidden:
                pass
            await ctx.guild.ban(user, reason=f"{reason} - By {ctx.author}",
                                 delete_message_days=delete_days)

        embed = discord.Embed(
            title="User Banned",
            description=f"{user.mention} has been banned.",
            color=0x992d22, timestamp=_now())
        if rule_number is not None:
            embed.add_field(name="Rule Violated", value=f"Rule {rule_number}", inline=True)
            embed.add_field(name="Rule Text",     value=rule_text,             inline=False)
            extra = reason[len(rule_text):].lstrip("|").strip()
            if extra:
                embed.add_field(name="Additional Note", value=extra, inline=False)
        else:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator",         value=ctx.author.mention,    inline=True)
        embed.add_field(name="Messages Deleted",  value=f"{delete_days} days", inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

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
        embed = discord.Embed(
            title="User Unbanned",
            description=f"{user.mention} has been unbanned.",
            color=0x2ecc71, timestamp=_now())
        embed.add_field(name="Reason",    value=reason,              inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention,  inline=True)
        await ctx.reply(embed=embed)
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
            dm = discord.Embed(
                title="You have been kicked",
                description=f"You have been kicked from **{ctx.guild.name}**",
                color=0xe67e22, timestamp=_now())
            dm.add_field(name="Reason",    value=reason,          inline=False)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            dm.set_footer(text="You can rejoin if you have an invite link")
            await member.send(embed=dm)
        except discord.Forbidden:
            pass
        if not fake:
            await member.kick(reason=f"{reason} - By {ctx.author}")
        embed = discord.Embed(
            title="Member Kicked",
            description=f"{member.mention} has been kicked.",
            color=0xe67e22, timestamp=_now())
        embed.add_field(name="Reason",    value=reason,             inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)
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
        embed = discord.Embed(
            title="Member Timed Out",
            description=f"{member.mention} timed out for **{duration}** minutes.",
            color=0xe74c3c, timestamp=_now())
        embed.add_field(name="Reason",    value=reason,              inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention,  inline=True)
        embed.add_field(name="Duration",  value=f"{duration} minutes", inline=True)
        await ctx.reply(embed=embed)
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
        embed = discord.Embed(
            title="Timeout Removed",
            description=f"{member.mention}'s timeout has been removed.",
            color=0x2ecc71, timestamp=_now())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
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
            dm = discord.Embed(title="You Have Been Muted",
                description=f"You have been muted in **{ctx.guild.name}**.",
                color=0xf39c12, timestamp=_now())
            dm.add_field(name="Reason", value=reason, inline=False)
            dm.add_field(name="Duration", value=duration_str, inline=True)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass
        embed = discord.Embed(title="Member Muted",
            description=f"{member.mention} has been muted.",
            color=0xf39c12, timestamp=_now())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Duration", value=duration_str, inline=True)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
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
        embed = discord.Embed(title="Member Unmuted",
            description=f"{member.mention} has been unmuted.",
            color=0x2ecc71, timestamp=_now())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
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
            await member.ban(reason=f"Softban: {reason} - By {ctx.author}",
                             delete_message_days=delete_days)
            await ctx.guild.unban(member, reason=f"Softban unban - By {ctx.author}")
        embed = discord.Embed(title="Member Softbanned",
            description=f"{member.mention} softbanned (messages deleted, can rejoin).",
            color=0x992d22, timestamp=_now())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Messages Deleted", value=f"{delete_days} days", inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)
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
            dm = discord.Embed(
                title="Warning",
                description=f"You have been warned in **{ctx.guild.name}**",
                color=0xf39c12, timestamp=_now())
            dm.add_field(name="Reason",          value=reason,               inline=False)
            dm.add_field(name="Moderator",        value=str(ctx.author),      inline=True)
            dm.add_field(name="Total Warnings",   value=str(strike_count),    inline=True)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass
        embed = discord.Embed(
            title="Member Warned",
            description=f"{member.mention} has been warned.",
            color=0xf39c12, timestamp=_now())
        embed.add_field(name="Reason",        value=reason,             inline=False)
        embed.add_field(name="Moderator",     value=ctx.author.mention, inline=True)
        embed.add_field(name="Total Warnings", value=str(strike_count), inline=True)
        await ctx.reply(embed=embed)
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
    embed = discord.Embed(
        title=f"Warnings for {member}",
        description=f"Total warnings: **{len(strikes)}**",
        color=0xf39c12, timestamp=_now())
    for i, s in enumerate(strikes[-10:], 1):
        ts = datetime.fromisoformat(s['timestamp']).strftime("%Y-%m-%d %H:%M UTC")
        embed.add_field(
            name=f"Warning {i}",
            value=f"**Reason:** {s['reason']}\n**Date:** {ts}", inline=False)
    if len(strikes) > 10:
        embed.set_footer(text=f"Showing last 10 of {len(strikes)} warnings")
    await ctx.reply(embed=embed, ephemeral=True)


async def _do_clearwarnings(ctx: ModContext, ms, member: discord.Member):
    if not has_owner_role(ctx.author, ms.cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    if ms.clear_strikes(member.id):
        embed = discord.Embed(
            title="Warnings Cleared",
            description=f"All warnings cleared for **{member}**.",
            color=0x2ecc71, timestamp=_now())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
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
            deleted = [None] * amount

        reason = f"Purged {len(deleted)} message(s)" + (
            f"from {target}" if target else "")
        embed = discord.Embed(
            title="Messages Purged",
            description=f"Deleted **{len(deleted)}** messages"
                        f"{f' from {target.mention}' if target else ''}.",
            color=0x2ecc71, timestamp=_now())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Channel",   value=ctx.channel.mention, inline=True)
        inchat_msg_id = await ctx.followup(embed=embed)
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
            embed = discord.Embed(title="Slowmode Disabled",
                                  description=f"Slowmode disabled in {target.mention}.",
                                  color=0x2ecc71)
        else:
            embed = discord.Embed(title="Slowmode Enabled",
                                  description=f"Slowmode set to **{seconds}s** in {target.mention}.",
                                  color=0x3498db)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
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
        embed = discord.Embed(
            title="Channel Locked",
            description=f"{target.mention} has been locked.",
            color=0xe74c3c, timestamp=_now())
        embed.add_field(name="Reason",    value=reason,             inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)
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
        embed = discord.Embed(
            title="Channel Unlocked",
            description=f"{target.mention} has been unlocked.",
            color=0x2ecc71, timestamp=_now())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unlocked {target.name}")
    except Exception as e:
        await ctx.error("An error occurred while trying to unlock the channel.")
        ctx.bot.logger.error(MODULE_NAME, "Unlock failed", e)
