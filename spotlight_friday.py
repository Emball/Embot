# spotlight_friday.py
from discord.ext import tasks
from datetime import datetime, timedelta
import discord

@tasks.loop(hours=1)
async def spotlight_friday(bot):
    now = datetime.utcnow()
    if now.weekday() == 4 and now.hour == 12:
        best_msg = None
        best_score = 0
        
        cutoff = now - timedelta(days=7)
        for msg_id in list(bot.VOTE_TRACKER.keys()):
            if bot.VOTE_TRACKER[msg_id] < cutoff:
                del bot.VOTE_TRACKER[msg_id]

        for msg_id in bot.VOTE_TRACKER:
            try:
                msg = await bot.project_channel.fetch_message(msg_id)
                for reaction in msg.reactions:
                    if str(reaction.emoji) == "🔥" and reaction.count > best_score:
                        best_score = reaction.count
                        best_msg = msg
                        break
            except (discord.NotFound, discord.Forbidden):
                continue

        if best_msg and bot.announcements_channel:
            embed = discord.Embed(
                title="🔥 Spotlight Friday!",
                description=f"**{best_msg.author.display_name}'s** project got the most 🔥 this week!\n\n{best_msg.content}",
                color=discord.Color.orange()
            )
            embed.set_footer(text="Vote in #projects and #artwork to be featured!")
            if best_msg.author.avatar:
                embed.set_thumbnail(url=best_msg.author.avatar.url)
            if best_msg.attachments:
                embed.set_image(url=best_msg.attachments[0].url)
            
            try:
                await bot.announcements_channel.send(embed=embed)
            except discord.Forbidden:
                print("Missing permissions to send messages in announcements")

        bot.VOTE_TRACKER.clear()