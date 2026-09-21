"""Sources : jeux gratuits (Epic, Steam, GOG, GamerPower, Dealabs, GG.deals, Reddit) et actus (flux RSS)."""
from __future__ import annotations

import asyncio
import calendar
import html
import json
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import aiohttp
import feedparser

import steam

log = logging.getLogger("sources")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FreeGamesDiscordBot/1.0)"}
TIMEOUT = aiohttp.ClientTimeout(total=30)

EPIC_API = (
    "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
    "?locale=fr&country=FR&allowCountries=FR"
)
EPIC_STORE = "https://store.epicgames.com/fr"
GAMERPOWER_API = "https://www.gamerpower.com/api/filter"
STEAM_SEARCH = "https://store.steampowered.com/search/results/"
GOG_CATALOG = "https://catalog.gog.com/v1/catalog"
UBISOFT_FREE = "https://store.ubisoft.com/fr/free-games"


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
    trusted: bool = False      # offre annoncée par la boutique elle-même : jamais filtrée sur la notoriété
    needs_price: bool = False  # source incapable de distinguer un cadeau d'un jeu gratuit en permanence
    steam: steam.SteamInfo | None = None  # rempli après coup par steam.enrich()

    @property
    def reviews(self) -> int:
        return self.steam.reviews if self.steam else 0


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
            trusted=True,
        ))
    return games


# ------------------------------------------------- Steam (promotions à -100 %)

_STEAM_ROW = re.compile(r'<a href="(https://store\.steampowered\.com/app/\d+[^"?]*)[^>]*?data-ds-appid="(\d+)"(.*?)</a>', re.S)
_STEAM_TITLE = re.compile(r'<span class="title">([^<]+)</span>')
_STEAM_DISCOUNT = re.compile(r'data-discount="(\d+)"')
_STEAM_PRICE = re.compile(r'<div class="discount_original_price">([^<]+)</div>')


async def fetch_steam_free(session: aiohttp.ClientSession) -> list[FreeGame]:
    """Jeux offerts directement par Steam (remise de 100 %), que GamerPower rate souvent.

    `specials=1` écarte les free-to-play (gratuits en permanence) et `category1=998`
    les DLC, bandes-son et packs d'objets, qui ne sont pas des jeux à part entière."""
    params = {
        "query": "", "start": 0, "count": 50, "force_infinite": 1, "infinite": 1,
        "maxprice": "free", "specials": 1, "category1": 998, "json": 1, "cc": "FR", "l": "french",
    }
    async with session.get(STEAM_SEARCH, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)

    games = []
    for url, appid, body in _STEAM_ROW.findall(data.get("results_html") or ""):
        discount = _STEAM_DISCOUNT.search(body)
        title = _STEAM_TITLE.search(body)
        if not discount or discount.group(1) != "100" or not title:
            continue
        price = _STEAM_PRICE.search(body)
        name = html.unescape(title.group(1)).strip()
        games.append(FreeGame(
            key=f"steam:{appid}",
            title=name,
            platform="Steam",
            url=url,
            image=steam.HEADER_IMAGE.format(appid=appid),
            worth=html.unescape(price.group(1)).strip() if price else None,
            name=game_name(name),
            trusted=True,
        ))
    return games


# ------------------------------------------------- Ubisoft Store (jeux offerts)

_UBI_PRODUCT = re.compile(r"var product = (\{.*?\});", re.S)
# « FOR HONOR - STANDARD EDITION YEAR 8 » ou « PC DIG-GROWTOPIA-STANDARD-WW » -> le nom du jeu
_UBI_CLEAN = re.compile(
    r"^PC DIG-|\s*[-–]\s*(?:STANDARD|DELUXE|ULTIMATE|GOLD|FREE|PC DIG)\b.*$|-(?:WW|EMEA|EU|STANDARD)\b",
    re.I,
)
# L'« édition » d'un jeu gratuit en permanence le dit : « Free to Play », « Accès Gratuit »…
# Un vrai cadeau, lui, offre l'édition normale du jeu (« Édition Standard »).
_UBI_FOREVER_FREE = re.compile(
    r"free[\s-]?to[\s-]?play|free (?:access|starter|trial)|acc[èe]s (?:gratuit|starter)|essai|d[ée]mo|trial",
    re.I,
)


async def fetch_ubisoft_free(session: aiohttp.ClientSession) -> list[FreeGame]:
    """Page « Jeux gratuits » du Ubisoft Store.

    Elle mélange les vrais cadeaux (For Honor) et les jeux gratuits en permanence (Brawlhalla,
    Trackmania, Rainbow Six Siege…). Deux garde-fous les séparent : le champ `edition` du site,
    puis `needs_price`, qui exige un vrai prix sur Steam avant de publier."""
    async with session.get(UBISOFT_FREE) as resp:
        resp.raise_for_status()
        page = await resp.text()

    games = []
    for blob in _UBI_PRODUCT.findall(page):
        try:
            product = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if _UBI_FOREVER_FREE.search(product.get("edition") or ""):
            continue
        title = _UBI_CLEAN.sub("", product.get("name") or "").strip(" -") or product.get("brand")
        if not title or not product.get("id"):
            continue
        games.append(FreeGame(
            key=f"ubisoft:{product['id']}",
            title=title,
            platform="Ubisoft Connect",
            url=product.get("url") or UBISOFT_FREE,
            image=product.get("image_url") or None,
            name=game_name(title),
            trusted=True,
            needs_price=True,
        ))
    return games


# ------------------------------------------------------ GOG (jeux à -100 %)

async def fetch_gog_free(session: aiohttp.ClientSession) -> list[FreeGame]:
    """Jeux offerts par GOG. Rare, mais ce sont souvent de vrais classiques."""
    params = {
        "limit": 48, "price": "between:0,0", "discounted": "eq:true",
        "productType": "in:game,pack", "page": 1,
        "countryCode": "FR", "locale": "fr-FR", "currencyCode": "EUR",
    }
    async with session.get(GOG_CATALOG, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)

    games = []
    for product in data.get("products") or []:
        title = product.get("title") or ""
        price = product.get("price") or {}
        final = (price.get("finalMoney") or {}).get("amount")
        if not title or final is None or float(final) > 0:
            continue
        games.append(FreeGame(
            key=f"gog:{product.get('id')}",
            title=title,
            platform="GOG",
            url=product.get("storeLink") or f"https://www.gog.com/fr/game/{product.get('slug', '')}",
            image=product.get("coverHorizontal") or None,
            worth=price.get("base"),
            name=game_name(title),
            trusted=True,
        ))
    return games


# ------------------------------------------ GamerPower (Steam, GOG, itch.io...)

# Plateformes comprises par GamerPower : les autres (prime-gaming…) feraient échouer la requête
GAMERPOWER_PLATFORMS = {
    "pc", "steam", "epic-games-store", "ubisoft", "gog", "itchio", "ps4", "ps5",
    "xbox-one", "xbox-series-xs", "switch", "android", "ios", "vr", "battlenet", "origin", "drm-free",
}

# Boutiques où un jeu offert l'est forcément par l'éditeur lui-même : ces offres ne sont
# jamais écartées par MIN_REVIEWS. Steam et Itch.io en sont volontairement absents :
# n'importe qui peut y distribuer des clés, c'est là que se cache le tout-venant.
FIRST_PARTY_STORES = re.compile(
    r"epic games|\bgog\b|ubisoft|origin|\bea app\b|electronic arts|battle\.?net|blizzard|"
    r"playstation|\bps[45]\b|xbox|nintendo|switch",
    re.I,
)


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
        platform = ", ".join(stores) or "PC"
        worth = item.get("worth")
        title = re.sub(r"\s+Giveaway$", "", item.get("title") or "", flags=re.I)
        games.append(FreeGame(
            key=f"gamerpower:{item['id']}",
            title=title,
            platform=platform,
            url=item.get("open_giveaway_url") or item.get("gamerpower_url"),
            description=shorten(item.get("description"), 350),
            image=item.get("image") or item.get("thumbnail"),
            worth=worth if worth and worth != "N/A" else None,
            end=end,
            name=game_name(title),
            # « FOR HONOR (Ubisoft) » vient d'Ubisoft : on ne l'écarte jamais, même sans avis Steam
            trusted=bool(FIRST_PARTY_STORES.search(f"{platform} {title}")),
        ))
    return games


async def _safe(label: str, coro) -> list[FreeGame]:
    """Exécute une source ; en cas de panne on continue avec les autres."""
    try:
        return await coro
    except Exception as error:
        log.warning("%s indisponible (%s)", label, describe_error(error))
        return []


async def collect_free_games(
    session: aiohttp.ClientSession, platforms: list[str], include_upcoming: bool
) -> list[FreeGame]:
    """Toutes les offres actuelles. Les sources sont interrogées en parallèle."""
    # L'ordre compte : en cas de doublon, c'est la première source qui l'emporte.
    # Ubisoft passe après GamerPower, qui connaît en plus la date de fin de l'offre.
    jobs = []
    others = [p for p in platforms if p != "epic-games-store" and p in GAMERPOWER_PLATFORMS]
    if "epic-games-store" in platforms:
        jobs.append(_safe("Epic Games", fetch_epic(session, include_upcoming)))
    if "steam" in platforms:
        jobs.append(_safe("Steam", fetch_steam_free(session)))
    if "gog" in platforms:
        jobs.append(_safe("GOG", fetch_gog_free(session)))
    if others:
        jobs.append(_safe("GamerPower", fetch_gamerpower(session, others)))
    if "ubisoft" in platforms:
        jobs.append(_safe("Ubisoft Store", fetch_ubisoft_free(session)))

    games: list[FreeGame] = []
    seen_keys: set[str] = set()
    seen_names: set[str] = set()
    for batch in await asyncio.gather(*jobs):
        for game in batch:
            # Steam et GamerPower annoncent souvent la même promo : on garde la 1re (Epic/Steam/GOG passent avant)
            if game.key in seen_keys or (game.name and game.name in seen_names):
                continue
            seen_keys.add(game.key)
            if game.name:
                seen_names.add(game.name)
            games.append(game)
    games.sort(key=lambda game: game.upcoming)  # offres disponibles d'abord, "bientôt gratuit" ensuite
    return games


# ------------------------------------------------------------ Outils flux RSS

_IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# Reddit refuse les User-Agent génériques (erreur 429) : il lui en faut un qui identifie le programme
REDDIT_HEADERS = {"User-Agent": "python:bot-jeux-gratuits:2.0 (flux RSS public, lecture seule)"}
RETRY_STATUSES = {429, 500, 502, 503, 504}


async def _download(session: aiohttp.ClientSession, url: str, attempts: int = 2) -> bytes:
    """Télécharge une page ; un refus temporaire (429, 503…) est retenté une fois."""
    headers = REDDIT_HEADERS if "reddit.com" in url else None
    for attempt in range(attempts):
        try:
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return await resp.read()
        except aiohttp.ClientResponseError as error:
            if error.status not in RETRY_STATUSES or attempt == attempts - 1:
                raise
            await asyncio.sleep(3)
    raise RuntimeError("unreachable")


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

# (motif, nom affiché, identifiants de PLATFORMS qui l'autorisent ; vide = jamais)
_PLATFORM_HINTS = [(re.compile(pattern, re.I), label, set(slugs)) for pattern, label, slugs in [
    # Prime Gaming demande un abonnement Amazon Prime, mais ce sont souvent de gros jeux :
    # à activer en ajoutant "prime-gaming" à PLATFORMS.
    (r"\bprime gaming\b|\bamazon (?:prime|gaming)\b|\bluna\b", "Prime Gaming", ["prime-gaming"]),
    (r"\bepic\b", "Epic Games", ["epic-games-store"]),
    (r"\bsteam\b", "Steam", ["steam"]),
    (r"\bgog\b", "GOG", ["gog"]),
    (r"\bitch(\.io|io)?\b", "Itch.io", ["itchio"]),
    (r"\bubisoft\b|\buplay\b", "Ubisoft Connect", ["ubisoft"]),
    (r"\bea app\b|\borigin\b|\belectronic arts\b", "EA", ["origin"]),
    (r"\bbattle\.?net\b", "Battle.net", ["battlenet"]),
    (r"\bindiegala\b|\bfanatical\b|\bdrm[- ]?free\b|\bsans drm\b", "DRM-Free", ["drm-free"]),
    (r"\bplaystation\b|\bps[45]\b", "PlayStation", ["ps4", "ps5"]),
    (r"\bxbox\b", "Xbox", ["xbox-one", "xbox-series-xs"]),
    (r"\bswitch\b|\bnintendo\b", "Nintendo Switch", ["switch"]),
    (r"\bandroid\b|\bgoogle play\b", "Android", ["android"]),
    (r"\bios\b|\biphone\b|\bapp store\b", "iOS", ["ios"]),
]]


def _detect_platform(text: str, platforms: list[str]) -> tuple[str | None, bool]:
    """Renvoie (plateforme reconnue ou None, autorisée par PLATFORMS ?)."""
    for pattern, label, slugs in _PLATFORM_HINTS:
        if pattern.search(text):
            return label, bool(slugs & set(platforms))
    return None, True  # aucune boutique reconnue : à chaque source de décider quoi en faire


def _parse_rfc822(value: str | None) -> datetime | None:
    try:
        return parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


_DEALABS_FREE = re.compile(r"\b(gratuite?s?|offerte?s?)\b", re.I)
_DEALABS_SKIP = re.compile(
    r"jouable|week-?end|essai|d[ée]mo|game ?pass|abonnement|b[êe]ta|playtest|\bdlc\b|extension|skin|\bpack\b",
    re.I,
)
# Ce qui n'est pas un jeu à garder : produits bancaires, matériel, contenu en jeu…
# (c'est ce qui faisait passer « carte Amex gratuite » ou « voitures offertes pour Gran Turismo 7 »)
_DEALABS_NOT_A_GAME = re.compile(
    r"carte (?:bancaire|de cr[ée]dit|cadeau)|\bamex\b|american express|\bvisa\b|mastercard|banque|assurance|"
    r"\bmiles\b|cashback|bon d'achat|forfait|box internet|mobile\b|"
    r"manette|console|clavier|souris|casque|[ée]cran|si[èe]ge|figurine|goodies|t-?shirt|"
    r"[ée]tude ponctuelle|timed research|\bresearch\b|monnaie|v-?bucks|robux|\bcoins?\b|gemmes|cr[ée]dits|"
    r"r[ée]compense|\bavatar\b|tenue|costume|v[ée]hicule|voitures?\b|offertes? pour\b",
    re.I,
)
# Une boutique de jeux connue dans le nom du marchand suffit à valider l'offre
_DEALABS_GAME_SHOP = re.compile(
    r"steam|epic|\bgog\b|itch|ubisoft|origin|\bea\b|electronic arts|battle|blizzard|"
    r"playstation|xbox|microsoft|nintendo|indiegala|fanatical|humble|prime gaming|google play|app store",
    re.I,
)
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
        merchant = fields.get("merchant", "")
        price = fields.get("price", "").strip()
        if not _DEALABS_FREE.search(title) or _DEALABS_SKIP.search(title):
            continue
        if _DEALABS_NOT_A_GAME.search(title):
            continue
        if price and not re.fullmatch(r"0+([,.]0+)?\s*€?", price):
            continue  # vraie promo payante
        platform, allowed = _detect_platform(f"{merchant} {title}", platforms)
        if not allowed or not fields.get("link"):
            continue
        # Le groupe « jeux-video » de Dealabs contient aussi du matériel et des bons plans divers :
        # sans boutique de jeux reconnue ni le mot « jeu », on passe notre chemin.
        if platform is None and not (_DEALABS_GAME_SHOP.search(merchant) or re.search(r"\bjeux?\b", title, re.I)):
            continue
        platform = platform or "PC"

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
        freebie_on_store = re.search(r"\bfreebies?\b", title, re.I) and platform is not None
        if not (_GG_KEEP.search(title) or freebie_on_store) or _GG_SKIP.search(title) or not allowed:
            continue
        name = next((match.group(1) for pattern in _GG_NAME if (match := pattern.search(title))), None)
        if not name or _VAGUE_NAME.match(name):
            continue  # « Two games are FREE… » : jeu non nommé, déjà couvert par Epic/GamerPower
        games.append(FreeGame(
            key=f"ggdeals:{entry.get('id') or entry.get('link')}",
            title=shorten(title, 256),
            platform=platform or "PC",
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
            platform=platform or store,
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
    async def one(source: str, label: str, url: str) -> list[FreeGame]:
        try:
            return _COMMUNITY_PARSERS[source](await _download(session, url), platforms)
        except Exception as error:
            log.warning("%s indisponible (%s)", label, describe_error(error))
            return []

    jobs = [
        one(source, COMMUNITY_SOURCES[source][0], url)
        for source in enabled if source in COMMUNITY_SOURCES
        for url in COMMUNITY_SOURCES[source][1]
    ]
    oldest = datetime.now(timezone.utc) - COMMUNITY_MAX_AGE
    games, seen = [], set()
    for batch in await asyncio.gather(*jobs):
        for game in batch:
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
    async def one(url: str) -> tuple[str, list[NewsItem] | None]:
        try:
            return url, await fetch_news(session, url)
        except Exception as error:
            log.warning("Flux d'actus indisponible (%s) : %s", url, describe_error(error))
            return url, None

    pairs = await asyncio.gather(*(one(url) for url in feed_urls))
    return {url: items for url, items in pairs if items is not None}
