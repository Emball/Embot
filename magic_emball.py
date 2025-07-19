# magic_emball.py
import discord
import random
import re
import logging
from discord import app_commands

logger = logging.getLogger('magic_emball')

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
            (l|1|!)+         # l, 1, or !
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

def setup(bot):  # Changed from setup_magic_emball
    """Setup the magic emball command"""
    logger.info("Initializing magic emball command")

    @bot.tree.command(name="magicemball", description="Ask the magic Emball a yes/no question")
    @app_commands.describe(question="Your question for the magic 8-ball")
    async def magic_emball(interaction: discord.Interaction, question: str):
        """Magic Emball with smart logic and regex-based detection"""
        try:
            logger.info(f"Magic emball question from {interaction.user}: {question}")
            
            lower_q = question.lower()
            response = None

            # Check for special patterns
            for category, data in SPECIAL_RESPONSES.items():
                if data['pattern'].search(question):
                    logger.debug(f"Matched special pattern: {category}")
                    if 'response' in data:
                        response = data['response']
                    else:
                        response = random.choice(data['responses'])
                    break

            # Default random response if no special pattern matched
            if not response:
                response = random.choice(RESPONSES)
                logger.debug("Using random response")

            embed = discord.Embed(
                title="🎱 Magic Emball",
                color=discord.Color.purple()
            )
            embed.add_field(name="Question", value=question, inline=False)
            embed.add_field(name="Answer", value=response, inline=False)
            embed.set_footer(text="Ask wisely.")

            await interaction.response.send_message(embed=embed)
            logger.info("Sent magic emball response")

        except Exception as e:
            logger.error(f"Magic emball error: {e}")
            await interaction.response.send_message(
                "❌ The magic is broken... try again later.",
                ephemeral=True
            )

    logger.info("Magic emball command registered")