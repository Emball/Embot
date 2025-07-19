# splash.py
import os
import time
import datetime
from pathlib import Path
import discord
import logging

logger = logging.getLogger('splash')

SPLASH_ART = """
╔═══╗╔╗──╔╗╔═══╗╔═══╗╔╗──╔╗╔═══╗
║╔═╗║║║──║║║╔═╗║║╔══╝║╚╗╔╝║║╔═╗║
║║─║║║║──║║║║─║║║╚══╗╚╗╚╝╔╝║║─╚╝
║╚═╝║║║─╔╣║║╚═╝║║╔══╝─╚╗╔╝─║║╔═╗
║╔═╗║║╚═╝║║║╔═╗║║╚══╗──║║──║╚╩═║
╚╝─╚╝╚═══╝╚╝─╚╝╚═══╝──╚╝──╚═══╝
"""

def show_console_splash(version):
    """Display ASCII art splash screen in console"""
    os.system('cls' if os.name == 'nt' else 'clear')
    print(SPLASH_ART)
    print(f"Version: {version}")
    print(f"Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*40)

def get_uptime_info():
    """Get uptime information from cache file"""
    uptime_file = Path("last_restart.txt")
    now = time.time()
    
    try:
        if uptime_file.exists():
            last_restart = float(uptime_file.read_text())
            uptime = now - last_restart
            return {
                'last_restart': last_restart,
                'uptime': uptime,
                'formatted': str(datetime.timedelta(seconds=uptime)).split(".")[0]
            }
    except Exception as e:
        logger.error(f"Error reading uptime info: {e}")
    
    # Create new file if doesn't exist or error occurred
    uptime_file.write_text(str(now))
    return {
        'last_restart': now,
        'uptime': 0,
        'formatted': "0:00:00"
    }

async def send_startup_message(bot, version):
    """Send startup embed to general channel"""
    uptime_info = get_uptime_info()
    general_channel = discord.utils.get(bot.get_all_channels(), name="general")
    
    if not general_channel:
        logger.warning("General channel not found for startup message")
        return

    embed = discord.Embed(
        title="🔄 Embot Online",
        color=0x00ff00,
        timestamp=datetime.datetime.utcnow()
    )
    
    embed.add_field(
        name="Version",
        value=version,
        inline=True
    )
    
    embed.add_field(
        name="Uptime",
        value=f"{uptime_info['formatted']} since last restart",
        inline=True
    )
    
    embed.add_field(
        name="Modules Loaded",
        value=f"{getattr(bot, 'loaded_extensions_count', 0)} extensions active",
        inline=False
    )
    
    if bot.user.avatar:
        embed.set_thumbnail(url=bot.user.avatar.url)
    
    embed.set_footer(text="Ready to serve!")
    
    try:
        await general_channel.send(embed=embed)
        logger.info("Startup message sent to general channel")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")