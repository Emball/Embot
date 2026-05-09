import discord
import aiohttp
import re
import json
from difflib import SequenceMatcher
from discord import app_commands

MODULE_NAME = "ARTWORK"

ITUNES_SEARCH = "https://itunes.apple.com/search"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
ART_SIZE = 3600

def art_url(raw: str, size: int = ART_SIZE) -> str:
    return re.sub(r"\d+x\d+bb", f"{size}x{size}bb", raw)

def _artist_score(query: str, name: str) -> float:
    q = query.casefold()
    a = name.casefold()
    if a.startswith(q):
        return 2.0 + SequenceMatcher(None, q, a).ratio()
    q_words = set(q.split())
    a_words = set(a.split())
    overlap = q_words & a_words
    if overlap:
        return 1.0 + len(overlap) / len(q_words) + SequenceMatcher(None, q, a).ratio() * 0.1
    return SequenceMatcher(None, q, a).ratio()

def _album_score(query: str, name: str) -> float:
    al = query.casefold()
    nm = name.casefold()
    ratio = SequenceMatcher(None, al, nm).ratio()
    al_words = set(al.split())
    nm_words = set(nm.split())
    return ratio + len(al_words & nm_words) * 0.5

async def _resolve_artist(session: aiohttp.ClientSession, query: str) -> dict | None:
    params = {"term": query, "entity": "musicArtist", "limit": 25}
    async with session.get(ITUNES_SEARCH, params=params) as resp:
        if resp.status != 200:
            return None
        results = json.loads(await resp.text()).get("results", [])
    if not results:
        return None

    total = len(results)
    scored = sorted(
        enumerate(results),
        key=lambda ir: -_rank_key(query, ir[0], total, ir[1].get("artistName", "")),
    )
    for _, artist in scored:
        albums = await _artist_albums(session, artist["artistName"], artist["artistId"])
        if albums:
            return artist
    return scored[0][1] if scored else None

def _rank_key(query: str, index: int, total: int, name: str) -> float:
    q = query.casefold()
    a = name.casefold()
    if not a.startswith(q):
        return _artist_score(query, name)
    position_bonus = (1.0 - index / total) * 0.3
    extra_words = max(len(a.split()) - len(q.split()), 0)
    word_bonus = min(extra_words * 0.1, 0.3)
    return 2.0 + position_bonus + word_bonus

async def _artist_albums(session: aiohttp.ClientSession, name: str, artist_id: int) -> list[dict]:
    params = {"id": artist_id, "entity": "album", "limit": 200}
    async with session.get(ITUNES_LOOKUP, params=params) as resp:
        if resp.status != 200:
            return []
        results = json.loads(await resp.text()).get("results", [])
    return [a for a in results if a.get("artistId") == artist_id and a.get("wrapperType") == "collection"]

async def search_itunes(artist: str, album: str) -> dict | None:
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        artist_info = await _resolve_artist(session, artist)
        if not artist_info:
            return None

        artist_name = artist_info["artistName"]
        artist_id = artist_info["artistId"]
        albums = await _artist_albums(session, artist_name, artist_id)
        if not albums:
            return None

        if not album:
            return albums[0]

        best = None
        best_score = -1.0
        for a in albums:
            s = _album_score(album, a.get("collectionName", ""))
            if s > best_score:
                best_score = s
                best = a
        return best

def setup(bot):

    @bot.tree.command(name="artwork", description="Fetch high-resolution album artwork from Apple Music")
    @app_commands.describe(
        artist="Artist name",
        album="Album name (optional)",
    )
    async def artwork_cmd(interaction: discord.Interaction, artist: str, album: str = ""):
        await interaction.response.defer(thinking=True)

        try:
            result = await search_itunes(artist, album)
        except Exception as e:
            bot.logger.error(MODULE_NAME, "iTunes search failed", e)
            await interaction.followup.send("Failed to reach Apple Music. Try again later.")
            return

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
