import discord
import aiohttp
import difflib
import re
from discord import app_commands

MODULE_NAME = "ARTWORK"

ITUNES_SEARCH = "https://itunes.apple.com/search"
ART_SIZE = 3000


def normalize(text: str) -> str:
    t = re.sub(r'[^\\w\\s]', '', text)
    t = re.sub(r'\\s+', '', t).strip()
    return t.casefold()


def art_url(raw: str, size: int = ART_SIZE) -> str:
    return re.sub(r'\\d+x\\d+bb', f'{size}x{size}bb', raw)


async def search_itunes(query: str, limit: int = 10) -> list[dict]:
    params = {"term": query, "entity": "album", "limit": limit}
    async with aiohttp.ClientSession() as session:
        async with session.get(ITUNES_SEARCH, params=params) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("results", [])


def best_match(query: str, results: list[dict]) -> dict | None:
    if not results:
        return None

    q = normalize(query)

    # Build candidate strings: "artistname albumname"
    keys = [normalize(f"{r.get('artistName','')} {r.get('collectionName','')}") for r in results]

    matches = difflib.get_close_matches(q, keys, n=1, cutoff=0.3)
    if matches:
        return results[keys.index(matches[0])]

    # Fallback: score by word overlap
    q_words = set(q.split()) if ' ' in q else set(q)
    best, best_score = None, -1
    for i, k in enumerate(keys):
        k_words = set(k.split()) if ' ' in k else set(k)
        score = len(q_words & k_words)
        if score > best_score:
            best_score, best = score, results[i]
    return best


def setup(bot):

    @bot.tree.command(name="artwork", description="Fetch high-resolution album artwork from Apple Music")
    @app_commands.describe(query="Artist and album name, e.g. 'Eminem Marshall Mathers LP 2'")
    async def artwork_cmd(interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)

        try:
            results = await search_itunes(query)
        except Exception as e:
            bot.logger.error(MODULE_NAME, "iTunes search failed", e)
            await interaction.followup.send("Failed to reach Apple Music. Try again later.")
            return

        result = best_match(query, results)
        if not result:
            await interaction.followup.send(f"No results found for **{query}**.")
            return

        raw_art = result.get("artworkUrl100", "")
        if not raw_art:
            await interaction.followup.send("Found the album but it has no artwork.")
            return

        url = art_url(raw_art)
        artist = result.get("artistName", "Unknown Artist")
        album = result.get("collectionName", "Unknown Album")
        year = result.get("releaseDate", "")[:4]

        embed = discord.Embed(title=album, description=f"{artist} • {year}", color=0x000000)
        embed.set_image(url=url)
        embed.set_footer(text="Apple Music • 3000×3000")

        await interaction.followup.send(embed=embed)
        bot.logger.log(MODULE_NAME, f"{interaction.user} fetched artwork: {artist} — {album}")

    bot.logger.log(MODULE_NAME, "Artwork module loaded")
