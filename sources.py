"""Sources : jeux gratuits (Epic Games, Steam, GOG, itch.io...) et actualités jeux vidéo (flux RSS)."""
from __future__ import annotations

import calendar
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
import feedparser

log = logging.getLogger("sources")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FreeGamesDiscordBot/1.0)"}
TIMEOUT = aiohttp.ClientTimeout(total=30)

EPIC_API = (
    "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
    "?locale=fr&country=FR&allowCountries=FR"
)
EPIC_STORE = "https://store.epicgames.com/fr"
GAMERPOWER_API = "https://www.gamerpower.com/api/filter"


@dataclass
class FreeGame:
    key: str  # identifiant unique : sert à ne jamais publier deux fois la même offre
    title: str
    platform: str
    url: str
    description: str = ""
    image: str | None = None
    worth: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    upcoming: bool = False


@dataclass
class NewsItem:
    key: str
    title: str
    url: str
    source: str
    summary: str = ""
    image: str | None = None
    published: datetime | None = None


def shorten(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clean_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text or ""))


# ---------------------------------------------------------------- Epic Games

def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _epic_free_window(groups, now: datetime, upcoming: bool):
    """Renvoie (début, fin) de la première promo à 100 % trouvée, sinon None."""
    for group in groups or []:
        for offer in group.get("promotionalOffers") or []:
            # Chez Epic, discountPercentage = pourcentage du prix restant : 0 => gratuit
            if (offer.get("discountSetting") or {}).get("discountPercentage") != 0:
                continue
            start, end = _parse_iso(offer.get("startDate")), _parse_iso(offer.get("endDate"))
            if upcoming and start and start > now:
                return start, end
            if not upcoming and (start is None or start <= now) and (end is None or now < end):
                return start, end
    return None


def _epic_url(element: dict) -> str:
    mappings = (element.get("offerMappings") or []) + (
        (element.get("catalogNs") or {}).get("mappings") or []
    )
    slug = next((m["pageSlug"] for m in mappings if m.get("pageSlug")), None)
    slug = slug or (element.get("productSlug") or "").removesuffix("/home")
    if not slug:
        return f"{EPIC_STORE}/free-games"
    kind = "bundles" if element.get("offerType") == "BUNDLE" else "p"
    return f"{EPIC_STORE}/{kind}/{slug}"


def _epic_image(element: dict) -> str | None:
    images = {img.get("type"): img.get("url") for img in element.get("keyImages") or []}
    for kind in ("OfferImageWide", "DieselStoreFrontWide", "Thumbnail", "OfferImageTall"):
        if images.get(kind):
            return images[kind]
    return None


async def fetch_epic(session: aiohttp.ClientSession, include_upcoming: bool) -> list[FreeGame]:
    async with session.get(EPIC_API) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)

    now = datetime.now(timezone.utc)
    games = []
    for element in data["data"]["Catalog"]["searchStore"]["elements"]:
        promos = element.get("promotions") or {}
        window = _epic_free_window(promos.get("promotionalOffers"), now, upcoming=False)
        upcoming = False
        if window is None and include_upcoming:
            window = _epic_free_window(promos.get("upcomingPromotionalOffers"), now, upcoming=True)
            upcoming = window is not None
        if window is None:
            continue

        start, end = window
        prices = ((element.get("price") or {}).get("totalPrice") or {}).get("fmtPrice") or {}
        worth = prices.get("originalPrice")
        games.append(FreeGame(
            key=f"epic:{'soon:' if upcoming else ''}{element['id']}:{start.isoformat() if start else ''}",
            title=element.get("title") or "Jeu mystère",
            platform="Epic Games",
            url=_epic_url(element),
            description=shorten(element.get("description"), 350),
            image=_epic_image(element),
            worth=worth if worth and worth != "0" else None,
            start=start,
            end=end,
            upcoming=upcoming,
        ))
    return games


# ------------------------------------------ GamerPower (Steam, GOG, itch.io...)

async def fetch_gamerpower(session: aiohttp.ClientSession, platforms: list[str]) -> list[FreeGame]:
    params = {"platform": ".".join(platforms), "type": "game"}
    async with session.get(GAMERPOWER_API, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    if not isinstance(data, list):  # l'API renvoie un objet quand il n'y a aucune offre
        return []

    now = datetime.now(timezone.utc)
    games = []
    for item in data:
        if item.get("status") != "Active":
            continue
        try:
            end = datetime.strptime(item.get("end_date") or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            end = None  # "N/A" : pas de date de fin connue
        if end and end < now:
            continue

        stores = [p.strip() for p in (item.get("platforms") or "").split(",") if p.strip() not in ("", "PC")]
        worth = item.get("worth")
        games.append(FreeGame(
            key=f"gamerpower:{item['id']}",
            title=re.sub(r"\s+Giveaway$", "", item.get("title") or "", flags=re.I),
            platform=", ".join(stores) or "PC",
            url=item.get("open_giveaway_url") or item.get("gamerpower_url"),
            description=shorten(item.get("description"), 350),
            image=item.get("image") or item.get("thumbnail"),
            worth=worth if worth and worth != "N/A" else None,
            end=end,
        ))
    return games


async def collect_free_games(
    session: aiohttp.ClientSession, platforms: list[str], include_upcoming: bool
) -> list[FreeGame]:
    """Toutes les offres actuelles ; une source en panne n'empêche pas les autres."""
    games: list[FreeGame] = []
    others = [p for p in platforms if p != "epic-games-store"]
    if "epic-games-store" in platforms:
        try:
            games += await fetch_epic(session, include_upcoming)
        except Exception as error:
            log.warning("Epic Games indisponible : %r", error)
    if others:
        try:
            games += await fetch_gamerpower(session, others)
        except Exception as error:
            log.warning("GamerPower indisponible : %r", error)
    games.sort(key=lambda game: game.upcoming)  # offres disponibles d'abord, "bientôt gratuit" ensuite
    return games


# ------------------------------------------------------------ Actualités RSS

_IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


async def fetch_news(session: aiohttp.ClientSession, feed_url: str) -> list[NewsItem]:
    async with session.get(feed_url) as resp:
        resp.raise_for_status()
        raw = await resp.read()

    feed = feedparser.parse(raw)
    source = shorten(feed.feed.get("title") or feed_url, 100)
    items = []
    for entry in feed.entries:
        link = entry.get("link")
        if not link:
            continue

        image = None
        for media in entry.get("media_content", []) + entry.get("media_thumbnail", []):
            if media.get("url"):
                image = media["url"]
                break
        for enclosure in entry.get("enclosures", []):
            if not image and enclosure.get("type", "").startswith("image/"):
                image = enclosure.get("href")
        raw_summary = entry.get("summary", "")
        if not image and (match := _IMG_RE.search(raw_summary)):
            image = match.group(1)

        published = None
        if entry.get("published_parsed"):
            published = datetime.fromtimestamp(calendar.timegm(entry.published_parsed), timezone.utc)

        items.append(NewsItem(
            key=f"news:{link}",
            title=shorten(entry.get("title") or "Sans titre", 250),
            url=link,
            source=source,
            summary=shorten(clean_html(raw_summary), 300),
            image=image,
            published=published,
        ))
    items.sort(key=lambda item: item.published or _EPOCH, reverse=True)  # plus récent d'abord
    return items


async def collect_news(session: aiohttp.ClientSession, feed_urls: list[str]) -> dict[str, list[NewsItem]]:
    """{url du flux: articles}. Les flux en erreur sont simplement absents du résultat."""
    result = {}
    for url in feed_urls:
        try:
            result[url] = await fetch_news(session, url)
        except Exception as error:
            log.warning("Flux d'actus indisponible (%s) : %r", url, error)
    return result
