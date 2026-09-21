"""Notoriété Steam : retrouve chaque offre sur Steam pour savoir si le jeu est connu.

Sert à trois choses :
- trier les offres (les gros jeux en dernier, donc tout en bas du salon, là où on regarde) ;
- ne mentionner le rôle que pour les jeux qui le méritent (PING_MIN_REVIEWS) ;
- afficher le vrai prix Steam plutôt que le prix gonflé annoncé par IndieGala & co.

Les réponses sont gardées dans steam_cache.json pour ne pas réinterroger Steam à chaque tour.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import aiohttp

log = logging.getLogger("steam")

STORE_SEARCH = "https://store.steampowered.com/api/storesearch/"
APP_REVIEWS = "https://store.steampowered.com/appreviews/{appid}"
APP_DETAILS = "https://store.steampowered.com/api/appdetails"
HEADER_IMAGE = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"
STORE_PAGE = "https://store.steampowered.com/app/{appid}/"

APPID_IN_URL = re.compile(r"store\.steampowered\.com/(?:[a-z]{2,5}/)?app/(\d+)")

APP_TTL = 7 * 24 * 3600    # les avis d'un jeu sont réinterrogés au bout d'une semaine
NAME_TTL = 30 * 24 * 3600  # un jeu absent de Steam y arrivera peut-être : on réessaie dans un mois
MAX_PARALLEL = 4           # requêtes Steam simultanées (on reste poli)

# (avis minimum, emoji, libellé) — du plus connu au moins connu
TIERS = [
    (30_000, "🏆", "Incontournable"),
    (3_000, "⭐", "Très connu"),
    (300, "👍", "Connu"),
    (0, "🎲", "Confidentiel"),
]
NOT_ON_STEAM = ("❔", "Absent de Steam")


def _norm(text: str | None) -> str:
    """« FOR HONOR™ (Ubisoft) » -> « for honor » : pour comparer deux titres de boutiques différentes."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _same_game(query: str, found: str) -> bool:
    """Steam renvoie toujours un résultat : on vérifie que c'est bien le jeu demandé."""
    a, b = _norm(query), _norm(found)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= 6 and long.startswith(short)


@dataclass
class SteamInfo:
    appid: int
    name: str
    reviews: int = 0
    positive_pct: int | None = None
    metacritic: int | None = None
    price: str | None = None  # prix normal, formaté par Steam ("29,99€")

    @property
    def url(self) -> str:
        return STORE_PAGE.format(appid=self.appid)

    @property
    def image(self) -> str:
        return HEADER_IMAGE.format(appid=self.appid)

    @property
    def tier(self) -> tuple[str, str]:
        return next((t[1:] for t in TIERS if self.reviews >= t[0]), TIERS[-1][1:])

    @property
    def rating(self) -> str | None:
        """Avis traduits nous-mêmes : Steam ne renvoie pas toujours le libellé en français."""
        if not self.reviews or self.positive_pct is None:
            return None
        pct = self.positive_pct
        if pct >= 95 and self.reviews >= 500:
            label = "Extrêmement positives"
        elif pct >= 80:
            label = "Très positives"
        elif pct >= 70:
            label = "Plutôt positives"
        elif pct >= 40:
            label = "Moyennes"
        else:
            label = "Négatives"
        return f"{label} ({pct} %)"

    def summary(self) -> str:
        emoji, label = self.tier
        parts = [f"{emoji} **{label}**"]
        if self.reviews:
            parts.append(f"{self.reviews:,} avis Steam".replace(",", " "))
        if self.rating:
            parts.append(self.rating)
        if self.metacritic:
            parts.append(f"Metacritic {self.metacritic}")
        return " • ".join(parts)


def tier_of(info: SteamInfo | None) -> tuple[str, str]:
    return info.tier if info else NOT_ON_STEAM


def reviews_of(info: SteamInfo | None) -> int:
    return info.reviews if info else 0


class Cache:
    """steam_cache.json : {nom recherché -> appid} et {appid -> infos}. Évite de réinterroger Steam."""

    def __init__(self, path: Path):
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        self.names: dict[str, list] = data.get("names", {})  # nom -> [appid, date]
        self.apps: dict[str, dict] = data.get("apps", {})    # appid -> {infos, "t": date}
        self.dirty = False

    def appid_for(self, name: str) -> int | None:
        entry = self.names.get(_norm(name))
        if entry and entry[1] >= time.time() - NAME_TTL:
            return entry[0]  # 0 = recherché sans succès, inutile de recommencer
        return None

    def remember_name(self, name: str, appid: int) -> None:
        self.names[_norm(name)] = [appid, time.time()]
        self.dirty = True

    def info_for(self, appid: int) -> SteamInfo | None:
        entry = self.apps.get(str(appid))
        if not entry or entry.get("t", 0) < time.time() - APP_TTL:
            return None
        fields = SteamInfo.__dataclass_fields__
        try:
            return SteamInfo(**{k: v for k, v in entry.items() if k in fields})
        except TypeError:
            return None  # cache écrit par une autre version du bot : on réinterroge Steam

    def remember_info(self, info: SteamInfo) -> None:
        self.apps[str(info.appid)] = {**info.__dict__, "t": time.time()}
        self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        cutoff = time.time() - NAME_TTL
        self.names = {k: v for k, v in self.names.items() if v[1] >= cutoff}
        self.apps = dict(sorted(self.apps.items(), key=lambda kv: kv[1].get("t", 0))[-3000:])
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"names": self.names, "apps": self.apps}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        self.dirty = False


async def _json(session: aiohttp.ClientSession, url: str, **params):
    try:
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)
    except Exception as error:  # Steam limite parfois les requêtes : on abandonne sans bruit
        log.debug("Steam %s : %s", url, error)
        return None


async def _search(session: aiohttp.ClientSession, name: str) -> SteamInfo | None:
    data = await _json(session, STORE_SEARCH, term=name, l="french", cc="FR")
    for item in (data or {}).get("items", [])[:3]:
        if item.get("type") == "app" and _same_game(name, item.get("name", "")):
            price = (item.get("price") or {}).get("initial")
            meta = str(item.get("metascore") or "")
            return SteamInfo(
                appid=int(item["id"]),
                name=item.get("name") or name,
                metacritic=int(meta) if meta.isdigit() else None,
                price=f"{price / 100:.2f}€".replace(".", ",") if price else None,
            )
    return None


async def _price(session: aiohttp.ClientSession, appid: int) -> str | None:
    data = await _json(session, APP_DETAILS, appids=appid, cc="FR", l="french", filters="price_overview")
    overview = ((data or {}).get(str(appid), {}).get("data") or {}).get("price_overview") or {}
    return overview.get("initial_formatted") or overview.get("final_formatted") or None


async def _reviews(session: aiohttp.ClientSession, appid: int) -> tuple[int, int | None]:
    data = await _json(
        session, APP_REVIEWS.format(appid=appid),
        json=1, language="all", purchase_type="all", num_per_page=0,
    )
    summary = (data or {}).get("query_summary") or {}
    total = int(summary.get("total_reviews") or 0)
    positive = int(summary.get("total_positive") or 0)
    return total, round(100 * positive / total) if total else None


async def lookup(session: aiohttp.ClientSession, cache: Cache, name: str, url: str | None) -> SteamInfo | None:
    """Infos Steam d'un jeu, à partir du lien s'il pointe vers Steam, sinon de son nom."""
    match = APPID_IN_URL.search(url or "")
    appid = int(match.group(1)) if match else cache.appid_for(name)
    if appid == 0:  # déjà cherché récemment : ce jeu n'est pas sur Steam
        return None

    if appid is not None:
        cached = cache.info_for(appid)
        if cached is not None:
            return cached
        info = SteamInfo(appid=appid, name=name, price=await _price(session, appid))
    else:
        info = await _search(session, name)
        cache.remember_name(name, info.appid if info else 0)
        if info is None:
            return None
        cached = cache.info_for(info.appid)
        if cached is not None:
            return cached

    info.reviews, info.positive_pct = await _reviews(session, info.appid)
    cache.remember_info(info)
    return info


async def enrich(session: aiohttp.ClientSession, cache: Cache, games: list) -> None:
    """Ajoute `game.steam` à chaque offre. Une erreur sur un jeu n'empêche pas les autres."""
    limit = asyncio.Semaphore(MAX_PARALLEL)

    async def one(game):
        async with limit:
            try:
                game.steam = await lookup(session, cache, game.name or game.title, game.url)
            except Exception as error:
                log.debug("Notoriété introuvable pour %s : %s", game.title, error)

    await asyncio.gather(*(one(game) for game in games))
    cache.save()
