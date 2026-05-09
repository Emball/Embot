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


def normalize(text: str) -> str:
    t = _STRIP.sub("", text)
    return _WS.sub(" ", t).strip().casefold()


def art_url(raw: str, size: int = ART_SIZE) -> str:
    return re.sub(r"\d+x\d+bb", f"{size}x{size}bb", raw)


async def _search(params: dict) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(ITUNES_SEARCH, params=params, headers=headers) as resp:
            if resp.status != 200:
                return []
            return json.loads(await resp.text()).get("results", [])


async def search_itunes(artist: str, album: str) -> list[dict]:
    # Primary: search by artistTerm only so iTunes filters to that artist's catalogue
    results = await _search({
        "term": artist,
        "entity": "album",
        "attribute": "artistTerm",
        "limit": 25,
    })
    # If album specified, filter down; otherwise return as-is
    if album and results:
        a = normalize(album)
        results.sort(
            key=lambda r: difflib.SequenceMatcher(None, a, normalize(r.get("collectionName", ""))).ratio(),
            reverse=True,
        )
    return results


def best_match(artist: str, album: str, results: list[dict]) -> dict | None:
    if not results:
        return None
    # Top result is already the best artist+album match from search_itunes sort
    r = results[0]
    # Sanity: artist name should at least partially match
    artist_ratio = difflib.SequenceMatcher(None, normalize(artist), normalize(r.get("artistName", ""))).ratio()
    if artist_ratio < 0.3:
        return None
    return r


def setup(bot):

    @bot.tree.command(name="artwork", description="Fetch high-resolution album artwork from Apple Music")
    @app_commands.describe(
        artist="Artist name",
        album="Album name (optional — returns top album if omitted)",
    )
    async def artwork_cmd(interaction: discord.Interaction, artist: str, album: str = ""):
        await interaction.response.defer(thinking=True)

        try:
            results = await search_itunes(artist, album)
        except Exception as e:
            bot.logger.error(MODULE_NAME, "iTunes search failed", e)
            await interaction.followup.send("Failed to reach Apple Music. Try again later.")
            return

        result = best_match(artist, album, results)
        if not result:
            label = f"{artist} — {album}" if album else artist
            await interaction.followup.send(f"No results found for **{label}**.")
            return

        raw_art = result.get("artworkUrl100", "")
        if not raw_art:
            await interaction.followup.send("Found the album but it has no artwork.")
            return

        url = art_url(raw_art)
        r_artist = result.get("artistName", "Unknown Artist")
        r_album  = result.get("collectionName", "Unknown Album")
        year     = result.get("releaseDate", "")[:4]

        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f"Found **{r_album}** by **{r_artist}** but couldn't download the artwork.")
                    return
                data = await resp.read()

        import io
        ext = url.split(".")[-1].split("?")[0] or "jpg"
        filename = f"{r_artist} - {r_album}.{ext}".replace("/", "-")
        await interaction.followup.send(
            f"**{r_artist}** — **{r_album}** ({year})",
            file=discord.File(io.BytesIO(data), filename=filename)
        )
        bot.logger.log(MODULE_NAME, f"{interaction.user} fetched artwork: {r_artist} — {r_album}")

    bot.logger.log(MODULE_NAME, "Artwork module loaded")
