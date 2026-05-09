import discord
from discord.ext import tasks
import os
from datetime import datetime
import pytz

MODULE_NAME = "ICONS"

from _utils import script_dir

class IconManager:

    def __init__(self, bot):
        self.bot = bot
        self.icons_dir = str(script_dir() / 'icons')
        self.est = pytz.timezone('America/Detroit')

        self.icon_schedule = {
            'Emball_911.png': {
                'start': (9, 11),
                'end': (9, 11),
                'description': '9/11 Memorial',
                'single_day': True
            },
            'Emball_July4.png': {
                'start': (7, 4),
                'end': (7, 4),
                'description': 'Independence Day',
                'single_day': True
            },
            'Emball_Pride.png': {
                'start': (6, 1),
                'end': (6, 30),
                'description': 'Pride Month (June)',
            },
            'Emball_Halloween.png': {
                'start': (10, 1),
                'end': (10, 31),
                'description': 'Halloween (All October)'
            },
            'Emball_Christmas.png': {
                'start': (12, 1),
                'end': (12, 31),
                'description': 'Christmas (All December)'
            },
            'Emball_Thanksgiving.png': {
                'start': (11, 1),
                'end': (11, 30),
                'description': 'Thanksgiving (All November)'
            }
        }

        self.default_icon = 'Emball_Pit.png'
        self.current_icon = None
        self._last_avatar_change: datetime | None = None

    def should_use_icon(self, icon_name, now_est):
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

        if start_month == end_month:
            if current_month == start_month and start_day <= current_day <= end_day:
                self.bot.logger.log(MODULE_NAME,
                    f"{icon_name} MATCHES! Using this icon.")
                return True
        else:
            if (current_month == start_month and current_day >= start_day) or \
               (current_month == end_month and current_day <= end_day):
                self.bot.logger.log(MODULE_NAME,
                    f"{icon_name} MATCHES! Using this icon.")
                return True

        return False

    def get_appropriate_icon(self):
        now_est = datetime.now(self.est)

        self.bot.logger.log(MODULE_NAME,
            f"Current date (EST): {now_est.strftime('%Y-%m-%d %H:%M:%S')} (Month: {now_est.month}, Day: {now_est.day})")

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

        default_path = os.path.join(self.icons_dir, self.default_icon)
        if os.path.exists(default_path):
            self.bot.logger.log(MODULE_NAME, "Using default icon")
            return default_path, self.default_icon

        return None, None

    async def update_server_icon(self):
        try:
            icon_path, icon_name = self.get_appropriate_icon()

            if not icon_path:
                self.bot.logger.log(MODULE_NAME,
                    f"No valid icon found in {self.icons_dir} directory", "WARNING")
                return

            if icon_name == self.current_icon:
                self.bot.logger.log(MODULE_NAME,
                    f"Icon already set to {icon_name}, no change needed")
                return

            server_updated = False
            for guild in self.bot.guilds:
                try:
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

            now = datetime.now(self.est)
            cooldown_minutes = 65
            if self._last_avatar_change is not None:
                elapsed = (now - self._last_avatar_change).total_seconds() / 60
                if elapsed < cooldown_minutes:
                    self.bot.logger.log(MODULE_NAME,
                        f"Skipping avatar update — cooldown ({elapsed:.1f}/{cooldown_minutes} min elapsed)")
                    cooldown_ok = False
                else:
                    cooldown_ok = True
            else:
                cooldown_ok = True

            if cooldown_ok:
                try:
                    with open(icon_path, 'rb') as icon_file:
                        icon_data = icon_file.read()
                        await self.bot.user.edit(avatar=icon_data)
                        self._last_avatar_change = now
                        self.bot.logger.log(MODULE_NAME,
                            f"Updated bot profile picture to {icon_name}")
                except discord.HTTPException as e:
                    if e.status == 429:
                        self.bot.logger.log(MODULE_NAME,
                            "Avatar rate-limited by Discord — will retry next cycle", "WARNING")
                    elif e.code == 50035:
                        self.bot.logger.error(MODULE_NAME,
                            f"Failed to update bot profile picture - invalid image format or size: {e.text}")
                    else:
                        self.bot.logger.error(MODULE_NAME,
                            f"Failed to update bot profile picture", e)
                except discord.Forbidden:
                    self.bot.logger.error(MODULE_NAME,
                        "Missing permissions to change bot profile picture")
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
        self.bot.logger.log(MODULE_NAME, "Running periodic icon check")
        await self.update_server_icon()

    @icon_check_loop.before_loop
    async def before_icon_check(self):
        await self.bot.wait_until_ready()
        self.bot.logger.log(MODULE_NAME, "Icon manager initialized")

        if not os.path.exists(self.icons_dir):
            self.bot.logger.log(MODULE_NAME,
                f"Creating {self.icons_dir} directory", "WARNING")
            os.makedirs(self.icons_dir)

        self.bot.logger.log(MODULE_NAME, f"Icons directory path: {os.path.abspath(self.icons_dir)}")
        if os.path.exists(self.icons_dir):
            files = os.listdir(self.icons_dir)
            self.bot.logger.log(MODULE_NAME, f"Files found in icons directory: {files}")

        await self.update_server_icon()

def setup(bot):
    bot.logger.log(MODULE_NAME, "Setting up icon manager module")

    icon_manager = IconManager(bot)
    icon_manager.icon_check_loop.start()

    bot.logger.log(MODULE_NAME, "Icon manager module setup complete")
