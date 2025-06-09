# magic_emball.py
import discord
import random
import re
from discord import app_commands

def setup_magic_emball(bot):
    @bot.tree.command(name="magicemball", description="Ask the magic Emball a yes/no question")
    @app_commands.describe(question="Your question for the magic 8-ball")
    async def magic_emball(interaction: discord.Interaction, question: str):
        """Magic Emball with smart logic and regex-based revival detection"""
        responses = [
            "Yeah", "Nah", "Maybe", 
            "Ask again later", "Most likely",
            "Don't count on it", "Nah bruh",
            "LMFAO no", "Huh?",
            "Unalive."
        ]

        # Regex pattern to match obfuscated 'revival'
        revival_pattern = re.compile(
            r"""
            r+[\W_]*         # r or repeated r
            (e|3)+[\W_]*     # e or 3
            (v|\/)+[\W_]*    # v or /
            (i|1|!)+[\W_]*   # i, 1, or !
            (v|\/)+[\W_]*    # v
            (a|4)+[\W_]*     # a or 4
            (l|1|!)+         # l, 1, or !
            """,
            re.IGNORECASE | re.VERBOSE
        )

        lower_q = question.lower()

        if "revival" in lower_q or revival_pattern.search(question):
            response = "Revival sucks."

        elif re.search(r"\bshould i\b", lower_q):
            response = random.choice([
                "Only if you're ready for the consequences.",
                "You probably shouldn't, but who am I to stop you?",
                "Yes, but act like you didn’t hear it from me.",
            ])

        elif re.search(r"\bwill i\b", lower_q):
            response = random.choice([
                "Eventually, maybe.",
                "Signs point to 'meh'.",
                "Only if you believe hard enough.",
            ])

        elif re.search(r"\bis .*real\?", lower_q):
            response = "Nothing is real. Especially Emball."

        elif "love" in lower_q:
            response = random.choice([
                "Love is a scam.",
                "Try touching grass first.",
                "Emball ships it.",
            ])

        elif lower_q.startswith("when") or " when " in lower_q:
            response = random.choice([
                "When the stars align.",
                "Soon.",
                "In approximately 3 to 5 business eternities.",
                "Right after you stop asking.",
                "When hell freezes over",
            ])

        else:
            response = random.choice(responses)

        embed = discord.Embed(
            title="🎱 Magic Emball",
            color=discord.Color.purple()
        )
        embed.add_field(name="Question", value=question, inline=False)
        embed.add_field(name="Answer", value=response, inline=False)
        embed.set_footer(text="Ask wisely.")

        await interaction.response.send_message(embed=embed)