# commands.py
import discord
from discord import app_commands
from datetime import timedelta

def setup_commands(bot):
    @bot.tree.command(name="ban")
    @app_commands.describe(user="User to ban", reason="Reason for ban")
    async def ban(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
        if not interaction.user.guild_permissions.ban_members:
            return await interaction.response.send_message("Missing permissions.", ephemeral=True)
        try:
            await user.ban(reason=reason)
            await interaction.response.send_message(f"{user.mention} banned: {reason}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Ban failed: {e}", ephemeral=True)

    @bot.tree.command(name="kick")
    @app_commands.describe(user="User to kick", reason="Reason for kick")
    async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
        if not interaction.user.guild_permissions.kick_members:
            return await interaction.response.send_message("Missing permissions.", ephemeral=True)
        try:
            await user.kick(reason=reason)
            await interaction.response.send_message(f"{user.mention} kicked: {reason}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Kick failed: {e}", ephemeral=True)

    @bot.tree.command(name="timeout")
    @app_commands.describe(user="User to timeout", duration="Timeout duration in seconds", reason="Reason for timeout")
    async def timeout(interaction: discord.Interaction, user: discord.Member, duration: int, reason: str = "No reason provided"):
        if not interaction.user.guild_permissions.moderate_members:
            return await interaction.response.send_message("Missing permissions.", ephemeral=True)
        try:
            await user.timeout(discord.utils.utcnow() + timedelta(seconds=duration), reason=reason)
            await interaction.response.send_message(f"{user.mention} timed out for {duration} seconds.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Timeout failed: {e}", ephemeral=True)