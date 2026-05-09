import asyncio
import discord
import aiohttp
import re
import json
from discord import app_commands

MODULE_NAME = "ARTWORK"

ITUNES_SEARCH = "https://itunes.apple.com/search"
ART_SIZE = 3600


def art_url(raw: str, size: int = ART_SIZE) -> str:
    return re.sub(r"\d+x\d+bb", f"{size}x{size}bb", raw)


async def _itunes(session: aiohttp.ClientSession, term: str, attribute: str, limit: int) -> list[dict]:
    params = {"term": term, "entity": "album", "attribute": attribute, "limit": limit}
    async with session.get(ITUNES_SEARCH, params=params) as resp:
        if resp.status != 200:
            return []
        return json.loads(await resp.text()).get("results", [])


async def search_itunes(artist: str, album: str) -> dict | None:
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        if album:
            artist_results, album_results = await asyncio.gather(
                _itunes(session, artist, "artistTerm", 50),
                _itunes(session, album,  "albumTerm",  50),
            )
            # Intersect: albums that appear in both artist and album searches
            artist_ids = {r["collectionId"] for r in artist_results}
            intersect  = [r for r in album_results if r["collectionId"] in artist_ids]
            if intersect:
                return intersect[0]
            # Fallback: artist results, sorted by how many album words appear in collection name
            if artist_results:
                al = album.casefold()
                artist_results.sort(
                    key=lambda r: -sum(w in r.get("collectionName", "").casefold() for w in al.split())
                )
                return artist_results[0]
            return None
        else:
            results = await _itunes(session, artist, "artistTerm", 10)
            return results[0] if results else None


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
