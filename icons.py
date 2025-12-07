import discord
from discord.ext import tasks
import os
from datetime import datetime, timedelta
import pytz

MODULE_NAME = "ICONS"

class IconManager:
    """Manages automatic server icon changes based on holidays and special dates"""
    
    def __init__(self, bot):
        self.bot = bot
        # Use path relative to the current script location (bot directory)
        self.icons_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icons')
        self.est = pytz.timezone('America/Detroit')
        
        # Icon configuration: filename -> (start_date, end_date, description)
        # Dates are (month, day) tuples
        self.icon_schedule = {
            'Emball_911.png': {
                'start': (9, 11),  # September 11
                'end': (9, 11),
                'description': '9/11 Memorial',
                'single_day': True
            },
            'Emball_July4.png': {
                'start': (7, 4),  # July 4
                'end': (7, 4),
                'description': 'Independence Day',
                'single_day': True
            },
            'Emball_Pride.png': {
                'start': (6, 1),  # June 1
                'end': (6, 1),
                'description': 'Pride Month (First Day)',
                'single_day': True
            },
            'Emball_Halloween.png': {
                'start': (10, 1),  # All of October
                'end': (10, 31),  # Ends at midnight after Oct 31
                'description': 'Halloween (All October)'
            },
            'Emball_Christmas.png': {
                'start': (12, 1),  # All of December
                'end': (12, 31),  # Ends at midnight after Dec 31
                'description': 'Christmas (All December)'
            },
            'Emball_Thanksgiving.png': {
                'start': (11, 1),  # All of November
                'end': (11, 30),  # Ends at midnight after Nov 30
                'description': 'Thanksgiving (All November)'
            }
        }
        
        self.default_icon = 'Emball_Pit.png'
        self.current_icon = None
    
    def should_use_icon(self, icon_name, now_est):
        """Determine if a specific icon should be active right now"""
        config = self.icon_schedule.get(icon_name)
        if not config:
            return False
        
        current_month = now_est.month
        current_day = now_est.day
        
        start = config['start']
        end = config['end']
        
        self.bot.logger.log(MODULE_NAME, 
            f"Checking {icon_name}: Start: {start}, End: {end}, Current: ({current_month}, {current_day})")
        
        start_month, start_day = start
        end_month, end_day = end
        
        # Check if we're in the date range
        if start_month == end_month:
            # Same month
            if current_month == start_month and start_day <= current_day <= end_day:
                self.bot.logger.log(MODULE_NAME, 
                    f"{icon_name} MATCHES! Using this icon.")
                return True
        else:
            # Spans multiple months (shouldn't happen with current config, but just in case)
            if (current_month == start_month and current_day >= start_day) or \
               (current_month == end_month and current_day <= end_day):
                self.bot.logger.log(MODULE_NAME, 
                    f"{icon_name} MATCHES! Using this icon.")
                return True
        
        return False
    
    def get_appropriate_icon(self):
        """Determine which icon should be used right now"""
        now_est = datetime.now(self.est)
        
        self.bot.logger.log(MODULE_NAME, 
            f"Current date (EST): {now_est.strftime('%Y-%m-%d %H:%M:%S')} (Month: {now_est.month}, Day: {now_est.day})")
        
        # Check each special icon in priority order
        for icon_name in self.icon_schedule.keys():
            if self.should_use_icon(icon_name, now_est):
                icon_path = os.path.join(self.icons_dir, icon_name)
                if os.path.exists(icon_path):
                    config = self.icon_schedule[icon_name]
                    self.bot.logger.log(MODULE_NAME, 
                        f"Special date detected: {config['description']}")
                    return icon_path, icon_name
                else:
                    self.bot.logger.log(MODULE_NAME, 
                        f"Icon file not found: {icon_path}", "WARNING")
        
        # Default icon
        default_path = os.path.join(self.icons_dir, self.default_icon)
        if os.path.exists(default_path):
            self.bot.logger.log(MODULE_NAME, "Using default icon")
            return default_path, self.default_icon
        
        return None, None
    
    async def update_server_icon(self):
        """Check and update server icon if needed"""
        try:
            # Get the appropriate icon for today
            icon_path, icon_name = self.get_appropriate_icon()
            
            if not icon_path:
                self.bot.logger.log(MODULE_NAME, 
                    f"No valid icon found in {self.icons_dir} directory", "WARNING")
                return
            
            # Check if icon needs to be changed
            if icon_name == self.current_icon:
                self.bot.logger.log(MODULE_NAME, 
                    f"Icon already set to {icon_name}, no change needed")
                return
            
            # Update server icons for all guilds
            server_updated = False
            for guild in self.bot.guilds:
                try:
                    # Check if bot has permission to change guild icon
                    if guild.me.guild_permissions.manage_guild:
                        with open(icon_path, 'rb') as icon_file:
                            icon_data = icon_file.read()
                            await guild.edit(icon=icon_data)
                            self.bot.logger.log(MODULE_NAME, 
                                f"Updated server icon for guild '{guild.name}' to {icon_name}")
                            server_updated = True
                    else:
                        self.bot.logger.log(MODULE_NAME, 
                            f"Missing 'manage_guild' permission to change icon in guild '{guild.name}'")
                except discord.Forbidden:
                    self.bot.logger.error(MODULE_NAME, 
                        f"Missing permissions to change server icon in guild '{guild.name}'")
                except Exception as e:
                    self.bot.logger.error(MODULE_NAME, 
                        f"Failed to update server icon for guild '{guild.name}'", e)
            
            # Update bot's profile picture
            try:
                with open(icon_path, 'rb') as icon_file:
                    icon_data = icon_file.read()
                    await self.bot.user.edit(avatar=icon_data)
                    self.bot.logger.log(MODULE_NAME, 
                        f"Updated bot profile picture to {icon_name}")
            except discord.Forbidden:
                self.bot.logger.error(MODULE_NAME, 
                    "Missing permissions to change bot profile picture")
            except discord.HTTPException as e:
                if e.code == 50035:  # Invalid Form Body - usually means image is too large or invalid format
                    self.bot.logger.error(MODULE_NAME, 
                        f"Failed to update bot profile picture - invalid image format or size: {e.text}")
                else:
                    self.bot.logger.error(MODULE_NAME, 
                        f"Failed to update bot profile picture", e)
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, 
                    "Unexpected error updating bot profile picture", e)
            
            self.current_icon = icon_name
            
            if server_updated:
                self.bot.logger.log(MODULE_NAME, 
                    f"Icon update completed successfully - using {icon_name}")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error in update_server_icon", e)
    
    @tasks.loop(hours=1)
    async def icon_check_loop(self):
        """Periodic task to check if icon needs updating"""
        self.bot.logger.log(MODULE_NAME, "Running periodic icon check")
        await self.update_server_icon()
    
    @icon_check_loop.before_loop
    async def before_icon_check(self):
        """Wait until bot is ready before starting the loop"""
        await self.bot.wait_until_ready()
        self.bot.logger.log(MODULE_NAME, "Icon manager initialized")
        
        # Verify icons directory exists
        if not os.path.exists(self.icons_dir):
            self.bot.logger.log(MODULE_NAME, 
                f"Creating {self.icons_dir} directory", "WARNING")
            os.makedirs(self.icons_dir)
        
        # Debug: Log the icons directory path and contents
        self.bot.logger.log(MODULE_NAME, f"Icons directory path: {os.path.abspath(self.icons_dir)}")
        if os.path.exists(self.icons_dir):
            files = os.listdir(self.icons_dir)
            self.bot.logger.log(MODULE_NAME, f"Files found in icons directory: {files}")
        
        # Run initial check immediately
        await self.update_server_icon()

def setup(bot):
    """Setup function called by main bot to initialize this module"""
    bot.logger.log(MODULE_NAME, "Setting up icon manager module")
    
    icon_manager = IconManager(bot)
    icon_manager.icon_check_loop.start()
    
    bot.logger.log(MODULE_NAME, "Icon manager module setup complete")