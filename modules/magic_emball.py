import discord
import random
import re
from discord import app_commands

MODULE_NAME = "MAGIC_EMBALL"

RESPONSES = [
    "Yeah", "Nah", "Maybe",
    "Ask again later", "Most likely",
    "Don't count on it", "Nah bruh",
    "LMFAO no", "Huh?",
    "Unalive."
]

SPECIAL_RESPONSES = {
    'revival': {
        'pattern': re.compile(
            r"""r+[\W_]*         # r or repeated r
            (e|3)+[\W_]*     # e or 3
            (v|\/)+[\W_]*    # v or /
            (i|1|!)+[\W_]*   # i, 1, or !
            (v|\/)+[\W_]*    # v
            (a|4)+[\W_]*     # a or 4
            (l|1|!)+
            """,
            re.IGNORECASE | re.VERBOSE
        ),
        'response': "Revival sucks."
    },
    'should_i': {
        'pattern': re.compile(r"\bshould i\b", re.IGNORECASE),
        'responses': [
            "Only if you're ready for the consequences.",
            "You probably shouldn't, but who am I to stop you?",
            "Yes, but act like you didn't hear it from me.",
        ]
    },
    'will_i': {
        'pattern': re.compile(r"\bwill i\b", re.IGNORECASE),
        'responses': [
            "Eventually, maybe.",
            "Signs point to 'meh'.",
            "Only if you believe hard enough.",
        ]
    },
    'is_real': {
        'pattern': re.compile(r"\bis .*real\?", re.IGNORECASE),
        'response': "Nothing is real. Especially Emball."
    },
    'love': {
        'pattern': re.compile(r"\blove\b", re.IGNORECASE),
        'responses': [
            "Love is a scam.",
            "Try touching grass first.",
            "Emball ships it.",
        ]
    },
    'when': {
        'pattern': re.compile(r"^when| when ", re.IGNORECASE),
        'responses': [
            "When the stars align.",
            "Soon.",
            "In approximately 3 to 5 business eternities.",
            "Right after you stop asking.",
            "When hell freezes over",
        ]
    }
}

def setup(bot):
    bot.logger.log(MODULE_NAME, "Setting up magic emball command")

    import time as _time
    _user_last_used: dict = {}
    _COOLDOWN_SECONDS = 5
    _cleanup_counter = 0
    _CLEANUP_INTERVAL = 100
    _CLEANUP_TTL = 3600

    @bot.tree.command(name="magicemball", description="Ask the magic Emball a yes/no question")
    @app_commands.describe(question="Your question for the magic 8-ball")
    async def magic_emball(interaction: discord.Interaction, question: str):
        nonlocal _cleanup_counter
        try:
            _cleanup_counter += 1
            if _cleanup_counter >= _CLEANUP_INTERVAL:
                _cleanup_counter = 0
                now_ts = _time.monotonic()
                stale = [uid for uid, ts in _user_last_used.items() if now_ts - ts > _CLEANUP_TTL]
                for uid in stale:
                    del _user_last_used[uid]

            uid = interaction.user.id
            now = _time.monotonic()
            last = _user_last_used.get(uid, 0)
            if now - last < _COOLDOWN_SECONDS:
                remaining = _COOLDOWN_SECONDS - (now - last)
                await interaction.response.send_message(
                    f"⏳ Slow down! Try again in {remaining:.1f}s.", ephemeral=True)
                return
            _user_last_used[uid] = now

            bot.logger.log(MODULE_NAME, f"Question from {interaction.user}: {question}")

            response = None

            for category, data in SPECIAL_RESPONSES.items():
                if data['pattern'].search(question):
                    bot.logger.log(MODULE_NAME, f"Matched special pattern: {category}")
                    if 'response' in data:
                        response = data['response']
                    else:
                        response = random.choice(data['responses'])
                    break

            if not response:
                response = random.choice(RESPONSES)
                bot.logger.log(MODULE_NAME, "Using random response")

            embed = discord.Embed(
                title="Magic Emball",
                color=discord.Color.purple()
            )
            embed.add_field(name="Question", value=question, inline=False)
            embed.add_field(name="Answer", value=response, inline=False)
            embed.set_footer(text="Ask wisely.")

            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, "Sent magic emball response")

        except Exception as e:
            bot.logger.error(MODULE_NAME, "Magic emball command failed", e)
            try:
                await interaction.response.send_message(
                    "The magic is broken... try again later.",
                    ephemeral=True
                )
            except:
                pass

    bot.logger.log(MODULE_NAME, "Magic emball command registered successfully")
