import discord
from discord import app_commands
from discord.ext import commands
import json
from _utils import _now
from mod_core import (
    MODULE_NAME, _db_exec, _db_one, _db_all,
    SUSPICION_THRESHOLD, SIGNAL_WEIGHTS, _THROWAWAY_PATTERNS,
    _is_default_avatar, has_elevated_role, has_owner_role,
    ERROR_NO_PERMISSION, ERROR_CANNOT_ACTION_SELF,
)

_suspicion_engine: "SuspicionEngine | None" = None


class SuspicionEngine:

    def __init__(self, bot: commands.Bot, db_path: str, cfg: "ModConfig"):
        self.bot      = bot
        self._db      = db_path
        self.cfg      = cfg

    def _exec(self, q, p=()):  _db_exec(self._db, q, p)
    def _one(self, q, p=()):   return _db_one(self._db, q, p)
    def _all(self, q, p=()):   return _db_all(self._db, q, p)

    async def score_member(self, member: discord.Member,
                           invite_source: str = "custom") -> dict:
        gid = str(member.guild.id)
        uid = str(member.id)
        now = _now()

        signals: list[str] = []
        score = 0

        def add(signal: str):
            w = SIGNAL_WEIGHTS.get(signal, 0)
            signals.append(signal)
            return w

        acct_age = (now - member.created_at).days
        if acct_age < 7:
            score += add("account_age_under_7d")
        elif acct_age < 30:
            score += add("account_age_under_30d")

        if _is_default_avatar(member):
            score += add("default_avatar")

        uname = (member.name or "").lower()
        if any(p.match(uname) for p in _THROWAWAY_PATTERNS):
            score += add("throwaway_username")

        if member.joined_at:
            join_age = (now - member.joined_at).days
            if join_age < 7:
                score += add("joined_recently_under_7d")

        existing = self._one(
            "SELECT * FROM mod_suspicion WHERE guild_id=? AND user_id=?", (gid, uid))
        if existing:
            msg_count = self._one(
                "SELECT msg_count FROM mod_suspicion WHERE guild_id=? AND user_id=?", (gid, uid))
            if msg_count and msg_count["msg_count"] == 0:
                score += add("no_messages")

        releases_role_name = self.cfg.get("releases_role_name",
                                          self.bot.__dict__.get("_remasters_role_name",
                                                                "Emball Releases"))
        non_default_roles = [r for r in member.roles
                             if r.name != "@everyone" and r.name != releases_role_name]
        if not non_default_roles:
            score += add("only_releases_role")

        if invite_source == "leaktracker":
            score += add("invite_leaktracker")
        elif invite_source == "youtube":
            score += add("invite_youtube")

        auto_flagged  = score >= SUSPICION_THRESHOLD
        flagged_at    = now.isoformat() if auto_flagged else None

        if existing and existing["cleared"]:
            auto_flagged = False
            flagged_at   = None

        self._exec(
            """
            INSERT INTO mod_suspicion
              (guild_id, user_id, score, flagged, cleared, invite_source,
               scored_at, flagged_at, signals)
            VALUES (?,?,?,?,0,?,?,?,?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              score         = excluded.score,
              flagged       = CASE WHEN cleared=1 THEN flagged ELSE excluded.flagged END,
              invite_source = excluded.invite_source,
              scored_at     = excluded.scored_at,
              flagged_at    = CASE WHEN cleared=1 THEN flagged_at ELSE excluded.flagged_at END,
              signals       = excluded.signals
            """,
            (gid, uid, score, int(auto_flagged),
             invite_source,
             now.isoformat(), flagged_at,
             json.dumps(signals))
        )

        record = self._one(
            "SELECT * FROM mod_suspicion WHERE guild_id=? AND user_id=?", (gid, uid))
        record = dict(record)

        if auto_flagged and not (existing and existing["flagged"]):
            self.bot.logger.log(
                MODULE_NAME,
                f"AUTO-FLAGGED {member} (id={uid}) - score {score}/{SUSPICION_THRESHOLD} "
                f"signals: {signals}"
            )
            await self._notify_mods(member, record)

        return record

    async def _notify_mods(self, member: discord.Member, record: dict) -> None:
        bot_logs = None
        for guild in self.bot.guilds:
            ch_id = self.cfg.bot_logs_channel_id
            if ch_id:
                bot_logs = guild.get_channel(ch_id)
                if bot_logs:
                    break
        if not bot_logs:
            return

        signals = json.loads(record.get("signals", "[]"))
        score   = record.get("score", 0)

        embed = discord.Embed(
            title=" Suspicious Account Flagged",
            color=discord.Color.from_rgb(255, 160, 50),
            timestamp=_now(),
        )
        embed.set_author(
            name=str(member),
            icon_url=member.display_avatar.url,
        )
        embed.add_field(name="User", value=member.mention, inline=True)
        embed.add_field(name="Score", value=f"`{score}` / threshold `{SUSPICION_THRESHOLD}`", inline=True)
        embed.add_field(name="Invite source", value=record.get('invite_source', 'custom'), inline=True)
        embed.add_field(
            name="Signals",
            value="\n".join(f" - `{s}`  (+{SIGNAL_WEIGHTS.get(s, 0)})" for s in signals) or "none",
            inline=False,
        )
        embed.set_footer(text=f"Use /fedcheck, /fedflag, or /fedclear to manage  -  ID: {member.id}")
        await bot_logs.send(embed=embed)

    def manual_flag(self, guild_id: str, user_id: str, note: str = "") -> None:
        now = _now().isoformat()
        self._exec(
            """
            INSERT INTO mod_suspicion
              (guild_id, user_id, score, flagged, cleared, scored_at, flagged_at, signals, note)
            VALUES (?,?,?,1,0,?,?,?,?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              flagged=1, cleared=0, flagged_at=excluded.flagged_at, note=excluded.note
            """,
            (guild_id, user_id, 0, now, now, "[]", note)
        )

    def manual_clear(self, guild_id: str, user_id: str, cleared_by: str) -> None:
        now = _now().isoformat()
        self._exec(
            """
            INSERT INTO mod_suspicion
              (guild_id, user_id, score, flagged, cleared, scored_at, cleared_at, cleared_by, signals)
            VALUES (?,?,?,0,1,?,?,?,?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              flagged=0, cleared=1, cleared_at=excluded.cleared_at, cleared_by=excluded.cleared_by
            """,
            (guild_id, user_id, 0, now, now, cleared_by, "[]")
        )

    def is_flagged(self, guild_id: str, user_id: str) -> bool:
        row = self._one(
            "SELECT flagged, cleared FROM mod_suspicion WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id))
        )
        if not row:
            return False
        return bool(row["flagged"]) and not bool(row["cleared"])

    def get_record(self, guild_id: str, user_id: str) -> dict | None:
        row = self._one(
            "SELECT * FROM mod_suspicion WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id))
        )
        return dict(row) if row else None


def is_flagged(guild_id, user_id: str) -> bool:
    engine = _suspicion_engine
    if not engine:
        return False
    return engine.is_flagged(str(guild_id), str(user_id))


def _setup_suspicion(bot: commands.Bot, _mod, _cfg: "ModConfig"):
    global _suspicion_engine

    engine = SuspicionEngine(bot, _mod._db, _cfg)
    _suspicion_engine = engine
    bot.suspicion     = engine

    @bot.listen("on_member_join")
    async def _suspicion_on_member_join(member: discord.Member):
        invite_source = "custom"
        try:
            invites = await member.guild.invites()
            labels: dict = _cfg.get("invite_labels", {})
            for inv in invites:
                for label, codes in labels.items():
                    if inv.code in (codes or []):
                        invite_source = label
                        break
        except discord.Forbidden:
            pass
        await engine.score_member(member, invite_source=invite_source)

    @bot.listen("on_member_update")
    async def _suspicion_on_member_update(before: discord.Member, after: discord.Member):
        if [r.id for r in before.roles] != [r.id for r in after.roles]:
            existing = engine.get_record(str(after.guild.id), str(after.id))
            if existing and not existing.get("cleared"):
                await engine.score_member(after)

    @bot.listen("on_message")
    async def _suspicion_on_message(message: discord.Message):
        if message.author.bot or not message.guild:
            return
        gid = str(message.guild.id)
        uid = str(message.author.id)
        try:
            engine._exec(
                "UPDATE mod_suspicion SET msg_count = COALESCE(msg_count, 0) + 1 "
                "WHERE guild_id=? AND user_id=?",
                (gid, uid)
            )
        except Exception:
            pass

    @bot.tree.command(name="fedcheck",
                      description="[Mod] Show suspicion report for a member")
    @app_commands.describe(member="Member to inspect")
    async def slash_fedcheck(interaction: discord.Interaction, member: discord.Member):
        if not has_elevated_role(interaction.user, _cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return

        record = engine.get_record(str(interaction.guild_id), str(member.id))

        embed = discord.Embed(
            title=f" Suspicion Report - {member}",
            color=(discord.Color.red() if (record and record["flagged"] and not record["cleared"])
                   else discord.Color.green() if (record and record["cleared"])
                   else discord.Color.greyple()),
            timestamp=_now(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Status",
                        value=("Flagged" if record and record["flagged"] and not record["cleared"]
                               else "Cleared" if record and record["cleared"]
                               else "Unscored"),
                        inline=True)
        embed.add_field(name="Score",
                        value=f"`{record['score'] if record else '-'}` / `{SUSPICION_THRESHOLD}`",
                        inline=True)

        if record:
            signals = json.loads(record.get("signals") or "[]")
            embed.add_field(
                name="Triggered signals",
                value=("\n".join(f" - `{s}`  (+{SIGNAL_WEIGHTS.get(s, 0)})" for s in signals)
                       or "none"),
                inline=False,
            )
            embed.add_field(name="Invite source",
                            value=record.get('invite_source', 'custom'),
                            inline=True)
            embed.add_field(name="Scored at",
                            value=record.get("scored_at", "-")[:19].replace("T", ""),
                            inline=True)
            if record.get("cleared"):
                embed.add_field(name="Cleared by",
                                value=record.get("cleared_by", "unknown"),
                                inline=True)
            if record.get("note"):
                embed.add_field(name="Note", value=record["note"], inline=False)

        acct_age = (_now() - member.created_at).days
        join_age = ((_now() - member.joined_at).days
                    if member.joined_at else "?")
        embed.add_field(name="Account age",  value=f"{acct_age}d", inline=True)
        embed.add_field(name="Server tenure", value=f"{join_age}d", inline=True)
        embed.set_footer(text=f"ID: {member.id}")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="fedflag",
                      description="[Mod] Manually flag a member as suspicious")
    @app_commands.describe(member="Member to flag", note="Optional note")
    async def slash_fedflag(interaction: discord.Interaction,
                            member: discord.Member,
                            note: str = ""):
        if not has_elevated_role(interaction.user, _cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_SELF, ephemeral=True)
            return

        engine.manual_flag(str(interaction.guild_id), str(member.id), note=note)
        bot.logger.log(MODULE_NAME,
                       f"MANUAL FLAG: {member} flagged by {interaction.user} - {note or 'no note'}")
        await interaction.response.send_message(
            f"**{member}** has been flagged. They will be silently denied access to "
            f"protected features.",
            ephemeral=True
        )

    @bot.tree.command(name="fedclear",
                      description="[Mod] Clear a suspicion flag from a member")
    @app_commands.describe(member="Member to clear")
    async def slash_fedclear(interaction: discord.Interaction, member: discord.Member):
        if not has_elevated_role(interaction.user, _cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return

        engine.manual_clear(str(interaction.guild_id), str(member.id),
                             cleared_by=str(interaction.user))
        bot.logger.log(MODULE_NAME,
                       f"CLEARED: {member} cleared by {interaction.user}")
        await interaction.response.send_message(
            f"Suspicion flag cleared for **{member}**. They now have normal access.",
            ephemeral=True
        )

    @bot.tree.command(name="fedscan",
                      description="[Owner only] Re-score all members in the server")
    async def slash_fedscan(interaction: discord.Interaction):
        if not has_owner_role(interaction.user, _cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild   = interaction.guild
        scanned = 0
        flagged = 0
        async for member in guild.fetch_members(limit=None):
            if member.bot:
                continue
            record = await engine.score_member(member)
            scanned += 1
            if record.get("flagged") and not record.get("cleared"):
                flagged += 1
        await interaction.followup.send(
            f"Scan complete - **{scanned}** members scored, **{flagged}** flagged.",
            ephemeral=True
        )

    @bot.tree.command(name="fedinvites",
                      description="[Mod] Show invite classification labels")
    async def slash_fedinvites(interaction: discord.Interaction):
        if not has_elevated_role(interaction.user, _cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        labels: dict = _cfg.get("invite_labels", {})
        if not labels:
            await interaction.response.send_message(
                "No invite labels configured yet. Add them to `config/mod.json` under "
                "`invite_labels`: `{\"leaktracker\": [\"code1\"], \"youtube\": [\"code2\"]}`",
                ephemeral=True
            )
            return
        lines = []
        for label, codes in labels.items():
            for code in codes:
                lines.append(f" - `{code}` -> **{label}**")
        await interaction.response.send_message(
            "**Invite label config:**\n" + "\n".join(lines) or "Empty.",
            ephemeral=True
        )

    try:
        _db_exec(_mod._db,
                 "ALTER TABLE mod_suspicion ADD COLUMN msg_count INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass

    bot.logger.log(MODULE_NAME, "Suspicion engine loaded - commands: /fedcheck /fedflag /fedclear /fedscan /fedinvites")


def setup(bot):
    bot.logger.log(MODULE_NAME, "Mod suspicion loaded")
