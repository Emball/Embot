import discord
import hashlib
import json
from typing import Optional
import asyncio
from _utils import _now
from mod_core import _db_one, _db_exec, ModConfig


class RulesManager:

    def __init__(self, bot, db_path: str, cfg: ModConfig):
        self.bot = bot
        self._db = db_path
        self.cfg = cfg

    def _get_state(self, guild_id: int) -> tuple:
        row = _db_one(self._db,
                      "SELECT message_id, rules_hash FROM mod_rules_state WHERE guild_id=?",
                      (str(guild_id),))
        if row:
            return row["message_id"], row["rules_hash"]
        return None, None

    def _save_state(self, guild_id: int, message_id: int, rules_hash: str):
        _db_exec(self._db,
                 "INSERT INTO mod_rules_state (guild_id, message_id, rules_hash) VALUES (?,?,?) "
                 "ON CONFLICT(guild_id) DO UPDATE SET "
                 "message_id=excluded.message_id, rules_hash=excluded.rules_hash",
                 (str(guild_id), message_id, rules_hash))

    def load_rules(self) -> Optional[dict]:
        data = self.cfg.get_rules()
        if data is None:
            self.bot.logger.log("RULES", "No rules content found in config/mod.json", "WARNING")
        return data

    def save_rules(self, data: dict) -> None:
        self.cfg.save_rules(data)

    @staticmethod
    def _hash_rules(data: dict) -> str:
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def get_rule_text(self, rule_number: int) -> Optional[str]:
        data = self.load_rules()
        if not data:
            return None
        for rule in data.get("rules", []):
            if rule.get("number") == rule_number:
                return f"**Rule {rule['number']} - {rule['title']}**: {rule['description']}"
        return None

    def list_rules_summary(self) -> list:
        data = self.load_rules()
        if not data:
            return []
        return [f"Rule {r['number']} - {r['title']}" for r in data.get("rules", [])]

    def build_embed(self, data: dict) -> discord.Embed:
        color = data.get("color", 0x3498db)
        embed = discord.Embed(
            title=f" {data.get('title', 'Server Rules')}",
            description=data.get("description", ""),
            color=color,
            timestamp=_now(),
        )
        for rule in data.get("rules", []):
            embed.add_field(
                name=f"Rule {rule['number']}  -  {rule['title']}",
                value=rule["description"],
                inline=False,
            )
        embed.set_footer(text=data.get("footer", "Please follow the rules"))
        return embed

    def _get_rules_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        return discord.utils.get(guild.text_channels, name=self.cfg.rules_channel_name)

    async def sync(self, guild: discord.Guild, *, force: bool = False) -> bool:
        data = self.load_rules()
        if not data:
            return False

        current_hash = self._hash_rules(data)
        channel      = self._get_rules_channel(guild)
        if not channel:
            self.bot.logger.log("RULES", f"#rules channel not found in {guild.name}", "WARNING")
            return False

        posted_msg_id, posted_hash = self._get_state(guild.id)

        existing_ok = False
        if posted_msg_id and not force:
            try:
                msg = await channel.fetch_message(posted_msg_id)
                if current_hash == posted_hash:
                    existing_ok = True
                else:
                    await msg.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                self.bot.logger.log("RULES", f"Could not fetch rules message: {e}", "WARNING")

        if existing_ok:
            return False

        try:
            async for msg in channel.history(limit=50):
                if msg.author == guild.me and msg.embeds:
                    await msg.delete()
        except Exception:
            pass

        embed = self.build_embed(data)
        try:
            new_msg = await channel.send(embed=embed)
            self._save_state(guild.id, new_msg.id, current_hash)
            self.bot.logger.log("RULES", f"Rules embed posted (message {new_msg.id})")
            return True
        except Exception as e:
            self.bot.logger.log("RULES", f"Failed to post rules embed: {e}", "ERROR")
            return False

    async def on_ready(self, guild: discord.Guild):
        await self.sync(guild)

    def start_watcher(self, guild: discord.Guild):
        self._watch_guild = guild
        if not self._watcher_task_running():
            self._watch_task = self.bot.loop.create_task(self._watch_loop())

    def _watcher_task_running(self) -> bool:
        return hasattr(self, "_watch_task") and not self._watch_task.done()

    async def _watch_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            try:
                guild = self._watch_guild
                if guild:
                    data = self.load_rules()
                    if data:
                        current_hash = self._hash_rules(data)
                        _, posted_hash = self._get_state(guild.id)
                        if current_hash != posted_hash:
                            self.bot.logger.log("RULES", "rules content change detected - syncing embed")
                            await self.sync(guild, force=True)
            except Exception as e:
                self.bot.logger.log("RULES", f"Watcher error: {e}", "WARNING")


def setup(bot):
    bot.logger.log("MOD_RULES", "Mod rules loaded")
