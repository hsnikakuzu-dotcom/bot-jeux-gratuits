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
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import sources
import steam

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


def env_bool(name: str, default: bool) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    return value in {"1", "true", "oui", "yes"} if value else default


TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
GAMES_CHANNEL_ID = env_int("GAMES_CHANNEL_ID")
UPCOMING_CHANNEL_ID = env_int("UPCOMING_CHANNEL_ID")  # vide = les "bientôt gratuits" vont dans GAMES_CHANNEL_ID
NEWS_CHANNEL_ID = env_int("NEWS_CHANNEL_ID")
PING_ROLE_ID = env_int("PING_ROLE_ID")
CHECK_INTERVAL_MINUTES = max(env_int("CHECK_INTERVAL_MINUTES", 40), 5)
PLATFORMS = env_list("PLATFORMS", "epic-games-store,steam,gog,itchio,ubisoft,origin,battlenet,drm-free,prime-gaming")
SHOW_UPCOMING = env_bool("SHOW_UPCOMING", True)
COMMUNITY_SOURCES = env_list("COMMUNITY_SOURCES", "dealabs,ggdeals,reddit")
NEWS_FEEDS = env_list("NEWS_FEEDS", "https://www.jeuxvideo.com/rss/rss.xml,https://www.actugaming.net/feed/")
NEWS_FIRST_RUN_LIMIT = 3  # 1re lecture d'un flux : on ne publie que les 3 derniers articles (pas tout l'historique)

# Notoriété : le bot mesure chaque jeu au nombre d'avis Steam pour séparer les gros titres du tout-venant.
STEAM_LOOKUP = env_bool("STEAM_LOOKUP", True)
MIN_REVIEWS = env_int("MIN_REVIEWS")        # 0 = tout publier ; 300 = seulement les jeux un peu connus
PING_MIN_REVIEWS = env_int("PING_MIN_REVIEWS")  # 0 = mentionner le rôle pour tout ; 3000 = que les gros jeux

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
    (("prime", "amazon", "luna"), 0x00A8E1, site_logo("gaming.amazon.com")),
    (("playstation", "ps4", "ps5"), 0x0070D1, site_logo("playstation.com")),
    (("xbox",), 0x107C10, site_logo("xbox.com")),
    (("nintendo", "switch"), 0xE60012, site_logo("nintendo.com")),
    (("android", "google play"), 0x3DDC84, site_logo("play.google.com")),
    (("ios", "app store"), 0x555555, site_logo("apps.apple.com")),
]
DEFAULT_STYLE = (0x57F287, None)


class State:
    """Mémorise ce qui a déjà été publié (posted.json) pour ne jamais reposter, même après un redémarrage."""

    MAX_KEYS = 5000
    NAME_MEMORY = 30 * 24 * 3600  # un jeu déjà annoncé n'est plus republié par un autre site pendant 30 jours

    def __init__(self, path: Path):
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        self.keys: list[str] = data.get("posted", [])
        self.feeds: set[str] = set(data.get("feeds", []))
        self.names: dict[str, float] = data.get("names", {})
        self._seen = set(self.keys)

    def recent_names(self) -> set[str]:
        cutoff = time.time() - self.NAME_MEMORY
        return {name for name, posted_at in self.names.items() if posted_at >= cutoff}

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
        recent = self.recent_names()
        self.names = {name: posted_at for name, posted_at in self.names.items() if name in recent}
        payload = {"posted": self.keys, "feeds": sorted(self.feeds), "names": self.names}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


def is_duplicate(game: sources.FreeGame, known: set[str]) -> bool:
    """Le jeu a-t-il déjà été annoncé ailleurs ?

    Les titres des sites communautaires sont bavards (« Retro-inspired FPS Deadshot is free… ») :
    il ne suffit pas de comparer les noms, il faut chercher un nom déjà connu à l'intérieur."""
    if game.name and game.name in known:
        return True
    haystack = f" {game.name or sources.game_name(f'{game.title} {game.description}') or ''} "
    return any(len(name) >= 5 and f" {name} " in haystack for name in known)


def discord_time(dt, style: str) -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"  # Discord affiche la date dans le fuseau de chaque membre


def game_header(game: sources.FreeGame) -> str:
    """Première ligne du message : les jeux connus le disent haut et fort."""
    if game.upcoming:
        return "🔜 Bientôt gratuit"
    emoji, _ = steam.tier_of(game.steam)
    return f"{emoji} Jeu gratuit à ne pas rater" if emoji in ("🏆", "⭐") else "🎁 Jeu gratuit"


def game_message(game: sources.FreeGame) -> dict:
    name = game.platform.lower()
    color, logo = next(
        ((c, l) for keywords, c, l in PLATFORM_STYLES if any(k in name for k in keywords)), DEFAULT_STYLE
    )
    if steam.tier_of(game.steam)[0] == "🏆":
        color = 0xFFC107  # un incontournable se repère d'un coup d'œil dans le salon
    embed = discord.Embed(
        title=sources.shorten(game.title, 256),
        url=game.url,
        description=game.description or None,
        color=color,
    )
    via = f" • via {game.via}" if game.via else ""
    embed.set_author(name=f"{game_header(game)} • {game.platform}{via}")
    if game.steam:
        embed.add_field(name="Notoriété", value=game.steam.summary(), inline=False)
    # Le prix Steam est plus fiable que celui annoncé par IndieGala & co, souvent gonflé
    worth = (game.steam.price if game.steam else None) or game.worth
    if worth:
        embed.add_field(name="Prix normal", value=f"~~{worth}~~ → **Gratuit**")
    if game.upcoming and game.start:
        embed.add_field(name="Gratuit à partir du", value=f"{discord_time(game.start, 'f')}\n{discord_time(game.start, 'R')}")
    if game.end:
        embed.add_field(name="Fin de l'offre", value=f"{discord_time(game.end, 'f')}\n{discord_time(game.end, 'R')}")
    if logo:
        embed.set_thumbnail(url=logo)  # logo de la plateforme, en haut à droite
    image = game.image or (game.steam.image if game.steam else None)
    if image:
        embed.set_image(url=image)  # grande image du jeu

    view = discord.ui.View()
    label = "Voir sur le store" if game.upcoming else "Voir l'offre" if game.via else "Récupérer le jeu"
    view.add_item(discord.ui.Button(label=label, url=game.url, emoji="🔗"))
    if game.steam and steam.APPID_IN_URL.search(game.url or "") is None:
        view.add_item(discord.ui.Button(label="Fiche Steam", url=game.steam.url, emoji="📊"))
    pinged = PING_ROLE_ID and not game.upcoming and game.reviews >= PING_MIN_REVIEWS
    return {"content": f"<@&{PING_ROLE_ID}>" if pinged else None, "embed": embed, "view": view}


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
        self.steam_cache = steam.Cache(BASE_DIR / "steam_cache.json")
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None
        self.once = once
        self.failed = False
        self._once_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(headers=sources.HEADERS, timeout=sources.TIMEOUT)
        if self.once:
            # Mode GitHub Actions : le bot n'est en ligne que quelques secondes, les commandes
            # ne répondraient pas. On ne les déclare donc pas.
            self._once_task = asyncio.create_task(self.run_once())
            return
        self.register_commands()
        try:
            await self.tree.sync()
        except discord.HTTPException as error:
            log.warning("Commandes /gratuits indisponibles (%s). Réinvite le bot avec le scope applications.commands.", error)
        self.check_loop.change_interval(minutes=CHECK_INTERVAL_MINUTES)
        self.check_loop.start()

    # ------------------------------------------------------------- Commandes

    def register_commands(self) -> None:
        @self.tree.command(name="gratuits", description="Les jeux gratuits à récupérer en ce moment")
        async def gratuits(interaction: discord.Interaction) -> None:
            await self.answer_list(interaction, upcoming=False)

        @self.tree.command(name="prochainement", description="Les jeux qui deviennent gratuits bientôt")
        async def prochainement(interaction: discord.Interaction) -> None:
            await self.answer_list(interaction, upcoming=True)

    async def answer_list(self, interaction: discord.Interaction, upcoming: bool) -> None:
        await interaction.response.defer(thinking=True)
        try:
            games = await sources.collect_free_games(self.session, PLATFORMS, True)
        except Exception:
            log.exception("Commande impossible à traiter")
            await interaction.followup.send("Les boutiques ne répondent pas, réessaie dans un instant.")
            return
        games = [game for game in games if game.upcoming == upcoming]
        if STEAM_LOOKUP:
            await steam.enrich(self.session, self.steam_cache, games)
        games.sort(key=lambda game: -game.reviews)  # ici on lit de haut en bas : le plus connu d'abord

        title = "🔜 Bientôt gratuits" if upcoming else "🎁 Gratuits en ce moment"
        if not games:
            await interaction.followup.send(f"**{title}** — rien à signaler pour l'instant.")
            return
        lines = []
        for game in games[:10]:
            emoji, _ = steam.tier_of(game.steam)
            details = [game.platform]
            if game.reviews:
                details.append(f"{game.reviews:,} avis".replace(",", " "))
            date = game.start if upcoming else game.end
            if date:
                details.append(("dispo " if upcoming else "fin ") + discord_time(date, "R"))
            lines.append(f"{emoji} **[{sources.shorten(game.title, 90)}]({game.url})** — {' · '.join(details)}")
        embed = discord.Embed(title=title, description="\n".join(lines), color=0x57F287)
        if len(games) > 10:
            embed.set_footer(text=f"… et {len(games) - 10} autre(s).")
        await interaction.followup.send(embed=embed)

    async def close(self) -> None:
        self.check_loop.cancel()
        self.steam_cache.save()
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

    async def send(self, channel, key: str, message: dict, name: str | None = None) -> bool:
        try:
            await channel.send(**message)
        except discord.HTTPException as error:
            log.error("Envoi impossible dans #%s : %s", channel, error)
            return False
        if name:
            self.state.names[name] = time.time()
        self.state.add(key)
        return True

    def community_offers(self, official: list[sources.FreeGame], community: list[sources.FreeGame]) -> list[sources.FreeGame]:
        """Garde les offres des sites communautaires qui ne sont pas déjà annoncées par une autre source.
        Les jeux Epic « bientôt gratuits » comptent aussi : Epic les publiera lui-même le jour venu."""
        known = self.state.recent_names() | {game.name for game in official if game.name}
        fresh = []
        # Les noms les plus courts d'abord : « deadshot » est enregistré avant
        # « retro inspired fps deadshot », qui est alors reconnu comme le même jeu.
        for game in sorted(community, key=lambda game: len(game.name or "")):
            if game.key in self.state:
                continue
            if is_duplicate(game, known):
                self.state.add(game.key)  # même jeu déjà annoncé : on l'ignore pour de bon
                continue
            if game.name:
                known.add(game.name)
            fresh.append(game)
        return fresh

    async def post_free_games(self) -> None:
        if not (GAMES_CHANNEL_ID or UPCOMING_CHANNEL_ID):
            return
        want_community = bool(COMMUNITY_SOURCES and GAMES_CHANNEL_ID)
        games, community = await asyncio.gather(
            sources.collect_free_games(self.session, PLATFORMS, SHOW_UPCOMING),
            sources.collect_community(self.session, COMMUNITY_SOURCES, PLATFORMS) if want_community
            else asyncio.sleep(0, result=[]),
        )
        available = [game for game in games if not game.upcoming]
        upcoming = [game for game in games if game.upcoming]
        if UPCOMING_CHANNEL_ID:
            # Clé propre au salon dédié : les jeux déjà annoncés dans le salon principal y sont aussi publiés
            upcoming = [dataclasses.replace(game, key=f"{game.key}@{UPCOMING_CHANNEL_ID}") for game in upcoming]
        available += self.community_offers(games, community)
        await self.post_games(GAMES_CHANNEL_ID, "des jeux disponibles", available)
        await self.post_games(UPCOMING_CHANNEL_ID or GAMES_CHANNEL_ID, "des jeux bientôt gratuits", upcoming)

    async def post_games(self, channel_id: int, label: str, games: list[sources.FreeGame]) -> None:
        if not channel_id:
            return
        new_games = [game for game in games if game.key not in self.state]
        if new_games and STEAM_LOOKUP:
            await steam.enrich(self.session, self.steam_cache, new_games)
            kept = [game for game in new_games if game.trusted or game.reviews >= MIN_REVIEWS]
            if len(kept) < len(new_games):
                log.info("Salon %s : %d offre(s) trop confidentielle(s) écartée(s) (MIN_REVIEWS=%d).",
                         label, len(new_games) - len(kept), MIN_REVIEWS)
            # Steam n'affiche pas de prix pour un free-to-play : c'est ainsi qu'on reconnaît
            # Brawlhalla ou Rainbow Six Siege, gratuits en permanence, parmi les vrais cadeaux.
            # On ne les marque pas comme publiés : si Steam était juste indisponible, le prochain
            # tour les rattrapera.
            new_games = [game for game in kept if not game.needs_price or (game.steam and game.steam.price)]
            if len(new_games) < len(kept):
                log.info("Salon %s : %d jeu(x) gratuit(s) en permanence ignoré(s).", label, len(kept) - len(new_games))
        elif new_games:
            # Sans Steam, impossible de reconnaître les free-to-play : on ignore ces sources-là
            new_games = [game for game in new_games if not game.needs_price]
        # Du moins connu au plus connu : le gros jeu est publié en dernier, donc tout en bas du salon,
        # là où tout le monde regarde en arrivant.
        new_games.sort(key=lambda game: game.reviews)
        if not new_games:
            log.info("Salon %s : rien de nouveau.", label)
            return
        channel = await self.get_channel_or_log(channel_id, label)
        if channel is None:
            return
        sent = 0
        for game in new_games:
            sent += await self.send(channel, game.key, game_message(game), None if game.upcoming else game.name)
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


def _describe(game: sources.FreeGame, note: str = "") -> str:
    emoji, tier = steam.tier_of(game.steam)
    avis = f"{game.reviews:,} avis".replace(",", " ") if game.reviews else "—"
    worth = (game.steam.price if game.steam else None) or game.worth or "?"
    date = game.end or game.start
    return (f"{emoji} [{game.platform}] {sources.shorten(game.title, 70)}{note}\n"
            f"    {tier} · {avis} · prix {worth} · "
            f"{date.strftime('%d/%m %H:%M') if date else 'date ?'}\n    {game.url}")


async def dry_run() -> None:
    """Affiche ce que le bot publierait, sans se connecter à Discord."""
    cache = steam.Cache(BASE_DIR / "steam_cache.json")
    async with aiohttp.ClientSession(headers=sources.HEADERS, timeout=sources.TIMEOUT) as session:
        games = await sources.collect_free_games(session, PLATFORMS, SHOW_UPCOMING)
        community = await sources.collect_community(session, COMMUNITY_SOURCES, PLATFORMS) if COMMUNITY_SOURCES else []
        if STEAM_LOOKUP:
            print("Mesure de la notoriété sur Steam…")
            await steam.enrich(session, cache, games + community)

        print(f"\n=== {len(games)} jeu(x) gratuit(s) trouvé(s) sur les boutiques ===")
        for game in sorted(games, key=lambda g: (g.upcoming, -g.reviews)):
            game_message(game)["embed"].to_dict()  # vérifie que le message Discord se construit bien
            if game.needs_price and not (game.steam and game.steam.price):
                note = "  -> GRATUIT EN PERMANENCE, ignoré"
            else:
                note = "  (BIENTÔT)" if game.upcoming else ""
            print(_describe(game, note))

        if community:
            # Même ordre que le bot (noms courts d'abord) pour que les doublons soient repérés pareil
            known = {game.name for game in games if game.name}
            notes = {}
            for game in sorted(community, key=lambda g: len(g.name or "")):
                if is_duplicate(game, known):
                    notes[game.key] = "  -> DOUBLON, ignoré"
                elif not game.trusted and game.reviews < MIN_REVIEWS:
                    notes[game.key] = f"  -> ÉCARTÉ (moins de {MIN_REVIEWS} avis)"
                else:
                    notes[game.key] = ""
                if game.name:
                    known.add(game.name)

            print(f"\n=== {len(community)} offre(s) des sites communautaires ===")
            for game in sorted(community, key=lambda g: -g.reviews):
                game_message(game)["embed"].to_dict()
                print(_describe(game, notes[game.key]) + f"\n    via {game.via} · nom repéré: {game.name}")
            print(f"\n-> {sum(1 for n in notes.values() if not n)} offre(s) communautaire(s) seraient publiées.")

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
