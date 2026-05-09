import discord
import aiohttp
import difflib
import re
import json
from discord import app_commands

MODULE_NAME = "ARTWORK"

ITUNES_SEARCH = "https://itunes.apple.com/search"
ART_SIZE = 3600

_STRIP = re.compile(r"[^\w\s]")
_WS    = re.compile(r"\s+")
_STOP  = {"the", "a", "an", "of", "in", "and", "feat", "ft", "with", "by", "da"}


def normalize(text: str) -> str:
    t = _STRIP.sub("", text)
    return _WS.sub(" ", t).strip().casefold()


def tokenize(text: str) -> set[str]:
    return {w for w in normalize(text).split() if w not in _STOP}


def art_url(raw: str, size: int = ART_SIZE) -> str:
    return re.sub(r"\d+x\d+bb", f"{size}x{size}bb", raw)


async def search_itunes(query: str, limit: int = 25) -> list[dict]:
    params = {"term": query, "entity": "album", "limit": limit}
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(ITUNES_SEARCH, params=params) as resp:
            if resp.status != 200:
                return []
            data = json.loads(await resp.text())
            return data.get("results", [])


def score_result(q_tokens: set[str], q_norm: str, r: dict) -> float:
    artist_tokens = tokenize(r.get("artistName", ""))
    album_tokens  = tokenize(r.get("collectionName", ""))
    full_norm     = normalize(f"{r.get('artistName', '')} {r.get('collectionName', '')}")

    artist_overlap = len(q_tokens & artist_tokens)
    album_overlap  = len(q_tokens & album_tokens)
    ratio          = difflib.SequenceMatcher(None, q_norm, full_norm).ratio()

    # Artist match weighted 3x over album — "royce rock city" should
    # hit Royce da 5'9 (artist) before Riot - Rock City (album title match)
    return artist_overlap * 3 + album_overlap * 1.5 + ratio


def best_match(query: str, results: list[dict]) -> dict | None:
    if not results:
        return None

    q_tokens = tokenize(query)
    q_norm   = normalize(query)

    scored = [(score_result(q_tokens, q_norm, r), r) for r in results]
    scored.sort(key=lambda x: x[0], reverse=True)

    best_score, best = scored[0]
    if best_score < 0.5:
        return None
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
        album  = result.get("collectionName", "Unknown Album")
        year   = result.get("releaseDate", "")[:4]

        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f"Found **{album}** by **{artist}** but couldn't download the artwork.")
                    return
                data = await resp.read()

        import io
        ext = url.split(".")[-1].split("?")[0] or "jpg"
        filename = f"{artist} - {album}.{ext}".replace("/", "-")
        await interaction.followup.send(
            f"**{artist}** — **{album}** ({year})",
            file=discord.File(io.BytesIO(data), filename=filename)
        )
        bot.logger.log(MODULE_NAME, f"{interaction.user} fetched artwork: {artist} — {album}")

    bot.logger.log(MODULE_NAME, "Artwork module loaded")
