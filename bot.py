"""Bot Discord : publie tout seul les jeux gratuits (Epic, Steam, GOG...) et les actus jeux vidéo.

Lancement :  python bot.py
Une seule vérification puis arrêt (utilisé par GitHub Actions) :  python bot.py --once
Test sans Discord (affiche ce qui serait publié) :  python bot.py --test
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import tasks
from dotenv import load_dotenv

import sources

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%d/%m %H:%M:%S")
log = logging.getLogger("bot")
# Le bot n'utilise pas le vocal : on masque l'avertissement "PyNaCl is not installed"
logging.getLogger("discord.client").addFilter(lambda record: "voice will NOT be supported" not in record.getMessage())


def env_int(name: str, default: int = 0) -> int:
    value = (os.getenv(name) or "").strip()
    return int(value) if value.isdigit() else default


def env_list(name: str, default: str) -> list[str]:
    return [v.strip() for v in (os.getenv(name) or default).split(",") if v.strip()]


TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
GAMES_CHANNEL_ID = env_int("GAMES_CHANNEL_ID")
UPCOMING_CHANNEL_ID = env_int("UPCOMING_CHANNEL_ID")  # vide = les "bientôt gratuits" vont dans GAMES_CHANNEL_ID
NEWS_CHANNEL_ID = env_int("NEWS_CHANNEL_ID")
PING_ROLE_ID = env_int("PING_ROLE_ID")
CHECK_INTERVAL_MINUTES = max(env_int("CHECK_INTERVAL_MINUTES", 40), 5)
PLATFORMS = env_list("PLATFORMS", "epic-games-store,steam,gog,itchio,ubisoft,origin,battlenet,drm-free")
SHOW_UPCOMING = (os.getenv("SHOW_UPCOMING") or "true").strip().lower() in {"1", "true", "oui", "yes"}
NEWS_FEEDS = env_list("NEWS_FEEDS", "https://www.jeuxvideo.com/rss/rss.xml,https://www.actugaming.net/feed/")
NEWS_FIRST_RUN_LIMIT = 3  # 1re lecture d'un flux : on ne publie que les 3 derniers articles (pas tout l'historique)

WIKIMEDIA = "https://upload.wikimedia.org/wikipedia/commons/thumb"


def site_logo(domain: str) -> str:
    """Logo 128 px d'un site web (service d'icônes de Google)."""
    return f"https://t3.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https://{domain}&size=128"


# (mots-clés trouvés dans le nom de la plateforme, couleur du message, logo affiché en haut à droite)
PLATFORM_STYLES = [
    (("epic",), 0x0078F2, f"{WIKIMEDIA}/3/31/Epic_Games_logo.svg/250px-Epic_Games_logo.svg.png"),
    (("steam",), 0x66C0F4, f"{WIKIMEDIA}/8/83/Steam_icon_logo.svg/250px-Steam_icon_logo.svg.png"),
    (("gog",), 0x86328A, "https://www.gog.com/apple-touch-icon.png"),
    (("itch",), 0xFA5C5C, "https://static.itch.io/images/app-icon.png"),
    (("ubisoft",), 0x0070FF, f"{WIKIMEDIA}/f/fd/Ubisoft2017.png/250px-Ubisoft2017.png"),
    (("origin", "ea app", "electronic arts"), 0xF56C2D, f"{WIKIMEDIA}/0/0d/Electronic-Arts-Logo.svg/250px-Electronic-Arts-Logo.svg.png"),
    (("battle",), 0x00AEFF, site_logo("battle.net")),
]
DEFAULT_STYLE = (0x57F287, None)


class State:
    """Mémorise ce qui a déjà été publié (posted.json) pour ne jamais reposter, même après un redémarrage."""

    MAX_KEYS = 5000

    def __init__(self, path: Path):
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        self.keys: list[str] = data.get("posted", [])
        self.feeds: set[str] = set(data.get("feeds", []))
        self._seen = set(self.keys)

    def __contains__(self, key: str) -> bool:
        return key in self._seen

    def add(self, *keys: str) -> None:
        new = [k for k in dict.fromkeys(keys) if k not in self._seen]
        self.keys = (self.keys + new)[-self.MAX_KEYS:]
        self._seen = set(self.keys)
        self.save()

    def mark_feed(self, feed_url: str, keys: list[str]) -> None:
        self.feeds.add(feed_url)
        self.add(*keys)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        payload = {"posted": self.keys, "feeds": sorted(self.feeds)}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


def discord_time(dt, style: str) -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"  # Discord affiche la date dans le fuseau de chaque membre


def game_message(game: sources.FreeGame) -> dict:
    name = game.platform.lower()
    color, logo = next(
        ((c, l) for keywords, c, l in PLATFORM_STYLES if any(k in name for k in keywords)), DEFAULT_STYLE
    )
    embed = discord.Embed(
        title=sources.shorten(game.title, 256),
        url=game.url,
        description=game.description or None,
        color=color,
    )
    embed.set_author(name=f"{'🔜 Bientôt gratuit' if game.upcoming else '🎁 Jeu gratuit'} • {game.platform}")
    if game.worth:
        embed.add_field(name="Prix normal", value=f"~~{game.worth}~~ → **Gratuit**")
    if game.upcoming and game.start:
        embed.add_field(name="Gratuit à partir du", value=f"{discord_time(game.start, 'f')}\n{discord_time(game.start, 'R')}")
    if game.end:
        embed.add_field(name="Fin de l'offre", value=f"{discord_time(game.end, 'f')}\n{discord_time(game.end, 'R')}")
    if logo:
        embed.set_thumbnail(url=logo)  # logo de la plateforme, en haut à droite
    if game.image:
        embed.set_image(url=game.image)  # grande image du jeu

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Voir sur le store" if game.upcoming else "Récupérer le jeu", url=game.url, emoji="🔗"))
    content = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID and not game.upcoming else None
    return {"content": content, "embed": embed, "view": view}


def news_message(item: sources.NewsItem) -> dict:
    embed = discord.Embed(
        title=item.title,
        url=item.url,
        description=item.summary or None,
        color=0x5865F2,
        timestamp=item.published,
    )
    embed.set_author(name=f"📰 {item.source}", icon_url=site_logo(urlparse(item.url).netloc))
    if item.image:
        embed.set_image(url=item.image)
    return {"embed": embed}


class FreeGamesBot(discord.Client):
    def __init__(self, once: bool = False):
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
        )
        self.state = State(BASE_DIR / "posted.json")
        self.session: aiohttp.ClientSession | None = None
        self.once = once
        self.failed = False
        self._once_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(headers=sources.HEADERS, timeout=sources.TIMEOUT)
        if self.once:
            self._once_task = asyncio.create_task(self.run_once())
        else:
            self.check_loop.change_interval(minutes=CHECK_INTERVAL_MINUTES)
            self.check_loop.start()

    async def close(self) -> None:
        self.check_loop.cancel()
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        if not self.once:
            log.info("Connecté en tant que %s. Vérification automatique toutes les %d min.", self.user, CHECK_INTERVAL_MINUTES)

    async def run_once(self) -> None:
        """Mode GitHub Actions : une seule vérification, puis déconnexion."""
        await self.wait_until_ready()
        log.info("Connecté en tant que %s. Vérification unique.", self.user)
        try:
            await self.post_free_games()
            await self.post_news()
        except Exception:
            log.exception("Erreur pendant la vérification")
            self.failed = True
        finally:
            await self.close()

    @tasks.loop(minutes=40)
    async def check_loop(self) -> None:
        try:
            await self.post_free_games()
            await self.post_news()
        except Exception:
            log.exception("Erreur pendant la vérification (nouvel essai au prochain tour)")

    @check_loop.before_loop
    async def before_check_loop(self) -> None:
        await self.wait_until_ready()

    async def get_channel_or_log(self, channel_id: int, label: str):
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException as error:
                log.error("Salon %s introuvable ou inaccessible (id %s) : %s", label, channel_id, error)
        return channel

    async def send(self, channel, key: str, message: dict) -> bool:
        try:
            await channel.send(**message)
        except discord.HTTPException as error:
            log.error("Envoi impossible dans #%s : %s", channel, error)
            return False
        self.state.add(key)
        return True

    async def post_free_games(self) -> None:
        if not (GAMES_CHANNEL_ID or UPCOMING_CHANNEL_ID):
            return
        games = await sources.collect_free_games(self.session, PLATFORMS, SHOW_UPCOMING)
        available = [game for game in games if not game.upcoming]
        upcoming = [game for game in games if game.upcoming]
        if UPCOMING_CHANNEL_ID:
            # Clé propre au salon dédié : les jeux déjà annoncés dans le salon principal y sont aussi publiés
            upcoming = [dataclasses.replace(game, key=f"{game.key}@{UPCOMING_CHANNEL_ID}") for game in upcoming]
        await self.post_games(GAMES_CHANNEL_ID, "des jeux disponibles", available)
        await self.post_games(UPCOMING_CHANNEL_ID or GAMES_CHANNEL_ID, "des jeux bientôt gratuits", upcoming)

    async def post_games(self, channel_id: int, label: str, games: list[sources.FreeGame]) -> None:
        if not channel_id:
            return
        new_games = [game for game in games if game.key not in self.state]
        if not new_games:
            log.info("Salon %s : rien de nouveau.", label)
            return
        channel = await self.get_channel_or_log(channel_id, label)
        if channel is None:
            return
        sent = 0
        for game in new_games:
            sent += await self.send(channel, game.key, game_message(game))
        log.info("Salon %s : %d nouvelle(s) offre(s) publiée(s).", label, sent)

    async def post_news(self) -> None:
        if not NEWS_CHANNEL_ID or not NEWS_FEEDS:
            return
        channel = None
        sent = 0
        for feed_url, items in (await sources.collect_news(self.session, NEWS_FEEDS)).items():
            if feed_url not in self.state.feeds:
                self.state.mark_feed(feed_url, [item.key for item in items[NEWS_FIRST_RUN_LIMIT:]])
                items = items[:NEWS_FIRST_RUN_LIMIT]
            new_items = [item for item in items if item.key not in self.state]
            if not new_items:
                continue
            channel = channel or await self.get_channel_or_log(NEWS_CHANNEL_ID, "des actus")
            if channel is None:
                return
            for item in reversed(new_items):  # du plus ancien au plus récent
                sent += await self.send(channel, item.key, news_message(item))
        log.info("Actus : %d nouvel(s) article(s) publié(s).", sent)


async def dry_run() -> None:
    """Affiche ce que le bot publierait, sans se connecter à Discord."""
    async with aiohttp.ClientSession(headers=sources.HEADERS, timeout=sources.TIMEOUT) as session:
        games = await sources.collect_free_games(session, PLATFORMS, SHOW_UPCOMING)
        print(f"\n=== {len(games)} jeu(x) gratuit(s) trouvé(s) ===")
        for game in games:
            game_message(game)["embed"].to_dict()  # vérifie que le message Discord se construit bien
            status = "BIENTÔT" if game.upcoming else "GRATUIT"
            end = game.end.strftime("%d/%m/%Y %H:%M") if game.end else "?"
            print(f"[{status}] [{game.platform}] {game.title} | prix: {game.worth or '?'} | fin: {end}\n    {game.url}")

        for feed_url, items in (await sources.collect_news(session, NEWS_FEEDS)).items():
            print(f"\n=== Actus : {feed_url} ({len(items)} articles) ===")
            for item in items[:5]:
                news_message(item)["embed"].to_dict()
                print(f"- {item.title}\n    {item.url}")


def main() -> None:
    if "--test" in sys.argv:
        asyncio.run(dry_run())
        return
    if not TOKEN:
        sys.exit("DISCORD_TOKEN est vide : ouvre le fichier .env avec le Bloc-notes et colle le token de ton bot apres DISCORD_TOKEN=")
    if not (GAMES_CHANNEL_ID or UPCOMING_CHANNEL_ID or NEWS_CHANNEL_ID):
        sys.exit("GAMES_CHANNEL_ID est vide : ouvre le fichier .env et colle l'identifiant du salon apres GAMES_CHANNEL_ID=")
    client = FreeGamesBot(once="--once" in sys.argv)
    client.run(TOKEN, log_handler=None)
    if client.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
