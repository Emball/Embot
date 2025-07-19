# commands.py
import discord
import logging
from discord import app_commands
from datetime import timedelta
from typing import Optional
from config import BOT_LOGS_CHANNEL_NAME

logger = logging.getLogger('commands')

async def send_mod_log(bot, action: str, moderator: discord.Member, target: discord.Member, reason: str, duration: str = None):
    """Send moderation action to log channel"""
    try:
        channel = discord.utils.get(bot.guild.text_channels, name=BOT_LOGS_CHANNEL_NAME)
        if not channel:
            logger.warning("Mod log channel not found")
            return

        embed = discord.Embed(
            title=f"Moderation Action: {action}",
            color=0xFF0000,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Moderator", value=moderator.mention, inline=True)
        embed.add_field(name="Target", value=target.mention, inline=True)
        if duration:
            embed.add_field(name="Duration", value=duration, inline=True)
        embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
        embed.set_footer(text=f"ID: {target.id}")

        await channel.send(embed=embed)
        logger.info(f"Logged {action} for {target} by {moderator}")
    except Exception as e:
        logger.error(f"Failed to send mod log: {e}")

def setup(bot):  # Changed from setup_commands
    """Setup moderation commands"""
    logger.info("Initializing moderation commands")

    @bot.tree.command(name="ban", description="Ban a user from the server")
    @app_commands.describe(
        user="User to ban",
        reason="Reason for ban",
        delete_message_days="Number of days of messages to delete (0-7)"
    )
    async def ban(
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
        delete_message_days: Optional[int] = 0
    ):
        """Ban command with message cleanup"""
        if not interaction.user.guild_permissions.ban_members:
            logger.warning(f"Unauthorized ban attempt by {interaction.user}")
            return await interaction.response.send_message(
                "❌ You don't have permission to ban members.",
                ephemeral=True
            )

        try:
            # Validate delete days
            delete_days = max(0, min(7, delete_message_days or 0))
            await user.ban(
                reason=f"{interaction.user}: {reason}",
                delete_message_days=delete_days
            )
            
            await interaction.response.send_message(
                f"✅ Banned {user.mention}. Deleted {delete_days} days of messages.",
                ephemeral=True
            )
            logger.info(f"{user} banned by {interaction.user}. Deleted {delete_days} days.")
            
            await send_mod_log(
                bot,
                "Ban",
                interaction.user,
                user,
                reason,
                f"{delete_days} days messages deleted"
            )
        except Exception as e:
            logger.error(f"Ban failed for {user}: {e}")
            await interaction.response.send_message(
                f"❌ Failed to ban user: {e}",
                ephemeral=True
            )

    @bot.tree.command(name="kick", description="Kick a user from the server")
    @app_commands.describe(
        user="User to kick",
        reason="Reason for kick"
    )
    async def kick(
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided"
    ):
        """Kick command with logging"""
        if not interaction.user.guild_permissions.kick_members:
            logger.warning(f"Unauthorized kick attempt by {interaction.user}")
            return await interaction.response.send_message(
                "❌ You don't have permission to kick members.",
                ephemeral=True
            )

        try:
            await user.kick(reason=f"{interaction.user}: {reason}")
            await interaction.response.send_message(
                f"✅ Kicked {user.mention}.",
                ephemeral=True
            )
            logger.info(f"{user} kicked by {interaction.user}")
            
            await send_mod_log(
                bot,
                "Kick",
                interaction.user,
                user,
                reason
            )
        except Exception as e:
            logger.error(f"Kick failed for {user}: {e}")
            await interaction.response.send_message(
                f"❌ Failed to kick user: {e}",
                ephemeral=True
            )

    @bot.tree.command(name="timeout", description="Timeout a user")
    @app_commands.describe(
        user="User to timeout",
        duration="Duration in minutes",
        reason="Reason for timeout"
    )
    async def timeout(
        interaction: discord.Interaction,
        user: discord.Member,
        duration: app_commands.Range[int, 1, 40320],  # 1 minute to 28 days
        reason: Optional[str] = "No reason provided"
    ):
        """Timeout command with duration validation"""
        if not interaction.user.guild_permissions.moderate_members:
            logger.warning(f"Unauthorized timeout attempt by {interaction.user}")
            return await interaction.response.send_message(
                "❌ You don't have permission to timeout members.",
                ephemeral=True
            )

        try:
            timeout_duration = timedelta(minutes=duration)
            until = discord.utils.utcnow() + timeout_duration
            
            await user.timeout(
                until,
                reason=f"{interaction.user}: {reason}"
            )
            
            duration_str = str(timeout_duration).split(".")[0]  # Remove microseconds
            await interaction.response.send_message(
                f"✅ Timed out {user.mention} for {duration_str}.",
                ephemeral=True
            )
            logger.info(f"{user} timed out for {duration_str} by {interaction.user}")
            
            await send_mod_log(
                bot,
                "Timeout",
                interaction.user,
                user,
                reason,
                duration_str
            )
        except Exception as e:
            logger.error(f"Timeout failed for {user}: {e}")
            await interaction.response.send_message(
                f"❌ Failed to timeout user: {e}",
                ephemeral=True
            )

    @bot.tree.command(name="unban", description="Unban a user")
    @app_commands.describe(
        user_id="User ID to unban",
        reason="Reason for unban"
    )
    async def unban(
        interaction: discord.Interaction,
        user_id: str,
        reason: Optional[str] = "No reason provided"
    ):
        """Unban command with user ID input"""
        if not interaction.user.guild_permissions.ban_members:
            logger.warning(f"Unauthorized unban attempt by {interaction.user}")
            return await interaction.response.send_message(
                "❌ You don't have permission to unban members.",
                ephemeral=True
            )

        try:
            user = await bot.fetch_user(int(user_id))
            await interaction.guild.unban(
                user,
                reason=f"{interaction.user}: {reason}"
            )
            
            await interaction.response.send_message(
                f"✅ Unbanned {user.mention}.",
                ephemeral=True
            )
            logger.info(f"{user} unbanned by {interaction.user}")
            
            await send_mod_log(
                bot,
                "Unban",
                interaction.user,
                user,
                reason
            )
        except ValueError:
            logger.warning(f"Invalid user ID provided: {user_id}")
            await interaction.response.send_message(
                "❌ Please provide a valid user ID.",
                ephemeral=True
            )
        except discord.NotFound:
            logger.warning(f"User {user_id} not found in bans")
            await interaction.response.send_message(
                "❌ That user isn't banned or doesn't exist.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Unban failed for {user_id}: {e}")
            await interaction.response.send_message(
                f"❌ Failed to unban user: {e}",
                ephemeral=True
            )

    @bot.tree.command(name="purge", description="Delete multiple messages")
    @app_commands.describe(
        amount="Number of messages to delete (1-100)",
        user="Only delete messages from this user"
    )
    async def purge(
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
        user: Optional[discord.Member] = None
    ):
        """Bulk message deletion with filters"""
        if not interaction.user.guild_permissions.manage_messages:
            logger.warning(f"Unauthorized purge attempt by {interaction.user}")
            return await interaction.response.send_message(
                "❌ You don't have permission to manage messages.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        
        def check(m):
            return user is None or m.author == user

        try:
            deleted = await interaction.channel.purge(
                limit=amount,
                check=check,
                before=interaction.created_at
            )
            
            await interaction.followup.send(
                f"✅ Deleted {len(deleted)} messages{f' from {user.mention}' if user else ''}.",
                ephemeral=True
            )
            logger.info(f"Purged {len(deleted)} messages in {interaction.channel}")
            
            await send_mod_log(
                bot,
                "Purge",
                interaction.user,
                interaction.user,  # Target is same as moderator for purge
                f"Deleted {len(deleted)} messages in #{interaction.channel.name}",
                f"User filter: {user.name if user else 'None'}"
            )
        except Exception as e:
            logger.error(f"Purge failed in {interaction.channel}: {e}")
            await interaction.followup.send(
                f"❌ Failed to delete messages: {e}",
                ephemeral=True
            )

    logger.info("Moderation commands registered")