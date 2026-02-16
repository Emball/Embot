# [file name]: moderation.py
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import re
from datetime import datetime, timedelta
import asyncio
from typing import Optional, Union
import json
import os
from pathlib import Path

MODULE_NAME = "MODERATION"

# ==================== CONFIGURATION ====================

CONFIG = {
    "elevated_roles": ["Moderator", "Admin", "Owner"],
    "moderation": {"min_reason_length": 10, "muted_role_name": "Muted"}
}

# Severity categories (From OG file)
CHILD_SAFETY = ["child porn", "Teen leaks"]
RACIAL_SLURS = ["chink", "beaner", "n i g g e r", "nigger", "nigger'", "Nigger", 
                "niggers", "niiger", "niigger"]
TOS_VIOLATIONS = []
BANNED_WORDS = [
    "embis", "embis'", "Embis", "embis!", "Embis!", "embis's", "embiss", "embiz",
    "https://www.youtube.com/watch?v=fXvOrWWB3Vg", "https://youtu.be/fXvOrWWB3Vg",
    "https://youtu.be/fXvOrWWB3Vg?si=rSS11Yf2si_MVauu", "leaked porn", "nudes leak",
    "mbis", "m'bis", "Mbis", "mbs", "mebis", "Michael Blake Sinclair", 
    "Michael Sinclair", "montear", "www.youtube.com/watch?v=fXvOrWWB3Vg", 
    "youtube.com/watch?v=fXvOrWWB3Vg"
]

ELEVATED_ROLES = CONFIG.get("elevated_roles", ["Moderator", "Admin", "Owner"])
MIN_REASON_LENGTH = CONFIG.get("moderation", {}).get("min_reason_length", 10)
MUTED_ROLE_NAME = CONFIG.get("moderation", {}).get("muted_role_name", "Muted")

# Standard Errors
ERROR_NO_PERMISSION = "❌ You need a moderation role (Moderator, Admin, or Owner) to use this command."
ERROR_REASON_REQUIRED = "❌ You must provide a reason for this action."
ERROR_REASON_TOO_SHORT = f"❌ Reason must be at least {MIN_REASON_LENGTH} characters long."
ERROR_CANNOT_ACTION_SELF = "❌ You cannot perform this action on yourself."
ERROR_CANNOT_ACTION_BOT = "❌ I cannot perform this action on myself."
ERROR_HIGHER_ROLE = "❌ You cannot perform this action on someone with a higher or equal role."

# ==================== HELPERS ====================

def has_elevated_role(member: discord.Member) -> bool:
    if member.guild.owner_id == member.id:
        return True
    return any(role.name in ELEVATED_ROLES for role in member.roles)

def validate_reason(reason: Optional[str]) -> tuple:
    if not reason or reason.strip() == "" or reason == "No reason provided":
        return False, ERROR_REASON_REQUIRED
    if len(reason) < MIN_REASON_LENGTH:
        return False, ERROR_REASON_TOO_SHORT
    return True, None

def parse_duration(duration: str) -> tuple:
    """Parse a duration string like '10m', '2h', '1d'. Returns (seconds, label)."""
    if not duration:
        return None, "Permanent"
    m = re.match(r'^(\d+)([smhd])$', duration.lower())
    if not m:
        return None, "Permanent"
    value, unit = int(m.group(1)), m.group(2)
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    labels      = {'s': 'second', 'm': 'minute', 'h': 'hour', 'd': 'day'}
    seconds = value * multipliers[unit]
    label   = f"{value} {labels[unit]}{'s' if value != 1 else ''}"
    return seconds, label

def get_event_logger(bot):
    """Return the logger's EventLogger if available."""
    return getattr(bot, '_logger_event_logger', None)

# ==================== UNIFIED CONTEXT ====================

class ModContext:
    """
    Wraps either a discord.Interaction (slash) or commands.Context (prefix)
    into a single interface so command logic never has to branch on which one it got.
    """
    def __init__(self, source):
        self._source = source
        self._replied = False

        if isinstance(source, discord.Interaction):
            self.guild = source.guild
            self.channel = source.channel
            self.author = source.user
            self.bot = source.client
            self.message = None # Interactions don't have a message object in the same way
        else:
            self.guild = source.guild
            self.channel = source.channel
            self.author = source.author
            self.bot = source.bot
            self.message = source.message

    async def reply(self, content=None, *, embed=None, ephemeral=False, delete_after=None):
        """Send a response. Returns the message ID if available."""
        msg_obj = None
        if isinstance(self._source, discord.Interaction):
            if not self._replied:
                self._replied = True
                await self._source.response.send_message(
                    content=content, embed=embed, ephemeral=ephemeral)
                if not ephemeral:
                    try:
                        msg_obj = await self._source.original_response()
                    except Exception:
                        pass
            else:
                msg_obj = await self._source.followup.send(
                    content=content, embed=embed, ephemeral=ephemeral)
        else:
            msg_obj = await self._source.send(content=content, embed=embed)
            if delete_after and msg_obj:
                await msg_obj.delete(delay=delete_after)
        
        return msg_obj.id if msg_obj else None

    async def error(self, message: str):
        """Send an error message."""
        if isinstance(self._source, discord.Interaction):
            await self.reply(message, ephemeral=True)
        else:
            await self.reply(message, delete_after=8)

    async def defer(self):
        if isinstance(self._source, discord.Interaction):
            await self._source.response.defer()
        self._replied = True

    async def followup(self, content=None, *, embed=None, ephemeral=False):
        if isinstance(self._source, discord.Interaction):
            msg = await self._source.followup.send(
                content=content, embed=embed, ephemeral=ephemeral)
            return msg.id if msg else None
        else:
            msg = await self._source.send(content=content, embed=embed)
            return msg.id if msg else None

# ==================== OVERSIGHT HELPERS ====================

async def log_to_oversight(ctx: ModContext, action: str, user_id: Optional[int],
                           user_str: Optional[str], reason: str, inchat_msg_id: Optional[int],
                           botlog_msg_id: Optional[int], duration: Optional[str] = None,
                           additional: Optional[dict] = None) -> Optional[str]:
    """Log to mod_oversight if available."""
    if not hasattr(ctx.bot, 'mod_oversight'):
        return None
    
    try:
        action_id = await ctx.bot.mod_oversight.log_mod_action({
            'action': action,
            'moderator_id': ctx.author.id,
            'moderator': str(ctx.author),
            'user_id': user_id,
            'user': user_str,
            'reason': reason,
            'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
            'duration': duration,
            'additional': additional or {}
        })
        
        # Action ID might be None if the action type was ignored (e.g. mute/warn)
        if action_id:
            if inchat_msg_id:
                ctx.bot.mod_oversight.track_embed(inchat_msg_id, action_id, 'inchat')
            if botlog_msg_id:
                ctx.bot.mod_oversight.track_embed(botlog_msg_id, action_id, 'botlog')
        
        return action_id
    except Exception as e:
        ctx.bot.logger.error(MODULE_NAME, "Failed to log action to oversight", e)
        return None

# ==================== VIEWS & MANAGERS ====================

class BanAppealView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @ui.button(label="Submit Appeal", style=discord.ButtonStyle.primary, emoji="📝")
    async def appeal_button(self, interaction: discord.Interaction, button: ui.Button):
        if not hasattr(interaction.client, 'mod_oversight'):
            await interaction.response.send_message("❌ Appeal system not available.", ephemeral=True)
            return
        # Dynamically import to avoid circular dependency issues if any
        try:
            from mod_oversight import BanAppealModal
            modal = BanAppealModal(interaction.client.mod_oversight, self.guild_id)
            await interaction.response.send_modal(modal)
        except ImportError:
            await interaction.response.send_message("❌ Appeal modal not found.", ephemeral=True)

class RolePersistenceManager:
    """Manages role persistence (from OG file)"""
    def __init__(self, bot):
        self.bot = bot
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        self.roles_file = str(data_dir / "member_roles.json")
        self.role_cache = self._load()

    def _load(self):
        try:
            with open(self.roles_file) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load role cache", e)
            return {}

    def _save(self):
        try:
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.roles_file), suffix='.tmp')
            with os.fdopen(fd, 'w') as f:
                json.dump(self.role_cache, f, indent=2)
            os.replace(tmp, self.roles_file)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save role cache", e)

    def save_member_roles(self, member: discord.Member):
        gk, uk = str(member.guild.id), str(member.id)
        self.role_cache.setdefault(gk, {})[uk] = {
            'role_ids': [r.id for r in member.roles if r.id != member.guild.id],
            'saved_at': datetime.utcnow().isoformat(),
            'username': str(member)
        }
        self._save()

    async def restore_member_roles(self, member: discord.Member):
        gk, uk = str(member.guild.id), str(member.id)
        saved = self.role_cache.get(gk, {}).get(uk)
        if not saved:
            return
        roles = [member.guild.get_role(rid) for rid in saved.get('role_ids', [])]
        roles = [r for r in roles if r]
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Role persistence - restoring previous roles")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to restore roles for {member}", e)

class StrikeSystem:
    """Manages strikes (from OG file)"""
    def __init__(self, bot):
        self.bot = bot
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        self.strikes_file = str(data_dir / "moderation_strikes.json")
        self.strikes = self._load()

    def _load(self):
        try:
            with open(self.strikes_file) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load strikes", e)
            return {}

    def _save(self):
        try:
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.strikes_file), suffix='.tmp')
            with os.fdopen(fd, 'w') as f:
                json.dump(self.strikes, f, indent=2)
            os.replace(tmp, self.strikes_file)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save strikes", e)

    def add_strike(self, user_id, reason) -> int:
        key = str(user_id)
        self.strikes.setdefault(key, []).append({
            'timestamp': datetime.utcnow().isoformat(), 'reason': reason})
        self._save()
        return len(self.strikes[key])

    def get_strikes(self, user_id) -> int:
        return len(self.strikes.get(str(user_id), []))

    def get_strike_details(self, user_id) -> list:
        return self.strikes.get(str(user_id), [])

    def clear_strikes(self, user_id) -> bool:
        key = str(user_id)
        if key in self.strikes:
            del self.strikes[key]
            self._save()
            return True
        return False

class MuteManager:
    """Manages Mutes (from OG file)"""
    def __init__(self, bot):
        self.bot = bot
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        self.mutes_file = str(data_dir / "muted_users.json")
        self.muted_role_name = MUTED_ROLE_NAME
        self.mutes = self._load()

    def _load(self):
        try:
            with open(self.mutes_file) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load mutes", e)
            return {}

    def _save(self):
        try:
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.mutes_file), suffix='.tmp')
            with os.fdopen(fd, 'w') as f:
                json.dump(self.mutes, f, indent=2)
            os.replace(tmp, self.mutes_file)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save mutes", e)

    def add_mute(self, guild_id, user_id, reason, moderator, duration_seconds=None):
        gk, uk = str(guild_id), str(user_id)
        expiry = None
        if duration_seconds:
            expiry = (datetime.utcnow() + timedelta(seconds=duration_seconds)).isoformat()
        self.mutes.setdefault(gk, {})[uk] = {
            'user_id': user_id, 'reason': reason, 'moderator': str(moderator),
            'timestamp': datetime.utcnow().isoformat(),
            'duration_seconds': duration_seconds, 'expiry_time': expiry
        }
        self._save()

    def remove_mute(self, guild_id, user_id):
        gk, uk = str(guild_id), str(user_id)
        if gk in self.mutes and uk in self.mutes[gk]:
            del self.mutes[gk][uk]
            self._save()

    def is_muted(self, guild_id, user_id) -> bool:
        return str(user_id) in self.mutes.get(str(guild_id), {})

    def get_expired_mutes(self) -> list:
        expired, now = [], datetime.utcnow()
        for gk, users in self.mutes.items():
            for uk, data in users.items():
                expiry = data.get('expiry_time')
                if expiry:
                    try:
                        if now >= datetime.fromisoformat(expiry):
                            expired.append({'guild_id': int(gk), 'user_id': data['user_id'],
                                            'user_key': uk, 'guild_key': gk})
                    except (ValueError, AttributeError):
                        pass
        return expired

class ModerationManager:
    def __init__(self, bot):
        self.bot = bot
        self.strike_system    = StrikeSystem(bot)
        self.mute_manager     = MuteManager(bot)
        self.role_persistence = RolePersistenceManager(bot)

# ==================== UNIFIED COMMAND LOGIC ====================

async def _do_ban(ctx: ModContext, mgr: ModerationManager,
                  user: discord.User, reason: str, delete_days: int = 0):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason)
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
        # Send Appeal DM
        try:
            dm = discord.Embed(title="🔨 You have been banned",
                               description=f"You have been banned from **{ctx.guild.name}**",
                               color=0x992d22, timestamp=datetime.utcnow())
            dm.add_field(name="Reason", value=reason, inline=False)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            dm.add_field(name="📝 Appeal Process",
                         value="If you believe this ban was unjustified, submit an appeal below.",
                         inline=False)
            dm.set_footer(text="Appeals are reviewed by server staff")
            await user.send(embed=dm, view=BanAppealView(ctx.guild.id))
        except discord.Forbidden:
            pass

        # Perform Action
        await ctx.guild.ban(user, reason=f"{reason} - By {ctx.author}",
                            delete_message_days=delete_days)

        # In-Chat Response
        embed = discord.Embed(title="✅ User Banned",
                              description=f"{user.mention} has been banned.",
                              color=0x992d22, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Messages Deleted", value=f"{delete_days} days", inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        # Logs
        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_ban(ctx.guild, user, ctx.author, reason, delete_days, ctx.channel)
        
        await log_to_oversight(ctx, 'ban', user.id, str(user), reason, inchat_msg_id, botlog_msg_id,
                               additional={'delete_days': delete_days})
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} banned {user}")

    except discord.Forbidden:
        await ctx.error("❌ I don't have permission to ban this user.")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to ban the user.")
        ctx.bot.logger.error(MODULE_NAME, "Ban failed", e)

async def _do_unban(ctx: ModContext, mgr: ModerationManager,
                    user_id: str, reason: str = "No reason provided"):
    if not ctx.author.guild_permissions.ban_members:
        return await ctx.error("❌ You don't have permission to unban members.")
    try:
        user = await ctx.bot.fetch_user(int(user_id))
        await ctx.guild.unban(user, reason=f"{reason} - By {ctx.author}")
        
        # Report resolution logic
        if hasattr(ctx.bot, 'mod_oversight'):
            ctx.bot.mod_oversight.resolve_pending_action(user.id, 'ban')

        embed = discord.Embed(title="✅ User Unbanned",
                              description=f"{user.mention} has been unbanned.",
                              color=0x2ecc71, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unbanned {user}")
    except ValueError:
        await ctx.error("❌ Invalid user ID.")
    except discord.NotFound:
        await ctx.error("❌ User not found or not banned.")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to unban.")
        ctx.bot.logger.error(MODULE_NAME, "Unban failed", e)

async def _do_kick(ctx: ModContext, mgr: ModerationManager,
                   member: discord.Member, reason: str):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason)
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
            dm = discord.Embed(title="👢 You have been kicked",
                               description=f"You have been kicked from **{ctx.guild.name}**",
                               color=0xe67e22, timestamp=datetime.utcnow())
            dm.add_field(name="Reason", value=reason, inline=False)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            dm.set_footer(text="You can rejoin if you have an invite link")
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

        await member.kick(reason=f"{reason} - By {ctx.author}")

        embed = discord.Embed(title="✅ Member Kicked",
                              description=f"{member.mention} has been kicked.",
                              color=0xe67e22, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_kick(ctx.guild, member, ctx.author, reason, ctx.channel)
        
        await log_to_oversight(ctx, 'kick', member.id, str(member), reason, inchat_msg_id, botlog_msg_id)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} kicked {member}")

    except Exception as e:
        await ctx.error("❌ An error occurred while trying to kick the member.")
        ctx.bot.logger.error(MODULE_NAME, "Kick failed", e)

async def _do_timeout(ctx: ModContext, mgr: ModerationManager,
                      member: discord.Member, duration: int, reason: str):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    if not (1 <= duration <= 40320):
        return await ctx.error("❌ Duration must be between 1 and 40320 minutes.")
    try:
        await member.timeout(datetime.utcnow() + timedelta(minutes=duration),
                             reason=f"{reason} - By {ctx.author}")
        embed = discord.Embed(title="✅ Member Timed Out",
                              description=f"{member.mention} timed out for **{duration}** minutes.",
                              color=0xe74c3c, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Duration", value=f"{duration} minutes", inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_timeout(ctx.guild, member, ctx.author, reason,
                                 f"{duration} minutes", ctx.channel)
        
        await log_to_oversight(ctx, 'timeout', member.id, str(member), reason, inchat_msg_id, botlog_msg_id,
                               duration=f"{duration} minutes")
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} timed out {member} for {duration}m")

    except Exception as e:
        await ctx.error("❌ An error occurred while trying to timeout the member.")
        ctx.bot.logger.error(MODULE_NAME, "Timeout failed", e)

async def _do_untimeout(ctx: ModContext, mgr: ModerationManager, member: discord.Member):
    if not ctx.author.guild_permissions.moderate_members and not has_elevated_role(ctx.author):
        return await ctx.error("❌ You don't have permission to moderate members.")
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    try:
        await member.timeout(None, reason=f"Timeout removed by {ctx.author}")
        embed = discord.Embed(title="✅ Timeout Removed",
                              description=f"{member.mention}'s timeout has been removed.",
                              color=0x2ecc71, timestamp=datetime.utcnow())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} removed timeout from {member}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to remove the timeout.")
        ctx.bot.logger.error(MODULE_NAME, "Untimeout failed", e)

async def _do_mute(ctx: ModContext, mgr: ModerationManager,
                   member: discord.Member, reason: str = "No reason provided",
                   duration: Optional[str] = None):
    if not ctx.author.guild_permissions.manage_roles and not has_elevated_role(ctx.author):
        return await ctx.error("❌ You don't have permission to mute members.")
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)

    duration_seconds, duration_str = parse_duration(duration or "")
    try:
        muted_role = discord.utils.get(ctx.guild.roles, name=MUTED_ROLE_NAME)
        if not muted_role:
            muted_role = await ctx.guild.create_role(
                name=MUTED_ROLE_NAME, color=discord.Color.dark_gray(),
                reason="Creating Muted role for moderation")
            for ch in ctx.guild.channels:
                try:
                    await ch.set_permissions(muted_role, send_messages=False, speak=False)
                except Exception:
                    pass

        await member.add_roles(muted_role, reason=reason)
        mgr.mute_manager.add_mute(ctx.guild.id, member.id, reason, ctx.author, duration_seconds)

        try:
            dm = discord.Embed(title="🔇 You Have Been Muted",
                               description=f"You have been muted in **{ctx.guild.name}**.",
                               color=0xf39c12, timestamp=datetime.utcnow())
            dm.add_field(name="Reason", value=reason, inline=False)
            dm.add_field(name="Duration", value=duration_str, inline=True)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

        embed = discord.Embed(title="✅ Member Muted",
                              description=f"{member.mention} has been muted.",
                              color=0xf39c12, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Duration", value=duration_str, inline=True)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_mute(ctx.guild, member, ctx.author, reason, duration_str, ctx.channel)
        
        await log_to_oversight(ctx, 'mute', member.id, str(member), reason, inchat_msg_id, botlog_msg_id,
                               duration=duration_str)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} muted {member} for {duration_str}")

    except discord.Forbidden:
        await ctx.error("❌ I don't have permission to mute this member.")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to mute the member.")
        ctx.bot.logger.error(MODULE_NAME, "Mute failed", e)

async def _do_unmute(ctx: ModContext, mgr: ModerationManager, member: discord.Member):
    if not ctx.author.guild_permissions.manage_roles and not has_elevated_role(ctx.author):
        return await ctx.error("❌ You don't have permission to manage roles.")
    muted_role = discord.utils.get(ctx.guild.roles, name=MUTED_ROLE_NAME)
    if not muted_role or muted_role not in member.roles:
        return await ctx.error("❌ This member is not muted.")
    try:
        await member.remove_roles(muted_role, reason=f"Unmuted by {ctx.author}")
        mgr.mute_manager.remove_mute(ctx.guild.id, member.id)
        embed = discord.Embed(title="✅ Member Unmuted",
                              description=f"{member.mention} has been unmuted.",
                              color=0x2ecc71, timestamp=datetime.utcnow())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unmuted {member}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to unmute the member.")
        ctx.bot.logger.error(MODULE_NAME, "Unmute failed", e)

async def _do_softban(ctx: ModContext, mgr: ModerationManager,
                      member: discord.Member, reason: str, delete_days: int = 7):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason)
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
        await member.ban(reason=f"Softban: {reason} - By {ctx.author}",
                         delete_message_days=delete_days)
        await ctx.guild.unban(member, reason=f"Softban unban - By {ctx.author}")

        embed = discord.Embed(
            title="✅ Member Softbanned",
            description=f"{member.mention} softbanned (messages deleted, can rejoin).",
            color=0x992d22, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Messages Deleted", value=f"{delete_days} days", inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_softban(ctx.guild, member, ctx.author, reason, delete_days, ctx.channel)
        
        await log_to_oversight(ctx, 'softban', member.id, str(member), reason, inchat_msg_id, botlog_msg_id,
                               additional={'delete_days': delete_days})
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} softbanned {member}")

    except Exception as e:
        await ctx.error("❌ An error occurred while trying to softban the member.")
        ctx.bot.logger.error(MODULE_NAME, "Softban failed", e)

async def _do_warn(ctx: ModContext, mgr: ModerationManager,
                   member: discord.Member, reason: str):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    try:
        strike_count = mgr.strike_system.add_strike(member.id, reason)
        try:
            dm = discord.Embed(title="⚠️ Warning",
                               description=f"You have been warned in **{ctx.guild.name}**",
                               color=0xf39c12, timestamp=datetime.utcnow())
            dm.add_field(name="Reason", value=reason, inline=False)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            dm.add_field(name="Total Warnings", value=str(strike_count), inline=True)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

        embed = discord.Embed(title="⚠️ Member Warned",
                              description=f"{member.mention} has been warned.",
                              color=0xf39c12, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Total Warnings", value=str(strike_count), inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_warn(ctx.guild, member, ctx.author, reason, strike_count, ctx.channel)
        
        await log_to_oversight(ctx, 'warn', member.id, str(member), reason, inchat_msg_id, botlog_msg_id,
                               additional={'strike_count': strike_count})
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} warned {member}")

    except Exception as e:
        await ctx.error("❌ An error occurred while trying to warn the member.")
        ctx.bot.logger.error(MODULE_NAME, "Warn failed", e)

async def _do_warnings(ctx: ModContext, mgr: ModerationManager, member: discord.Member):
    if not ctx.author.guild_permissions.manage_messages and not has_elevated_role(ctx.author):
        return await ctx.error("❌ You don't have permission to view warnings.")
    strikes = mgr.strike_system.get_strike_details(member.id)
    if not strikes:
        return await ctx.reply(f"✅ **{member}** has no warnings.", ephemeral=True)
    embed = discord.Embed(title=f"⚠️ Warnings for {member}",
                          description=f"Total warnings: **{len(strikes)}**",
                          color=0xf39c12, timestamp=datetime.utcnow())
    for i, s in enumerate(strikes[-10:], 1):
        ts = datetime.fromisoformat(s['timestamp']).strftime("%Y-%m-%d %H:%M UTC")
        embed.add_field(name=f"Warning {i}",
                        value=f"**Reason:** {s['reason']}\n**Date:** {ts}", inline=False)
    if len(strikes) > 10:
        embed.set_footer(text=f"Showing last 10 of {len(strikes)} warnings")
    await ctx.reply(embed=embed, ephemeral=True)

async def _do_clearwarnings(ctx: ModContext, mgr: ModerationManager, member: discord.Member):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.error("❌ You need Administrator permission to clear warnings.")
    if mgr.strike_system.clear_strikes(member.id):
        embed = discord.Embed(title="✅ Warnings Cleared",
                              description=f"All warnings cleared for **{member}**.",
                              color=0x2ecc71, timestamp=datetime.utcnow())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} cleared warnings for {member}")
    else:
        await ctx.reply(f"**{member}** has no warnings to clear.", ephemeral=True)

async def _do_purge(ctx: ModContext, mgr: ModerationManager,
                    amount: int, target: Optional[discord.Member] = None):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    if not (1 <= amount <= 100):
        return await ctx.error("❌ Amount must be between 1 and 100.")

    if not isinstance(ctx._source, discord.Interaction):
        try:
            await ctx._source.message.delete()
        except Exception:
            pass

    await ctx.defer()
    try:
        check = (lambda m: m.author.id == target.id) if target else (lambda m: True)
        deleted = await ctx.channel.purge(limit=amount, check=check)

        reason = f"Purged {len(deleted)} message(s)" + (f" from {target}" if target else "")
        embed = discord.Embed(
            title="✅ Messages Purged",
            description=f"Deleted **{len(deleted)}** messages"
                        f"{f' from {target.mention}' if target else ''}.",
            color=0x2ecc71, timestamp=datetime.utcnow())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Channel", value=ctx.channel.mention, inline=True)
        inchat_msg_id = await ctx.followup(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_purge(ctx.guild, ctx.author, len(deleted), ctx.channel, target)
        
        await log_to_oversight(ctx, 'purge', target.id if target else None, 
                               str(target) if target else None, reason, inchat_msg_id, botlog_msg_id,
                               additional={'amount': len(deleted)})
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} purged {len(deleted)} messages")

    except Exception as e:
        await ctx.followup("❌ An error occurred while trying to purge messages.", ephemeral=True)
        ctx.bot.logger.error(MODULE_NAME, "Purge failed", e)

async def _do_slowmode(ctx: ModContext, mgr: ModerationManager,
                       seconds: int, channel: Optional[discord.TextChannel] = None):
    if not ctx.author.guild_permissions.manage_channels:
        return await ctx.error("❌ You don't have permission to manage channels.")
    if not (0 <= seconds <= 21600):
        return await ctx.error("❌ Slowmode must be between 0 and 21600 seconds.")
    target = channel or ctx.channel
    try:
        await target.edit(slowmode_delay=seconds, reason=f"Slowmode set by {ctx.author}")
        if seconds == 0:
            embed = discord.Embed(title="✅ Slowmode Disabled",
                                  description=f"Slowmode disabled in {target.mention}.",
                                  color=0x2ecc71)
        else:
            embed = discord.Embed(title="✅ Slowmode Enabled",
                                  description=f"Slowmode set to **{seconds}s** in {target.mention}.",
                                  color=0x3498db)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} set slowmode to {seconds}s in {target.name}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to set slowmode.")
        ctx.bot.logger.error(MODULE_NAME, "Slowmode failed", e)

async def _do_lock(ctx: ModContext, mgr: ModerationManager,
                   reason: str, channel: Optional[discord.TextChannel] = None):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason)
    if not ok:
        return await ctx.error(err)
    target = channel or ctx.channel
    try:
        await target.set_permissions(ctx.guild.default_role, send_messages=False,
                                     reason=f"{reason} - By {ctx.author}")
        embed = discord.Embed(title="🔒 Channel Locked",
                              description=f"{target.mention} has been locked.",
                              color=0xe74c3c, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_lock(ctx.guild, ctx.author, reason, target)
        
        await log_to_oversight(ctx, 'lock', None, None, reason, inchat_msg_id, botlog_msg_id,
                               additional={'channel': target.id})
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} locked {target.name}")

    except Exception as e:
        await ctx.error("❌ An error occurred while trying to lock the channel.")
        ctx.bot.logger.error(MODULE_NAME, "Lock failed", e)

async def _do_unlock(ctx: ModContext, mgr: ModerationManager,
                     channel: Optional[discord.TextChannel] = None):
    if not ctx.author.guild_permissions.manage_channels:
        return await ctx.error("❌ You don't have permission to manage channels.")
    target = channel or ctx.channel
    try:
        await target.set_permissions(ctx.guild.default_role, send_messages=None,
                                     reason=f"Unlocked by {ctx.author}")
        embed = discord.Embed(title="🔓 Channel Unlocked",
                              description=f"{target.mention} has been unlocked.",
                              color=0x2ecc71, timestamp=datetime.utcnow())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unlocked {target.name}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to unlock the channel.")
        ctx.bot.logger.error(MODULE_NAME, "Unlock failed", e)

# ==================== SETUP ====================

def setup(bot):

    moderation_manager = ModerationManager(bot)
    bot.moderation_manager = moderation_manager
    mgr = moderation_manager

    # ---- SLASH COMMANDS (/command) ----

    @bot.tree.command(name="ban", description="Ban a user from the server")
    @app_commands.describe(user="User to ban", reason="Reason (min 10 chars)",
                           delete_days="Days of messages to delete (0-7, default 0)")
    @app_commands.default_permissions(ban_members=True)
    async def slash_ban(interaction: discord.Interaction, user: discord.User,
                        reason: str, delete_days: Optional[int] = 0):
        await _do_ban(ModContext(interaction), mgr, user, reason, delete_days)

    @bot.tree.command(name="unban", description="Unban a user from the server")
    @app_commands.describe(user_id="User ID to unban", reason="Reason for unban")
    @app_commands.default_permissions(ban_members=True)
    async def slash_unban(interaction: discord.Interaction, user_id: str,
                          reason: Optional[str] = "No reason provided"):
        await _do_unban(ModContext(interaction), mgr, user_id, reason)

    @bot.tree.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Reason (min 10 chars)")
    @app_commands.default_permissions(kick_members=True)
    async def slash_kick(interaction: discord.Interaction, member: discord.Member, reason: str):
        await _do_kick(ModContext(interaction), mgr, member, reason)

    @bot.tree.command(name="timeout", description="Timeout a member")
    @app_commands.describe(member="Member to timeout", duration="Duration in minutes",
                           reason="Reason (min 10 chars)")
    @app_commands.default_permissions(moderate_members=True)
    async def slash_timeout(interaction: discord.Interaction, member: discord.Member,
                            duration: int, reason: str):
        await _do_timeout(ModContext(interaction), mgr, member, duration, reason)

    @bot.tree.command(name="untimeout", description="Remove timeout from a member")
    @app_commands.describe(member="Member to remove timeout from")
    @app_commands.default_permissions(moderate_members=True)
    async def slash_untimeout(interaction: discord.Interaction, member: discord.Member):
        await _do_untimeout(ModContext(interaction), mgr, member)

    @bot.tree.command(name="mute", description="Mute a member")
    @app_commands.describe(member="Member to mute", reason="Reason for mute",
                           duration="Duration e.g. 10m, 1h, 1d (empty = permanent)")
    @app_commands.default_permissions(manage_roles=True)
    async def slash_mute(interaction: discord.Interaction, member: discord.Member,
                         reason: str = "No reason provided", duration: Optional[str] = None):
        await _do_mute(ModContext(interaction), mgr, member, reason, duration)

    @bot.tree.command(name="unmute", description="Unmute a member")
    @app_commands.describe(member="Member to unmute")
    @app_commands.default_permissions(manage_roles=True)
    async def slash_unmute(interaction: discord.Interaction, member: discord.Member):
        await _do_unmute(ModContext(interaction), mgr, member)

    @bot.tree.command(name="softban", description="Softban a member (ban+unban to delete messages)")
    @app_commands.describe(member="Member to softban", reason="Reason (min 10 chars)",
                           delete_days="Days of messages to delete (0-7, default 7)")
    @app_commands.default_permissions(ban_members=True)
    async def slash_softban(interaction: discord.Interaction, member: discord.Member,
                            reason: str, delete_days: Optional[int] = 7):
        await _do_softban(ModContext(interaction), mgr, member, reason, delete_days)

    @bot.tree.command(name="warn", description="Warn a member")
    @app_commands.describe(member="Member to warn", reason="Reason (min 10 chars)")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_warn(interaction: discord.Interaction, member: discord.Member, reason: str):
        await _do_warn(ModContext(interaction), mgr, member, reason)

    @bot.tree.command(name="warnings", description="View warnings for a member")
    @app_commands.describe(member="Member to check")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_warnings(interaction: discord.Interaction, member: discord.Member):
        await _do_warnings(ModContext(interaction), mgr, member)

    @bot.tree.command(name="clearwarnings", description="Clear all warnings for a member")
    @app_commands.describe(member="Member to clear warnings for")
    @app_commands.default_permissions(administrator=True)
    async def slash_clearwarnings(interaction: discord.Interaction, member: discord.Member):
        await _do_clearwarnings(ModContext(interaction), mgr, member)

    @bot.tree.command(name="purge", description="Delete multiple messages")
    @app_commands.describe(amount="Number of messages to delete (1-100)",
                           user="Only delete messages from this user (optional)")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_purge(interaction: discord.Interaction, amount: int,
                          user: Optional[discord.Member] = None):
        await _do_purge(ModContext(interaction), mgr, amount, user)

    @bot.tree.command(name="slowmode", description="Set channel slowmode")
    @app_commands.describe(seconds="Slowmode delay in seconds (0 to disable)",
                           channel="Channel to apply to (default: current)")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_slowmode(interaction: discord.Interaction, seconds: int,
                             channel: Optional[discord.TextChannel] = None):
        await _do_slowmode(ModContext(interaction), mgr, seconds, channel)

    @bot.tree.command(name="lock", description="Lock a channel")
    @app_commands.describe(reason="Reason for locking (min 10 chars)",
                           channel="Channel to lock (default: current)")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_lock(interaction: discord.Interaction, reason: str,
                         channel: Optional[discord.TextChannel] = None):
        await _do_lock(ModContext(interaction), mgr, reason, channel)

    @bot.tree.command(name="unlock", description="Unlock a channel")
    @app_commands.describe(channel="Channel to unlock (default: current)")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_unlock(interaction: discord.Interaction,
                           channel: Optional[discord.TextChannel] = None):
        await _do_unlock(ModContext(interaction), mgr, channel)

    # ---- PREFIX COMMANDS (?command) ----

    @bot.command(name="ban")
    async def prefix_ban(ctx, user: discord.User = None, *, args: str = ""):
        """?ban @user <reason> [delete_days]"""
        if not user:
            return await ctx.send("❌ Usage: `?ban @user <reason> [delete_days]`", delete_after=8)
        delete_days = 0
        reason = args
        parts = args.rsplit(None, 1)
        if len(parts) == 2 and parts[-1].isdigit() and int(parts[-1]) <= 7:
            delete_days = int(parts[-1])
            reason = parts[0]
        await _do_ban(ModContext(ctx), mgr, user, reason, delete_days)

    @bot.command(name="unban")
    async def prefix_unban(ctx, user_id: str = None, *, reason: str = "No reason provided"):
        """?unban <user_id> [reason]"""
        if not user_id:
            return await ctx.send("❌ Usage: `?unban <user_id> [reason]`", delete_after=8)
        await _do_unban(ModContext(ctx), mgr, user_id, reason)

    @bot.command(name="kick")
    async def prefix_kick(ctx, member: discord.Member = None, *, reason: str = ""):
        """?kick @member <reason>"""
        if not member:
            return await ctx.send("❌ Usage: `?kick @member <reason>`", delete_after=8)
        await _do_kick(ModContext(ctx), mgr, member, reason)

    @bot.command(name="timeout")
    async def prefix_timeout(ctx, member: discord.Member = None,
                              duration: int = None, *, reason: str = ""):
        """?timeout @member <minutes> <reason>"""
        if not member or duration is None:
            return await ctx.send("❌ Usage: `?timeout @member <minutes> <reason>`", delete_after=8)
        await _do_timeout(ModContext(ctx), mgr, member, duration, reason)

    @bot.command(name="untimeout")
    async def prefix_untimeout(ctx, member: discord.Member = None):
        """?untimeout @member"""
        if not member:
            return await ctx.send("❌ Usage: `?untimeout @member`", delete_after=8)
        await _do_untimeout(ModContext(ctx), mgr, member)

    @bot.command(name="mute")
    async def prefix_mute(ctx, member: discord.Member = None, *, args: str = ""):
        """?mute @member [duration] [reason]  — duration e.g. 10m, 1h, 1d"""
        if not member:
            return await ctx.send("❌ Usage: `?mute @member [duration] [reason]`", delete_after=8)
        duration = None
        reason = args or "No reason provided"
        parts = args.split(None, 1)
        if parts and re.match(r'^\d+[smhd]$', parts[0].lower()):
            duration = parts[0]
            reason = parts[1] if len(parts) > 1 else "No reason provided"
        await _do_mute(ModContext(ctx), mgr, member, reason, duration)

    @bot.command(name="unmute")
    async def prefix_unmute(ctx, member: discord.Member = None):
        """?unmute @member"""
        if not member:
            return await ctx.send("❌ Usage: `?unmute @member`", delete_after=8)
        await _do_unmute(ModContext(ctx), mgr, member)

    @bot.command(name="softban")
    async def prefix_softban(ctx, member: discord.Member = None, *, reason: str = ""):
        """?softban @member <reason>"""
        if not member:
            return await ctx.send("❌ Usage: `?softban @member <reason>`", delete_after=8)
        await _do_softban(ModContext(ctx), mgr, member, reason)

    @bot.command(name="warn")
    async def prefix_warn(ctx, member: discord.Member = None, *, reason: str = ""):
        """?warn @member <reason>"""
        if not member:
            return await ctx.send("❌ Usage: `?warn @member <reason>`", delete_after=8)
        await _do_warn(ModContext(ctx), mgr, member, reason)

    @bot.command(name="warnings")
    async def prefix_warnings(ctx, member: discord.Member = None):
        """?warnings @member"""
        if not member:
            return await ctx.send("❌ Usage: `?warnings @member`", delete_after=8)
        await _do_warnings(ModContext(ctx), mgr, member)

    @bot.command(name="clearwarnings")
    async def prefix_clearwarnings(ctx, member: discord.Member = None):
        """?clearwarnings @member"""
        if not member:
            return await ctx.send("❌ Usage: `?clearwarnings @member`", delete_after=8)
        await _do_clearwarnings(ModContext(ctx), mgr, member)

    @bot.command(name="purge")
    async def prefix_purge(ctx, amount: int = None, member: discord.Member = None):
        """?purge <amount> [@member]"""
        if amount is None:
            return await ctx.send("❌ Usage: `?purge <amount> [@member]`", delete_after=8)
        await _do_purge(ModContext(ctx), mgr, amount, member)

    @bot.command(name="slowmode")
    async def prefix_slowmode(ctx, seconds: int = None,
                               channel: discord.TextChannel = None):
        """?slowmode <seconds> [#channel]"""
        if seconds is None:
            return await ctx.send("❌ Usage: `?slowmode <seconds> [#channel]`", delete_after=8)
        await _do_slowmode(ModContext(ctx), mgr, seconds, channel)

    @bot.command(name="lock")
    async def prefix_lock(ctx, channel: Optional[discord.TextChannel] = None, *, reason: str = ""):
        """?lock [#channel] <reason>"""
        await _do_lock(ModContext(ctx), mgr, reason, channel)

    @bot.command(name="unlock")
    async def prefix_unlock(ctx, channel: discord.TextChannel = None):
        """?unlock [#channel]"""
        await _do_unlock(ModContext(ctx), mgr, channel)

    # ==================== AUTO-MOD ====================

    @bot.listen()
    async def on_message(message):
        if message.author.bot or not message.guild:
            return

        content_lower = message.content.lower()

        # Child Safety
        for word in CHILD_SAFETY:
            if word.lower() in content_lower:
                try:
                    await message.delete()
                    await message.guild.ban(message.author,
                                            reason=f"Auto-ban: Child safety violation - '{word}'")
                    bot.logger.log(MODULE_NAME, f"AUTO-BAN: {message.author} child safety", "WARNING")
                    el = get_event_logger(bot)
                    if el:
                        await el.log_autoban(message.guild, message.author,
                                             "Child safety violation", message.channel)
                except Exception as e:
                    bot.logger.error(MODULE_NAME, "Auto-ban failed", e)
                return

        # Racial Slurs
        for word in RACIAL_SLURS:
            if word.lower() in content_lower:
                try:
                    await message.delete()
                    count = moderation_manager.strike_system.add_strike(
                        message.author.id, f"Racial slur: '{word}'")
                    if count >= 2:
                        await message.guild.ban(
                            message.author, reason="Auto-ban: Repeated racial slurs (2 strikes)")
                        bot.logger.log(MODULE_NAME,
                                       f"AUTO-BAN: {message.author} repeated slurs", "WARNING")
                        el = get_event_logger(bot)
                        if el:
                            await el.log_autoban_strike(
                                message.guild, message.author, count,
                                "Repeated racial slurs", message.channel)
                    else:
                        try:
                            dm = discord.Embed(
                                title="⚠️ Warning - Strike 1/2",
                                description="Your message was deleted for inappropriate language.",
                                color=0xf39c12)
                            dm.add_field(name="Action",
                                         value="Second strike = automatic ban.", inline=False)
                            await message.author.send(embed=dm)
                        except Exception:
                            pass
                        bot.logger.log(MODULE_NAME, f"STRIKE 1: {message.author} slur", "WARNING")
                except Exception as e:
                    bot.logger.error(MODULE_NAME, "Auto-mod slur handling failed", e)
                return

        # Banned Words
        for word in BANNED_WORDS:
            if word.lower() in content_lower:
                try:
                    await message.delete()
                    bot.logger.log(MODULE_NAME, f"Deleted banned word from {message.author}")
                except Exception as e:
                    bot.logger.error(MODULE_NAME, "Message deletion failed", e)
                return

    # ==================== MEMBER EVENTS ====================

    @bot.listen()
    async def on_member_remove(member):
        moderation_manager.role_persistence.save_member_roles(member)

    @bot.listen()
    async def on_member_join(member):
        await moderation_manager.role_persistence.restore_member_roles(member)

    # ==================== MUTE EXPIRY TASK ====================

    @tasks.loop(minutes=1)
    async def check_expired_mutes():
        try:
            for mute in moderation_manager.mute_manager.get_expired_mutes():
                guild = bot.get_guild(mute['guild_id'])
                if not guild:
                    continue
                member = guild.get_member(mute['user_id'])
                if not member:
                    moderation_manager.mute_manager.remove_mute(
                        mute['guild_id'], mute['user_id'])
                    continue
                muted_role = discord.utils.get(guild.roles, name=MUTED_ROLE_NAME)
                if muted_role and muted_role in member.roles:
                    try:
                        await member.remove_roles(muted_role, reason="Mute duration expired")
                        bot.logger.log(MODULE_NAME, f"Auto-unmuted {member}")
                    except Exception as e:
                        bot.logger.error(MODULE_NAME, f"Failed to auto-unmute {member}", e)
                moderation_manager.mute_manager.remove_mute(mute['guild_id'], mute['user_id'])
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Error in mute expiry checker", e)

    @check_expired_mutes.before_loop
    async def before_check_expired_mutes():
        await bot.wait_until_ready()

    check_expired_mutes.start()
    bot.logger.log(MODULE_NAME, "Started background mute expiry checker")
    bot.logger.log(MODULE_NAME, "Moderation setup complete")
}