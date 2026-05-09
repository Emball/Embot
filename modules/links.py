import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional
import json
import os
from pathlib import Path

MODULE_NAME = "LINKS"

from _utils import script_dir

class LinkManager:
    """Manages custom link commands that can be easily configured"""
    
    def __init__(self, bot):
        self.bot = bot
        self.config_file = str(script_dir() / "config"/ "links_config.json")
        self.links = self.load_links()
        self.prefix = "?" # Default prefix for link commands
    
    def load_links(self):
        """Load links config, auto-creating defaults if missing."""
        try:
            with open(self.config_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            defaults = {}
            Path(self.config_file).parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(defaults, f, indent=2)
            return defaults
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load links config", e)
            return {}

    def save_links(self, links=None):
        """Save links to configuration file atomically"""
        if links is None:
            links = self.links
        try:
            from _utils import atomic_json_write
            atomic_json_write(self.config_file, links)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save links config", e)
    
    async def handle_link_command(self, message):
        """Handle link commands in messages"""
        # Check if message starts with prefix
        if not message.content.startswith(self.prefix):
            return False
        
        # Extract command name (remove prefix)
        parts = message.content[len(self.prefix):].split()
        if not parts:
            return False
        command = parts[0].lower()
        
        # Let the bot framework handle real registered commands (ban, kick, etc.)
        if self.bot.get_command(command):
            return False
        
        # Check if it's a valid link command
        if command not in self.links:
            return False
        
        link_data = self.links[command]
        
        # Check if enabled
        if not link_data.get("enabled", True):
            return False
        
        # Get URL
        url = link_data.get("url", "")
        
        if not url:
            await message.channel.send(f"The `{command}` link is not configured yet. Please set it up using `/linkset {command} <url>`")
            return True
        
        # Send the link as plain text
        await message.channel.send(url)
        self.bot.logger.log(MODULE_NAME, f"{message.author} used ?{command}")
        
        return True

def setup(bot):
    """Setup function called by main.py"""
    
    link_manager = LinkManager(bot)
    
    # Message listener for link commands
    @bot.listen()
    async def on_message(message):
        """Listen for link commands"""
        if message.author.bot:
            return
        
        # Handle link commands - bot framework handles all other ? prefixed commands natively
        await link_manager.handle_link_command(message)
    
    # Slash commands for managing links
    @bot.tree.command(name="linkset", description="[Owner only] Set or update a quick-link command")
    @app_commands.describe(
        name="Name of the link command (without ?)",
        url="URL to link to",
        description="Optional description for the link"
    )
    async def link_set(
        interaction: discord.Interaction,
        name: str,
        url: str,
        description: Optional[str] = None
    ):
        from moderation import is_owner
        if not is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to owners.", ephemeral=True)
            return
        """Set or update a link command"""
        # Sanitize name (remove ? if present)
        name = name.lower().replace("?", "")
        
        # Validate URL (basic check)
        if not url.startswith(("http://", "https://")):
            await interaction.response.send_message(
                "Invalid URL. Please provide a valid URL starting with http:// or https://",
                ephemeral=True
            )
            return
        
        # Update or create link
        is_new = name not in link_manager.links
        
        link_manager.links[name] = {
            "url": url,
            "description": description or f"{name} link",
            "enabled": True
        }
        
        link_manager.save_links()
        
        embed = discord.Embed(
            title=f"Link {'Created' if is_new else 'Updated'}",
            description=f"Link command `?{name}` has been {'created' if is_new else 'updated'}",
            color=0x2ecc71
        )
        
        embed.add_field(name="Command", value=f"`?{name}`", inline=True)
        embed.add_field(name="URL", value=url, inline=False)
        if description:
            embed.add_field(name="Description", value=description, inline=False)
        
        await interaction.response.send_message(embed=embed)
        bot.logger.log(MODULE_NAME, f"{interaction.user} {'created' if is_new else 'updated'} link: ?{name}")
    
    @bot.tree.command(name="linkremove", description="[Owner only] Remove a quick-link command")
    @app_commands.describe(name="Name of the link command to remove (without ?)")
    async def link_remove(interaction: discord.Interaction, name: str):
        from moderation import is_owner
        if not is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to owners.", ephemeral=True)
            return
        """Remove a link command"""
        # Sanitize name
        name = name.lower().replace("?", "")
        
        if name not in link_manager.links:
            await interaction.response.send_message(
                f"Link command `?{name}` does not exist.",
                ephemeral=True
            )
            return
        
        del link_manager.links[name]
        link_manager.save_links()
        
        embed = discord.Embed(
            title="Link Removed",
            description=f"Link command `?{name}` has been removed",
            color=0xe74c3c
        )
        
        await interaction.response.send_message(embed=embed)
        bot.logger.log(MODULE_NAME, f"{interaction.user} removed link: ?{name}")
    
    @bot.tree.command(name="linktoggle", description="[Owner only] Enable or disable a quick-link command")
    @app_commands.describe(name="Name of the link command to toggle (without ?)")
    async def link_toggle(interaction: discord.Interaction, name: str):
        from moderation import is_owner
        if not is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to owners.", ephemeral=True)
            return
        """Toggle a link command on/off"""
        # Sanitize name
        name = name.lower().replace("?", "")
        
        if name not in link_manager.links:
            await interaction.response.send_message(
                f"Link command `?{name}` does not exist.",
                ephemeral=True
            )
            return
        
        # Toggle enabled status
        current_status = link_manager.links[name].get("enabled", True)
        link_manager.links[name]["enabled"] = not current_status
        link_manager.save_links()
        
        new_status = link_manager.links[name]["enabled"]
        
        embed = discord.Embed(
            title=f"{' Link Enabled' if new_status else ' Link Disabled'}",
            description=f"Link command `?{name}` has been {'enabled' if new_status else 'disabled'}",
            color=0x2ecc71 if new_status else 0xe74c3c
        )
        
        await interaction.response.send_message(embed=embed)
        bot.logger.log(MODULE_NAME, f"{interaction.user} {'enabled' if new_status else 'disabled'} link: ?{name}")
    
    @bot.tree.command(name="linklist", description="List all available link commands")
    async def link_list(interaction: discord.Interaction):
        """List all link commands"""
        if not link_manager.links:
            await interaction.response.send_message("No link commands configured yet.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="Available Link Commands",
            description=f"Use `?<command>` to access these links (e.g., `?tracker`)",
            color=0x5865f2
        )
        
        # Separate enabled and disabled links
        enabled_links = []
        disabled_links = []
        
        for name, data in sorted(link_manager.links.items()):
            link_info = f"**?{name}**"
            if data.get("description"):
                link_info += f"- {data['description']}"
            
            if data.get("url"):
                link_info += f"\n└─ [Link]({data['url']})"
            else:
                link_info += "\n└─  Not configured"
            
            if data.get("enabled", True):
                enabled_links.append(link_info)
            else:
                disabled_links.append(link_info)
        
        if enabled_links:
            # Split into chunks if too long
            enabled_text = "\n\n".join(enabled_links)
            if len(enabled_text) > 1024:
                # Split into multiple fields
                chunk_size = 1024
                chunks = [enabled_text[i:i+chunk_size] for i in range(0, len(enabled_text), chunk_size)]
                for i, chunk in enumerate(chunks):
                    field_name = "Enabled Links"if i == 0 else f"Enabled Links (cont. {i+1})"
                    embed.add_field(name=field_name, value=chunk, inline=False)
            else:
                embed.add_field(name="Enabled Links", value=enabled_text, inline=False)
        
        if disabled_links:
            disabled_text = "\n\n".join(disabled_links)
            if len(disabled_text) > 1024:
                disabled_text = disabled_text[:1021] + "..."
            embed.add_field(name="Disabled Links", value=disabled_text, inline=False)
        
        embed.set_footer(text=f"Total: {len(link_manager.links)} link commands")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bot.tree.command(name="linkinfo", description="Get detailed info about a specific link command")
    @app_commands.describe(name="Name of the link command (without ?)")
    async def link_info(interaction: discord.Interaction, name: str):
        """Get info about a specific link"""
        # Sanitize name
        name = name.lower().replace("?", "")
        
        if name not in link_manager.links:
            await interaction.response.send_message(
                f"Link command `?{name}` does not exist.",
                ephemeral=True
            )
            return
        
        data = link_manager.links[name]
        
        embed = discord.Embed(
            title=f"Link Info: ?{name}",
            color=0x5865f2 if data.get("enabled", True) else 0x95a5a6
        )
        
        embed.add_field(name="Command", value=f"`?{name}`", inline=True)
        embed.add_field(
            name="Status",
            value="Enabled"if data.get("enabled", True) else "Disabled",
            inline=True
        )
        
        if data.get("description"):
            embed.add_field(name="Description", value=data["description"], inline=False)
        
        if data.get("url"):
            embed.add_field(name="URL", value=data["url"], inline=False)
        else:
            embed.add_field(name="URL", value="Not configured", inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    bot.logger.log(MODULE_NAME, "Links module setup complete")
    bot.logger.log(MODULE_NAME, f"Loaded {len(link_manager.links)} link commands")