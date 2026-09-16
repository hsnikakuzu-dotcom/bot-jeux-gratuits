"""Sources : jeux gratuits (Epic Games, GamerPower, Dealabs, GG.deals, Reddit) et actualités jeux vidéo (flux RSS)."""
from __future__ import annotations

import calendar
import html
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

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
    name: str | None = None  # nom simplifié du jeu, pour repérer le même jeu sur plusieurs sites
    via: str | None = None   # site communautaire d'où vient l'offre (Dealabs, GG.deals, Reddit)


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


def describe_error(error: Exception) -> str:
    """Message d'erreur court (sans les en-têtes HTTP, qui sont visibles dans les journaux GitHub)."""
    if isinstance(error, aiohttp.ClientResponseError):
        return f"erreur HTTP {error.status}"
    return type(error).__name__ + (f" : {error}" if str(error) else "")


def clean_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text or ""))


_NAME_NOISE = {"giveaway", "key", "free", "gratuit", "gratuite", "offert", "offerte", "pc"}


def game_name(title: str | None) -> str | None:
    """« FOR HONOR (Ubisoft) Giveaway » et « For Honor » donnent tous les deux « for honor »."""
    text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text)
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    while words and words[-1] in _NAME_NOISE:
        words.pop()
    return " ".join(words) or None


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
            name=game_name(element.get("title")),
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
        title = re.sub(r"\s+Giveaway$", "", item.get("title") or "", flags=re.I)
        games.append(FreeGame(
            key=f"gamerpower:{item['id']}",
            title=title,
            platform=", ".join(stores) or "PC",
            url=item.get("open_giveaway_url") or item.get("gamerpower_url"),
            description=shorten(item.get("description"), 350),
            image=item.get("image") or item.get("thumbnail"),
            worth=worth if worth and worth != "N/A" else None,
            end=end,
            name=game_name(title),
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
            log.warning("Epic Games indisponible (%s)", describe_error(error))
    if others:
        try:
            games += await fetch_gamerpower(session, others)
        except Exception as error:
            log.warning("GamerPower indisponible (%s)", describe_error(error))
    games.sort(key=lambda game: game.upcoming)  # offres disponibles d'abord, "bientôt gratuit" ensuite
    return games


# ------------------------------------------------------------ Outils flux RSS

_IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


async def _download(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()


def _entry_image(entry) -> str | None:
    for media in entry.get("media_content", []) + entry.get("media_thumbnail", []):
        if media.get("url"):
            return media["url"]
    for enclosure in entry.get("enclosures", []):
        if enclosure.get("type", "").startswith("image/"):
            return enclosure.get("href")
    match = _IMG_RE.search(entry.get("summary", ""))
    return html.unescape(match.group(1)) if match else None


def _entry_date(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime.fromtimestamp(calendar.timegm(parsed), timezone.utc) if parsed else None


# ------------------------------------ Sites communautaires (Dealabs, GG.deals, Reddit)

COMMUNITY_SOURCES = {
    "dealabs": ("Dealabs", ["https://www.dealabs.com/rss/groupe/jeux-video", "https://www.dealabs.com/rss/groupe/jeux-pc"]),
    "ggdeals": ("GG.deals", ["https://gg.deals/news/freebies/feed/"]),
    "reddit": ("r/FreeGameFindings", ["https://www.reddit.com/r/FreeGameFindings/new/.rss"]),
}
COMMUNITY_MAX_AGE = timedelta(days=4)  # au-delà, l'offre est probablement terminée

# (motif, nom affiché, identifiants de PLATFORMS qui l'autorisent ; vide = jamais, car payant)
_PLATFORM_HINTS = [(re.compile(pattern, re.I), label, set(slugs)) for pattern, label, slugs in [
    (r"\bprime gaming\b|\bamazon\b|\bluna\b", "Prime Gaming", []),
    (r"\bepic\b", "Epic Games", ["epic-games-store"]),
    (r"\bsteam\b", "Steam", ["steam"]),
    (r"\bgog\b", "GOG", ["gog"]),
    (r"\bitch(\.io|io)?\b", "Itch.io", ["itchio"]),
    (r"\bubisoft\b|\buplay\b", "Ubisoft Connect", ["ubisoft"]),
    (r"\bea app\b|\borigin\b|\belectronic arts\b", "EA", ["origin"]),
    (r"\bbattle\.?net\b", "Battle.net", ["battlenet"]),
    (r"\bindiegala\b|\bdrm[- ]?free\b", "DRM-Free", ["drm-free"]),
    (r"\bplaystation\b|\bps[45]\b", "PlayStation", ["ps4", "ps5"]),
    (r"\bxbox\b", "Xbox", ["xbox-one", "xbox-series-xs"]),
    (r"\bswitch\b|\bnintendo\b", "Nintendo Switch", ["switch"]),
    (r"\bandroid\b|\bgoogle play\b", "Android", ["android"]),
    (r"\bios\b|\biphone\b|\bapp store\b", "iOS", ["ios"]),
]]


def _detect_platform(text: str, platforms: list[str]) -> tuple[str, bool]:
    """Renvoie (plateforme affichée, autorisée par PLATFORMS ?)."""
    for pattern, label, slugs in _PLATFORM_HINTS:
        if pattern.search(text):
            return label, bool(slugs & set(platforms))
    return "PC", True


def _parse_rfc822(value: str | None) -> datetime | None:
    try:
        return parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


_DEALABS_FREE = re.compile(r"\b(gratuite?s?|offerte?s?)\b", re.I)
_DEALABS_SKIP = re.compile(r"jouable|week-?end|essai|d[ée]mo|game ?pass|abonnement|b[êe]ta|playtest|\bdlc\b|extension|skin|\bpack\b", re.I)
_DEALABS_IMAGE = re.compile(r'<img[^>]+src="(https://static-pepper\.dealabs\.com/threads/raw/[^"]+/fs/[^"]+)"')


def _parse_dealabs(raw: bytes, platforms: list[str]) -> list[FreeGame]:
    games = []
    for item in ET.fromstring(raw).iter("item"):
        fields = {}
        for child in item:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "merchant":
                fields["merchant"], fields["price"] = child.get("name", ""), child.get("price", "")
            elif tag in ("title", "link", "description", "pubDate"):
                fields[tag] = (child.text or "").strip()

        title = html.unescape(fields.get("title", ""))
        price = fields.get("price", "").strip()
        if not _DEALABS_FREE.search(title) or _DEALABS_SKIP.search(title):
            continue
        if price and not re.fullmatch(r"0+([,.]0+)?\s*€?", price):
            continue  # vraie promo payante
        platform, allowed = _detect_platform(f"{fields.get('merchant', '')} {title}", platforms)
        if not allowed or not fields.get("link"):
            continue

        # « [PC] Mechabellum Gratuit sur PC (Dématérialisé) » -> « Mechabellum »
        name = re.sub(r"^\s*(\[[^\]]*\]\s*)+|^\s*(le\s+)?jeu\s+", "", title, flags=re.I)
        name = re.split(r"\s+(?:gratuite?s?|offerte?s?|sur)\b|\s+-\s+", name, maxsplit=1, flags=re.I)[0]
        image = _DEALABS_IMAGE.search(fields.get("description", ""))
        games.append(FreeGame(
            key=f"dealabs:{fields['link']}",
            title=shorten(title, 256),
            platform=platform,
            url=fields["link"],
            description=shorten(clean_html(fields.get("description")), 300),
            image=image.group(1) if image else None,
            start=_parse_rfc822(fields.get("pubDate")),  # date de publication
            name=game_name(name),
            via="Dealabs",
        ))
    return games


_GG_KEEP = re.compile(r"free to keep|giveaway|\b(get|grab|claim|snag)\b.{0,80}\bfree\b", re.I)
_GG_SKIP = re.compile(r"demo|trial|playtest|free weekend|free[- ]to[- ]play|alpha|beta|skin|\bpack\b|\bdlc\b|cosmetic|roundup|mobile|update|in-game", re.I)
_GG_NAME = [re.compile(pattern, re.I) for pattern in (
    r"^(?:last chance to\s+)?(?:get|grab|claim|snag)\s+(.+?)\s+(?:for\s+)?free\b",
    r"^(.+?)\s+is\s+(?:now\s+)?free\s+to\s+keep",
    r"^(.+?)\s+(?:steam\s+|epic games store\s+|gog\s+)?key giveaway",
)]
_VAGUE_NAME = re.compile(r"^(a|an|the next|this|these|two|three|four|five|next|new|some)\b", re.I)


def _parse_ggdeals(raw: bytes, platforms: list[str]) -> list[FreeGame]:
    games = []
    for entry in feedparser.parse(raw).entries:
        title = html.unescape(entry.get("title", ""))
        platform, allowed = _detect_platform(title, platforms)
        freebie_on_store = re.search(r"\bfreebies?\b", title, re.I) and platform != "PC"
        if not (_GG_KEEP.search(title) or freebie_on_store) or _GG_SKIP.search(title) or not allowed:
            continue
        name = next((match.group(1) for pattern in _GG_NAME if (match := pattern.search(title))), None)
        if not name or _VAGUE_NAME.match(name):
            continue  # « Two games are FREE… » : jeu non nommé, déjà couvert par Epic/GamerPower
        games.append(FreeGame(
            key=f"ggdeals:{entry.get('id') or entry.get('link')}",
            title=shorten(title, 256),
            platform=platform,
            url=entry.get("link"),
            description=shorten(clean_html(entry.get("summary")), 300),
            image=_entry_image(entry),
            start=_entry_date(entry),
            name=game_name(name),
            via="GG.deals",
        ))
    return games


_REDDIT_TITLE = re.compile(r"^\s*\[([^\]]+)\]\s*\(([^)]+)\)\s*(.+)$")
_EXTERNAL_LINK = re.compile(r'href="(https?://(?![^"]*(?:reddit\.com|redd\.it))[^"]+)"')


def _parse_reddit(raw: bytes, platforms: list[str]) -> list[FreeGame]:
    games = []
    for entry in feedparser.parse(raw).entries:
        # « [Steam] (Game) Nom du jeu » : on ne garde que les jeux complets
        match = _REDDIT_TITLE.match(html.unescape(entry.get("title", "")))
        if not match or match.group(2).strip().lower() != "game":
            continue
        store, name = match.group(1).strip(), match.group(3).strip()
        platform, allowed = _detect_platform(store, platforms)
        if not allowed:
            continue
        content = (entry.get("content") or [{}])[0].get("value", "") or entry.get("summary", "")
        link = _EXTERNAL_LINK.search(content)
        games.append(FreeGame(
            key=f"reddit:{entry.get('id') or entry.get('link')}",
            title=shorten(name, 256),
            platform=store if platform == "PC" else platform,
            url=html.unescape(link.group(1)) if link else entry.get("link"),
            image=_entry_image(entry),
            start=_entry_date(entry),
            name=game_name(name),
            via="r/FreeGameFindings",
        ))
    return games


_COMMUNITY_PARSERS = {"dealabs": _parse_dealabs, "ggdeals": _parse_ggdeals, "reddit": _parse_reddit}


async def collect_community(session: aiohttp.ClientSession, enabled: list[str], platforms: list[str]) -> list[FreeGame]:
    """Jeux gratuits repérés par les communautés. Un site en panne ou bloqué n'empêche pas les autres."""
    oldest = datetime.now(timezone.utc) - COMMUNITY_MAX_AGE
    games, seen = [], set()
    for source in enabled:
        if source not in COMMUNITY_SOURCES:
            continue
        label, urls = COMMUNITY_SOURCES[source]
        for url in urls:
            try:
                items = _COMMUNITY_PARSERS[source](await _download(session, url), platforms)
            except Exception as error:
                log.warning("%s indisponible (%s)", label, describe_error(error))
                continue
            for game in items:
                if game.key in seen or (game.start and game.start < oldest):
                    continue
                seen.add(game.key)
                games.append(game)
    return games


# ------------------------------------------------------------ Actualités RSS

async def fetch_news(session: aiohttp.ClientSession, feed_url: str) -> list[NewsItem]:
    feed = feedparser.parse(await _download(session, feed_url))
    source = shorten(feed.feed.get("title") or feed_url, 100)
    items = []
    for entry in feed.entries:
        link = entry.get("link")
        if not link:
            continue
        items.append(NewsItem(
            key=f"news:{link}",
            title=shorten(entry.get("title") or "Sans titre", 250),
            url=link,
            source=source,
            summary=shorten(clean_html(entry.get("summary", "")), 300),
            image=_entry_image(entry),
            published=_entry_date(entry),
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
            log.warning("Flux d'actus indisponible (%s) : %s", url, describe_error(error))
    return result
