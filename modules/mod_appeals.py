import discord
from discord import ui
import json
from datetime import timedelta
from typing import Optional
from _utils import _now
from mod_core import (
    MODULE_NAME, ModConfig, _db_exec, _db_one, _db_all, has_elevated_role,
)
from mod_oversight import send_bot_log, _create_ban_reversal_invite


class BanAppealView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @ui.button(label="Submit Appeal", style=discord.ButtonStyle.primary, emoji="\U0001f4dd",
               custom_id="ban_appeal_submit")
    async def appeal_button(self, interaction: discord.Interaction, button: ui.Button):
        if not hasattr(interaction.client, 'moderation') or \
                not hasattr(interaction.client.moderation, 'submit_appeal'):
            await interaction.response.send_message(
                "Appeal system not available.", ephemeral=True)
            return
        guild_id = interaction.guild_id or self.guild_id
        modal = BanAppealModal(interaction.client.moderation, guild_id)
        await interaction.response.send_modal(modal)


class AppealVoteView(ui.View):
    def __init__(self, moderation_system, appeal_id: str):
        super().__init__(timeout=None)
        self.moderation = moderation_system
        self.appeal_id  = appeal_id
        yes_btn = ui.Button(label="Vote Yes", style=discord.ButtonStyle.green,
                            emoji="\u2705", custom_id=f"appeal_accept:{appeal_id}")
        no_btn  = ui.Button(label="Vote No",  style=discord.ButtonStyle.red,
                            emoji="\u274c", custom_id=f"appeal_deny:{appeal_id}")
        yes_btn.callback = self._accept_callback
        no_btn.callback  = self._deny_callback
        self.add_item(yes_btn)
        self.add_item(no_btn)

    def _updated_embed(self, message: discord.Message,
                        votes_for: list, votes_against: list) -> discord.Embed:
        old = message.embeds[0] if message.embeds else None
        embed = discord.Embed(
            title=old.title if old else "Ban Appeal",
            description=old.description if old else "",
            color=old.color if old else 0x9b59b6,
            timestamp=old.timestamp if old else _now(),
        )
        for field in (old.fields if old else []):
            if field.name == "Votes":
                continue
            embed.add_field(name=field.name, value=field.value, inline=field.inline)
        embed.add_field(
            name="Votes",
            value=f"Yes: **{len(votes_for)}**  -   No: **{len(votes_against)}**",
            inline=False,
        )
        if old and old.footer:
            embed.set_footer(text=old.footer.text)
        return embed

    async def _accept_callback(self, interaction: discord.Interaction):
        await self._vote_callback(interaction, is_yes=True)

    async def _deny_callback(self, interaction: discord.Interaction):
        await self._vote_callback(interaction, is_yes=False)

    async def _vote_callback(self, interaction: discord.Interaction, is_yes: bool):
        cfg = self.moderation.cfg
        if not has_elevated_role(interaction.user, cfg):
            await interaction.response.send_message(
                "Only moderators can vote on appeals.", ephemeral=True)
            return
        appeal = appeal_get(self.moderation, self.appeal_id)
        if not appeal:
            await interaction.response.send_message(
                "Appeal no longer exists.", ephemeral=True)
            return

        uid           = str(interaction.user.id)
        votes_for     = json.loads(appeal["votes_for"])
        votes_against = json.loads(appeal["votes_against"])

        target, opposite = (votes_for, votes_against) if is_yes else (votes_against, votes_for)
        label = "Yes" if is_yes else "No"

        if uid in target:
            await interaction.response.send_message(
                f"You already voted {label}.", ephemeral=True)
            return
        if uid in opposite:
            opposite.remove(uid)
        target.append(uid)

        appeal_update_votes(self.moderation, self.appeal_id, votes_for, votes_against)
        updated_embed = self._updated_embed(interaction.message, votes_for, votes_against)
        await interaction.response.edit_message(embed=updated_embed, view=self)


class BanAppealModal(ui.Modal, title="Ban Appeal"):
    appeal_text = ui.TextInput(
        label="Why should you be unbanned?",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why you believe the ban should be lifted...",
        required=True,
        max_length=1000,
    )

    def __init__(self, moderation_system, guild_id: int):
        super().__init__()
        self.moderation = moderation_system
        self.guild_id   = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await appeal_submit(
            self.moderation, interaction.user.id, self.guild_id, self.appeal_text.value)
        embed = discord.Embed(
            title="Appeal Submitted",
            description="Your ban appeal has been submitted and will be reviewed.",
            color=0x2ecc71,
            timestamp=_now(),
        )
        embed.add_field(
            name="What happens next?",
            value="Your appeal has been posted to the staff team. They will vote on it "
                  "over the next 24 hours and you'll be notified of the outcome automatically.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


def _generate_appeal_id() -> str:
    import string, secrets
    chars = string.ascii_letters + string.digits + "-_"
    return ''.join(secrets.choice(chars) for _ in range(11))


def appeal_get(ms, appeal_id: str) -> Optional[dict]:
    row = _db_one(ms._db,
                  "SELECT * FROM mod_appeals WHERE appeal_id=?", (appeal_id,))
    return dict(row) if row else None


def appeal_update_votes(ms, appeal_id: str, votes_for: list, votes_against: list):
    _db_exec(ms._db,
        "UPDATE mod_appeals SET votes_for=?, votes_against=? WHERE appeal_id=?",
        (json.dumps(votes_for), json.dumps(votes_against), appeal_id),
    )


async def appeal_submit(ms, user_id: int, guild_id: int, appeal_text: str) -> str:
    appeal_id = _generate_appeal_id()
    deadline  = (_now() + timedelta(hours=24)).isoformat()
    _db_exec(ms._db,
        "INSERT OR REPLACE INTO mod_appeals "
        "(appeal_id, user_id, guild_id, appeal_text, submitted_at, deadline, "
        "status, votes_for, votes_against, channel_message_id) "
        "VALUES (?,?,?,?,?,?,'pending','[]','[]',NULL)",
        (appeal_id, user_id, guild_id, appeal_text,
         _now().isoformat(), deadline),
    )
    ms.bot.logger.log(MODULE_NAME, f"Appeal submitted: {appeal_id}")

    try:
        guild = ms.bot.get_guild(guild_id)
        if guild:
            appeals_ch = discord.utils.get(guild.text_channels, name="mod-chat")
            if appeals_ch:
                user = await ms.bot.fetch_user(user_id)
                embed = discord.Embed(
                    title="Ban Appeal",
                    description=appeal_text,
                    color=0x9b59b6, timestamp=_now())
                embed.add_field(name="User",
                                value=f"{user} (`{user_id}`)", inline=True)
                embed.add_field(
                    name="Deadline",
                    value=f"<t:{int((_now() + timedelta(hours=24)).timestamp())}:R>",
                    inline=True)
                embed.add_field(
                    name="Votes",
                    value="Yes: **0**  -   No: **0**",
                    inline=False)
                embed.set_footer(
                    text=f"Appeal ID: {appeal_id} - "
                         f"Voting closes in 24 hours - Ties are denied")
                view = AppealVoteView(ms, appeal_id)
                msg  = await appeals_ch.send(embed=embed, view=view)
                _db_exec(ms._db,
                    "UPDATE mod_appeals SET channel_message_id=? WHERE appeal_id=?",
                    (msg.id, appeal_id),
                )
            else:
                ms.bot.logger.log(
                    MODULE_NAME,
                    "appeal_submit: #mod-chat channel not found", "WARNING")
    except Exception as e:
        ms.bot.logger.error(MODULE_NAME, "Failed to post appeal", e)

    return appeal_id


async def appeal_approve(ms, appeal_id: str) -> bool:
    row = appeal_get(ms, appeal_id)
    if not row:
        return False
    _db_exec(ms._db, "DELETE FROM mod_appeals WHERE appeal_id=?", (appeal_id,))
    try:
        guild = ms.bot.get_guild(row["guild_id"])
        if not guild:
            return False
        user = await ms.bot.fetch_user(row["user_id"])
        try:
            await guild.unban(user, reason="Appeal approved")
        except discord.NotFound:
            ms.bot.logger.log(MODULE_NAME,
                f"Appeal {appeal_id}: user {row['user_id']} was not banned - skipping unban")
        invite_link = await _create_ban_reversal_invite(ms, guild, row["user_id"])
        try:
            embed = discord.Embed(
                title="Ban Appeal Approved",
                description=f"Your appeal for **{guild.name}** has been approved!",
                color=0x2ecc71, timestamp=_now())
            embed.add_field(
                name="Rejoin Server",
                value=f"You can rejoin using this invite:\n{invite_link}", inline=False)
            embed.set_footer(text="Welcome back!")
            await user.send(embed=embed)
        except discord.Forbidden:
            pass
        ch = None
        for g in ms.bot.guilds:
            ch_id = ms.cfg.bot_logs_channel_id
            if ch_id:
                ch = g.get_channel(ch_id)
                if ch:
                    break
        if not ch:
            logger = getattr(ms.bot, '_logger_event_logger', None)
            if logger:
                ch = logger.get_bot_logs_channel(guild)
        if ch:
            log_embed = discord.Embed(
                title="Ban Appeal Approved",
                description=f"**{user}** has been unbanned after appeal approval.",
                color=0x2ecc71, timestamp=_now())
            log_embed.add_field(name="Appeal Text",
                                value=row["appeal_text"][:1024], inline=False)
            await ch.send(embed=log_embed)
        return True
    except Exception as e:
        ms.bot.logger.error(MODULE_NAME, f"Failed to approve appeal {appeal_id}", e)
        return False


async def appeal_deny(ms, appeal_id: str) -> bool:
    row = appeal_get(ms, appeal_id)
    if not row:
        return False
    try:
        guild = ms.bot.get_guild(row["guild_id"])
        user  = await ms.bot.fetch_user(row["user_id"])
        try:
            guild_name = guild.name if guild else f"server {row['guild_id']}"
            embed = discord.Embed(
                title="Ban Appeal Denied",
                description=f"Your appeal for **{guild_name}** has been reviewed and denied.",
                color=0xe74c3c, timestamp=_now())
            await user.send(embed=embed)
        except discord.Forbidden:
            pass
        _db_exec(ms._db, "DELETE FROM mod_appeals WHERE appeal_id=?", (appeal_id,))
        return True
    except Exception as e:
        ms.bot.logger.error(MODULE_NAME, f"Failed to deny appeal {appeal_id}", e)
        return False


async def resolve_expired_appeals_task(ms):
    try:
        now      = _now().isoformat()
        past_due = _db_all(ms._db,
            "SELECT * FROM mod_appeals WHERE status='pending' AND deadline <= ?",
            (now,))
        for row in past_due:
            appeal_id     = row["appeal_id"]
            votes_for     = len(json.loads(row["votes_for"]))
            votes_against = len(json.loads(row["votes_against"]))
            accepted      = votes_for > votes_against

            ms.bot.logger.log(
                MODULE_NAME,
                f"Appeal {appeal_id} deadline reached - "
                f"Accept: {votes_for}, Deny: {votes_against} -> "
                f"{'APPROVED' if accepted else 'DENIED'}")

            try:
                guild = ms.bot.get_guild(row["guild_id"])
                if guild:
                    appeals_ch = discord.utils.get(
                        guild.text_channels, name="mod-chat")
                    if appeals_ch and row["channel_message_id"]:
                        try:
                            msg    = await appeals_ch.fetch_message(
                                row["channel_message_id"])
                            result_color = 0x2ecc71 if accepted else 0xe74c3c
                            result_text  = (
                                f"Accepted ({votes_for}-{votes_against})"
                                if accepted else
                                f"Denied ({votes_against}-{votes_for})")
                            embed = msg.embeds[0] if msg.embeds else discord.Embed()
                            embed.color = result_color
                            embed.add_field(
                                name="Result", value=result_text, inline=False)
                            embed.set_footer(
                                text=f"Appeal ID: {appeal_id} - Voting closed")
                            disabled_view = AppealVoteView(ms, appeal_id)
                            for item in disabled_view.children:
                                item.disabled = True
                            await msg.edit(embed=embed, view=disabled_view)
                        except Exception as e:
                            ms.bot.logger.error(
                                MODULE_NAME, "Failed to update appeal message", e)
            except Exception as e:
                ms.bot.logger.error(
                    MODULE_NAME, "Failed to resolve appeal channel message", e)

            if accepted:
                await appeal_approve(ms, appeal_id)
            else:
                await appeal_deny(ms, appeal_id)
    except Exception as e:
        ms.bot.logger.error(MODULE_NAME, "Error in appeal resolution task", e)
