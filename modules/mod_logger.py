import io
import discord
from discord import app_commands
from discord.ui import media_gallery as _mg
from typing import Optional
from _utils import _now

MODULE_NAME = "LOGGER"

image_exts = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
audio_exts = ('.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.opus', '.mp4', '.mov', '.webm')


def _layout(*items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _container(*children) -> discord.ui.Container:
    return discord.ui.Container(*children)


def _text(content: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(content)


def _sep() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def _section_with_avatar(text: str, avatar_url: str) -> discord.ui.Container:
    if '\n-#' in text:
        main, footer = text.split('\n-#', 1)
        return discord.ui.Container(
            discord.ui.Section(
                _text(main),
                accessory=discord.ui.Thumbnail(avatar_url)
            ),
            _sep(),
            _text(f'-#{footer}')
        )
    return discord.ui.Container(
        discord.ui.Section(
            _text(text),
            accessory=discord.ui.Thumbnail(avatar_url)
        )
    )


class EventLogger:

    def __init__(self, bot):
        self.bot = bot
        self._scanner = None

    @property
    def mod_cfg(self):
        ms = getattr(self.bot, 'moderation', None) or getattr(self.bot, 'moderation_manager', None)
        return ms.cfg if ms else None

    def get_join_logs_channel(self, guild):
        ch_id = self.mod_cfg.join_logs_channel_id if self.mod_cfg else 0
        return guild.get_channel(ch_id) if ch_id else None

    def get_bot_logs_channel(self, guild):
        ch_id = self.mod_cfg.bot_logs_channel_id if self.mod_cfg else 0
        return guild.get_channel(ch_id) if ch_id else None

    def _get_scanner(self):
        if self._scanner is not None:
            return self._scanner
        mod_sys = getattr(self.bot, '_mod_system', None)
        if mod_sys and hasattr(mod_sys, 'scanner'):
            self._scanner = mod_sys.scanner
        return self._scanner

    async def _send(self, channel, view: discord.ui.LayoutView, files: list = None) -> Optional[int]:
        if not channel:
            return None
        try:
            kwargs = {"view": view}
            if files:
                kwargs["files"] = files
            msg = await channel.send(**kwargs)
            return msg.id
        except discord.Forbidden:
            self.bot.logger.error(MODULE_NAME, f"No permission to send logs to {channel.name}")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to send log to {channel.name}", e)
        return None

    async def log_message_delete(self, message, rehosted_files: list = None):
        if not self.mod_cfg.get("log_message_deletes"):
            return
        if message.author.bot:
            return
        suppressed = getattr(self.bot, '_automod_purged_ids', set())
        if message.id in suppressed:
            suppressed.discard(message.id)
            return

        channel = self.get_bot_logs_channel(message.guild)
        if not channel:
            return

        header = f"🗑️ **Message deleted** in {message.channel.mention}\n-# {message.author} • ID: {message.author.id} • Msg: {message.id}"
        body = message.content if message.content else "*[No text content]*"
        main_text = f"{header}\n\n{body}"

        if rehosted_files:
            scanner = self._get_scanner()
            if scanner:
                verdict = await scanner.scan_files(
                    rehosted_files,
                    guild=message.guild,
                    context=f"deleted message {message.id} by {message.author}",
                )
                if verdict.blocked and not verdict.safe_files:
                    view = _layout(
                        _section_with_avatar(header, message.author.display_avatar.url),
                        _sep(),
                        _container(_text("⚠️ **Attachment(s) Withheld** — one or more files were blocked by MediaScanner. The server owner has been alerted."))
                    )
                    await self._send(channel, view)
                    return
                rehosted_files = verdict.safe_files

            discord_files = [
                discord.File(fp=io.BytesIO(f['data']), filename=f['filename'])
                for f in rehosted_files
            ]

            img_names = [f['filename'] for f in rehosted_files if f['filename'].lower().endswith(image_exts)]
            other_names = [f['filename'] for f in rehosted_files if not f['filename'].lower().endswith(image_exts)]

            items = [_section_with_avatar(main_text, message.author.display_avatar.url)]

            if img_names:
                items.append(_sep())
                items.append(_container(discord.ui.MediaGallery(
                    *[_mg.MediaGalleryItem(f"attachment://{name}") for name in img_names]
                )))

            if other_names:
                items.append(_sep())
                for name in other_names:
                    items.append(_container(discord.ui.File(f"attachment://{name}")))

            await self._send(channel, _layout(*items), files=discord_files)
        else:
            items = [_section_with_avatar(main_text, message.author.display_avatar.url)]

            if message.attachments:
                img_atts = [a for a in message.attachments if a.filename.lower().endswith(image_exts)]
                if img_atts:
                    items.append(_sep())
                    items.append(_container(discord.ui.MediaGallery(
                        *[_mg.MediaGalleryItem(a.url) for a in img_atts]
                    )))

            await self._send(channel, _layout(*items))

    async def log_message_edit(self, before, after):
        if not self.mod_cfg.get("log_message_edits"):
            return
        if before.author.bot:
            return
        if before.content == after.content:
            return

        channel = self.get_bot_logs_channel(before.guild)
        if not channel:
            return

        before_content = before.content if before.content else "*[No text content]*"
        if len(before_content) > 800:
            before_content = before_content[:797] + "..."

        after_content = after.content if after.content else "*[No text content]*"
        if len(after_content) > 800:
            after_content = after_content[:797] + "..."

        text = (
            f"✏️ **Message edited** in {after.channel.mention} • [Jump]({after.jump_url})\n"
            f"-# {after.author} • ID: {after.author.id} • Msg: {after.id}\n\n"
            f"**Before**\n{before_content}\n\n"
            f"**After**\n{after_content}"
        )

        view = _layout(_section_with_avatar(text, after.author.display_avatar.url))
        await self._send(channel, view)

    async def log_bulk_message_delete(self, messages):
        if not self.mod_cfg.get("log_message_deletes"):
            return
        if not messages:
            return

        channel = self.get_bot_logs_channel(messages[0].guild)
        if not channel:
            return

        text = f"🗑️ **{len(messages)} messages bulk deleted** in {messages[0].channel.mention}"
        view = _layout(_container(_text(text)))
        await self._send(channel, view)

    async def log_member_join(self, member):
        if not self.mod_cfg.get("log_member_joins"):
            return

        channel = self.get_join_logs_channel(member.guild)
        if not channel:
            return

        account_age = _now() - member.created_at
        years = account_age.days // 365
        months = (account_age.days % 365) // 30
        days = (account_age.days % 365) % 30

        age_parts = []
        if years > 0:
            age_parts.append(f"{years} year{'s' if years != 1 else ''}")
        if months > 0:
            age_parts.append(f"{months} month{'s' if months != 1 else ''}")
        if days > 0 or not age_parts:
            age_parts.append(f"{days} day{'s' if days != 1 else ''}")

        text = (
            f"✅ **Member Joined**\n"
            f"{member.mention} {member.name}\n"
            f"-# ID: {member.id}\n\n"
            f"**Account Age:** {', '.join(age_parts)}"
        )

        view = _layout(_section_with_avatar(text, member.display_avatar.url))
        await self._send(channel, view)

    async def log_member_leave(self, member):
        if not self.mod_cfg.get("log_member_leaves"):
            return

        channel = self.get_join_logs_channel(member.guild)
        if not channel:
            return

        text = (
            f"👋 **Member Left**\n"
            f"{member.mention} {member.name}\n"
            f"-# ID: {member.id}"
        )

        view = _layout(_section_with_avatar(text, member.display_avatar.url))
        await self._send(channel, view)

    async def log_member_update(self, before, after):
        guild = after.guild
        channel = self.get_bot_logs_channel(guild)
        if not channel:
            return

        if self.mod_cfg.get("log_role_changes") and before.roles != after.roles:
            added_roles = [r for r in after.roles if r not in before.roles]
            removed_roles = [r for r in before.roles if r not in after.roles]

            if added_roles or removed_roles:
                lines = [f"🎭 **Roles updated** for {after.mention}\n-# {after} • ID: {after.id}"]
                if added_roles:
                    lines.append(f"\n**Added:** {', '.join(r.mention for r in added_roles)}")
                if removed_roles:
                    lines.append(f"**Removed:** {', '.join(r.mention for r in removed_roles)}")

                view = _layout(_section_with_avatar("\n".join(lines), after.display_avatar.url))
                await self._send(channel, view)

        if self.mod_cfg.get("log_nickname_changes") and before.nick != after.nick:
            before_nick = before.nick or "*No nickname*"
            after_nick = after.nick or "*No nickname*"

            text = (
                f"📝 **Nickname changed** for {after.mention}\n"
                f"-# {after} • ID: {after.id}\n\n"
                f"**Before:** {before_nick}\n"
                f"**After:** {after_nick}"
            )

            view = _layout(_section_with_avatar(text, after.display_avatar.url))
            await self._send(channel, view)

    async def log_role_create(self, role):
        if not self.mod_cfg.get("log_role_changes"):
            return

        channel = self.get_bot_logs_channel(role.guild)
        if not channel:
            return

        text = (
            f"✅ **Role created:** {role.mention}\n"
            f"**Name:** {role.name} • **ID:** {role.id}\n"
            f"**Color:** {role.color} • **Hoisted:** {'Yes' if role.hoist else 'No'} • **Mentionable:** {'Yes' if role.mentionable else 'No'}"
        )

        view = _layout(_container(_text(text)))
        await self._send(channel, view)

    async def log_role_delete(self, role):
        if not self.mod_cfg.get("log_role_changes"):
            return

        channel = self.get_bot_logs_channel(role.guild)
        if not channel:
            return

        text = (
            f"❌ **Role deleted:** {role.name}\n"
            f"**ID:** {role.id} • **Color:** {role.color}"
        )

        view = _layout(_container(_text(text)))
        await self._send(channel, view)

    async def log_role_update(self, before, after):
        if not self.mod_cfg.get("log_role_changes"):
            return

        channel = self.get_bot_logs_channel(after.guild)
        if not channel:
            return

        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")
        if before.color != after.color:
            changes.append(f"**Color:** {before.color} → {after.color}")
        if before.hoist != after.hoist:
            changes.append(f"**Hoisted:** {before.hoist} → {after.hoist}")
        if before.mentionable != after.mentionable:
            changes.append(f"**Mentionable:** {before.mentionable} → {after.mentionable}")
        if before.permissions != after.permissions:
            changes.append("**Permissions:** Updated")

        if not changes:
            return

        text = f"🔧 **Role updated:** {after.mention} • ID: {after.id}\n\n" + "\n".join(changes)
        view = _layout(_container(_text(text)))
        await self._send(channel, view)

    async def log_channel_create(self, channel):
        if not self.mod_cfg.get("log_channel_changes"):
            return

        bot_logs = self.get_bot_logs_channel(channel.guild)
        if not bot_logs:
            return

        text = (
            f"✅ **Channel created:** {channel.mention}\n"
            f"**Name:** {channel.name} • **ID:** {channel.id} • **Type:** {channel.type}"
        )

        view = _layout(_container(_text(text)))
        await self._send(bot_logs, view)

    async def log_channel_delete(self, channel):
        if not self.mod_cfg.get("log_channel_changes"):
            return

        bot_logs = self.get_bot_logs_channel(channel.guild)
        if not bot_logs:
            return

        text = (
            f"❌ **Channel deleted:** #{channel.name}\n"
            f"**ID:** {channel.id} • **Type:** {channel.type}"
        )

        view = _layout(_container(_text(text)))
        await self._send(bot_logs, view)

    async def log_channel_update(self, before, after):
        if not self.mod_cfg.get("log_channel_changes"):
            return

        channel = self.get_bot_logs_channel(after.guild)
        if not channel:
            return

        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")
        if hasattr(before, 'topic') and hasattr(after, 'topic') and before.topic != after.topic:
            changes.append(f"**Topic:** {before.topic or 'None'} → {after.topic or 'None'}")
        if hasattr(before, 'slowmode_delay') and hasattr(after, 'slowmode_delay') and before.slowmode_delay != after.slowmode_delay:
            changes.append(f"**Slowmode:** {before.slowmode_delay}s → {after.slowmode_delay}s")

        if not changes:
            return

        text = f"🔧 **Channel updated:** {after.mention} • ID: {after.id}\n\n" + "\n".join(changes)
        view = _layout(_container(_text(text)))
        await self._send(channel, view)

    async def log_voice_state_update(self, member, before, after):
        if not self.mod_cfg.get("log_voice_changes"):
            return

        channel = self.get_bot_logs_channel(member.guild)
        if not channel:
            return

        if before.channel is None and after.channel is not None:
            text = f"🔊 **{member.mention} joined** {after.channel.mention}\n-# {member} • ID: {member.id}"
        elif before.channel is not None and after.channel is None:
            text = f"🔇 **{member.mention} left** {before.channel.mention}\n-# {member} • ID: {member.id}"
        elif before.channel != after.channel:
            text = f"🔀 **{member.mention} moved** {before.channel.mention} → {after.channel.mention}\n-# {member} • ID: {member.id}"
        else:
            return

        view = _layout(_section_with_avatar(text, member.display_avatar.url))
        await self._send(channel, view)

    async def log_invite_create(self, invite):
        if not self.mod_cfg.get("log_invite_changes"):
            return

        channel = self.get_bot_logs_channel(invite.guild)
        if not channel:
            return

        max_uses = "Unlimited" if not invite.max_uses else str(invite.max_uses)
        expires = f"{invite.max_age // 3600} hours" if invite.max_age else "Never"

        text = (
            f"🔗 **Invite created:** `{invite.code}`\n"
            f"**Channel:** {invite.channel.mention} • **Max Uses:** {max_uses} • **Expires:** {expires}\n"
            f"-# Code: {invite.code}"
        )

        if invite.inviter:
            text = f"-# {invite.inviter} • ID: {invite.inviter.id}\n" + text
            view = _layout(_section_with_avatar(text, invite.inviter.display_avatar.url))
        else:
            view = _layout(_container(_text(text)))

        await self._send(channel, view)

    async def log_invite_delete(self, invite):
        if not self.mod_cfg.get("log_invite_changes"):
            return

        channel = self.get_bot_logs_channel(invite.guild)
        if not channel:
            return

        channel_name = invite.channel.mention if invite.channel else "Unknown"
        text = f"❌ **Invite deleted:** `{invite.code}`\n**Channel:** {channel_name}"
        view = _layout(_container(_text(text)))
        await self._send(channel, view)

    async def log_ban(self, guild: discord.Guild, user: discord.User,
                      moderator: discord.Member, reason: str,
                      delete_days: int, action_channel: discord.TextChannel) -> Optional[int]:
        channel = self.get_bot_logs_channel(guild)
        text = (
            f"🔨 **User Banned**\n"
            f"{user.mention} was banned from the server.\n"
            f"-# {user} • ID: {user.id}\n\n"
            f"**Moderator:** {moderator.mention}\n"
            f"**Reason:** {reason}\n"
            f"**Messages Deleted:** {delete_days} day{'s' if delete_days != 1 else ''} • **Channel:** {action_channel.mention}"
        )
        view = _layout(_section_with_avatar(text, user.display_avatar.url))
        return await self._send(channel, view)

    async def log_kick(self, guild: discord.Guild, member: discord.Member,
                       moderator: discord.Member, reason: str,
                       action_channel: discord.TextChannel) -> Optional[int]:
        channel = self.get_bot_logs_channel(guild)
        text = (
            f"👢 **Member Kicked**\n"
            f"{member.mention} was kicked from the server.\n"
            f"-# {member} • ID: {member.id}\n\n"
            f"**Moderator:** {moderator.mention}\n"
            f"**Reason:** {reason}\n"
            f"**Channel:** {action_channel.mention}"
        )
        view = _layout(_section_with_avatar(text, member.display_avatar.url))
        return await self._send(channel, view)

    async def log_timeout(self, guild: discord.Guild, member: discord.Member,
                          moderator: discord.Member, reason: str,
                          duration_str: str, action_channel: discord.TextChannel) -> Optional[int]:
        channel = self.get_bot_logs_channel(guild)
        text = (
            f"⏱️ **Member Timed Out**\n"
            f"{member.mention} was timed out.\n"
            f"-# {member} • ID: {member.id}\n\n"
            f"**Moderator:** {moderator.mention}\n"
            f"**Reason:** {reason}\n"
            f"**Duration:** {duration_str} • **Channel:** {action_channel.mention}"
        )
        view = _layout(_section_with_avatar(text, member.display_avatar.url))
        return await self._send(channel, view)

    async def log_mute(self, guild: discord.Guild, member: discord.Member,
                       moderator: discord.Member, reason: str,
                       duration_str: str, action_channel: discord.TextChannel) -> Optional[int]:
        channel = self.get_bot_logs_channel(guild)
        text = (
            f"🔇 **Member Muted**\n"
            f"{member.mention} was muted.\n"
            f"-# {member} • ID: {member.id}\n\n"
            f"**Moderator:** {moderator.mention}\n"
            f"**Reason:** {reason}\n"
            f"**Duration:** {duration_str} • **Channel:** {action_channel.mention}"
        )
        view = _layout(_section_with_avatar(text, member.display_avatar.url))
        return await self._send(channel, view)

    async def log_softban(self, guild: discord.Guild, member: discord.Member,
                          moderator: discord.Member, reason: str,
                          delete_days: int, action_channel: discord.TextChannel) -> Optional[int]:
        channel = self.get_bot_logs_channel(guild)
        text = (
            f"🔨 **Member Softbanned**\n"
            f"{member.mention} was softbanned (messages deleted, can rejoin).\n"
            f"-# {member} • ID: {member.id}\n\n"
            f"**Moderator:** {moderator.mention}\n"
            f"**Reason:** {reason}\n"
            f"**Messages Deleted:** {delete_days} day{'s' if delete_days != 1 else ''} • **Channel:** {action_channel.mention}"
        )
        view = _layout(_section_with_avatar(text, member.display_avatar.url))
        return await self._send(channel, view)

    async def log_purge(self, guild: discord.Guild, moderator: discord.Member,
                        count: int, action_channel: discord.TextChannel,
                        target_user: Optional[discord.Member] = None) -> Optional[int]:
        channel = self.get_bot_logs_channel(guild)
        desc = f"**{count}** message{'s' if count != 1 else ''} deleted "
        desc += f"from {target_user.mention}" if target_user else f"in {action_channel.mention}"
        text = (
            f"🧹 **Messages Purged**\n"
            f"{desc}\n\n"
            f"**Moderator:** {moderator.mention} • **Channel:** {action_channel.mention} • **Amount:** {count}"
        )
        if target_user:
            view = _layout(_section_with_avatar(text, target_user.display_avatar.url))
        else:
            view = _layout(_container(_text(text)))
        return await self._send(channel, view)

    async def log_warn(self, guild: discord.Guild, member: discord.Member,
                       moderator: discord.Member, reason: str,
                       strike_count: int, action_channel: discord.TextChannel) -> Optional[int]:
        channel = self.get_bot_logs_channel(guild)
        text = (
            f"⚠️ **Member Warned**\n"
            f"{member.mention} was warned.\n"
            f"-# {member} • ID: {member.id}\n\n"
            f"**Moderator:** {moderator.mention}\n"
            f"**Reason:** {reason}\n"
            f"**Total Warnings:** {strike_count} • **Channel:** {action_channel.mention}"
        )
        view = _layout(_section_with_avatar(text, member.display_avatar.url))
        return await self._send(channel, view)

    async def log_lock(self, guild: discord.Guild, moderator: discord.Member,
                       reason: str, locked_channel: discord.TextChannel) -> Optional[int]:
        channel = self.get_bot_logs_channel(guild)
        text = (
            f"🔒 **Channel Locked**\n"
            f"{locked_channel.mention} was locked.\n\n"
            f"**Moderator:** {moderator.mention}\n"
            f"**Reason:** {reason} • **Channel:** {locked_channel.mention}"
        )
        view = _layout(_container(_text(text)))
        return await self._send(channel, view)


def setup(bot):

    from mod_core import is_owner

    event_logger = EventLogger(bot)
    bot._logger_event_logger = event_logger

    @bot.listen()
    async def on_message_delete(message):
        if message.guild:
            rehosted_files = None
            pending = getattr(bot, '_pending_rehosted_media', {})
            if message.id in pending:
                rehosted_files = pending.pop(message.id)
            await event_logger.log_message_delete(message, rehosted_files=rehosted_files)

    @bot.listen()
    async def on_message_edit(before, after):
        if before.guild:
            await event_logger.log_message_edit(before, after)

    @bot.listen()
    async def on_bulk_message_delete(messages):
        if messages and messages[0].guild:
            await event_logger.log_bulk_message_delete(messages)

    @bot.listen()
    async def on_member_join(member):
        await event_logger.log_member_join(member)

    @bot.listen()
    async def on_member_remove(member):
        await event_logger.log_member_leave(member)

    @bot.listen()
    async def on_member_update(before, after):
        await event_logger.log_member_update(before, after)

    @bot.listen()
    async def on_guild_role_create(role):
        await event_logger.log_role_create(role)

    @bot.listen()
    async def on_guild_role_delete(role):
        await event_logger.log_role_delete(role)

    @bot.listen()
    async def on_guild_role_update(before, after):
        await event_logger.log_role_update(before, after)

    @bot.listen()
    async def on_guild_channel_create(channel):
        await event_logger.log_channel_create(channel)

    @bot.listen()
    async def on_guild_channel_delete(channel):
        await event_logger.log_channel_delete(channel)

    @bot.listen()
    async def on_guild_channel_update(before, after):
        await event_logger.log_channel_update(before, after)

    @bot.listen()
    async def on_voice_state_update(member, before, after):
        await event_logger.log_voice_state_update(member, before, after)

    @bot.listen()
    async def on_invite_create(invite):
        await event_logger.log_invite_create(invite)

    @bot.listen()
    async def on_invite_delete(invite):
        await event_logger.log_invite_delete(invite)

    @bot.tree.command(name="setjoinlogs", description="[Owner only] Set the channel for join/leave logs")
    @app_commands.describe(channel="Channel to send join/leave logs to")
    async def set_join_logs(interaction: discord.Interaction, channel: discord.TextChannel):
        if not is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to owners.", ephemeral=True)
            return
        event_logger.mod_cfg.set("join_logs_channel_id", channel.id)
        view = _layout(_container(_text(f"✅ **Join Logs Channel Set**\nJoin/leave logs will now be sent to {channel.mention}")))
        await interaction.response.send_message(view=view)
        bot.logger.log(MODULE_NAME, f"Join logs channel set to {channel.name} by {interaction.user}")

    @bot.tree.command(name="setbotlogs", description="[Owner only] Set the channel for bot/moderation logs")
    @app_commands.describe(channel="Channel to send bot/moderation logs to")
    async def set_bot_logs(interaction: discord.Interaction, channel: discord.TextChannel):
        if not is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to owners.", ephemeral=True)
            return
        event_logger.mod_cfg.set("bot_logs_channel_id", channel.id)
        view = _layout(_container(_text(f"✅ **Bot Logs Channel Set**\nBot/moderation logs will now be sent to {channel.mention}")))
        await interaction.response.send_message(view=view)
        bot.logger.log(MODULE_NAME, f"Bot logs channel set to {channel.name} by {interaction.user}")

    @bot.tree.command(name="logconfig", description="[Owner only] View or toggle logging settings")
    async def log_config(interaction: discord.Interaction):
        if not is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to owners.", ephemeral=True)
            return
        join_channel = event_logger.get_join_logs_channel(interaction.guild)
        bot_channel = event_logger.get_bot_logs_channel(interaction.guild)

        cfg = event_logger.mod_cfg
        settings = [
            f"{'✅' if cfg.get('log_message_edits') else '❌'} Message Edits",
            f"{'✅' if cfg.get('log_message_deletes') else '❌'} Message Deletes",
            f"{'✅' if cfg.get('log_member_joins') else '❌'} Member Joins",
            f"{'✅' if cfg.get('log_member_leaves') else '❌'} Member Leaves",
            f"{'✅' if cfg.get('log_bans') else '❌'} Bans/Unbans",
            f"{'✅' if cfg.get('log_role_changes') else '❌'} Role Changes",
            f"{'✅' if cfg.get('log_channel_changes') else '❌'} Channel Changes",
            f"{'✅' if cfg.get('log_voice_changes') else '❌'} Voice Changes",
            f"{'✅' if cfg.get('log_invite_changes') else '❌'} Invite Changes",
            f"{'✅' if cfg.get('log_nickname_changes') else '❌'} Nickname Changes",
        ]

        channels_text = (
            f"## Logging Configuration\n"
            f"**Join Logs:** {join_channel.mention if join_channel else 'Not set'}\n"
            f"**Bot Logs:** {bot_channel.mention if bot_channel else 'Not set'}"
        )
        settings_text = "## Enabled Features\n" + "\n".join(settings)
        footer_text = "-# Use /setjoinlogs and /setbotlogs to configure channels"

        view = _layout(
            _container(_text(channels_text)),
            _sep(),
            _container(_text(settings_text)),
            _sep(),
            _text(footer_text),
        )
        await interaction.response.send_message(view=view, ephemeral=True)

    bot.logger.log(MODULE_NAME, "Logger module setup complete")
