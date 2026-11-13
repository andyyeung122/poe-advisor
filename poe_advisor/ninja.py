"""poe.ninja client: leagues, economy prices, and indexed characters (Path of Exile 1)."""

from __future__ import annotations

import difflib
import re
import time
from datetime import datetime
from typing import Any, Iterable

from . import net
from .config import settings

BASE = "https://poe.ninja/poe1/api"
GGG_LEAGUES = "https://www.pathofexile.com/api/leagues"  # has start dates, which poe.ninja lacks
PRICE_TTL = 30 * 60
INDEX_TTL = 10 * 60
LEAGUES_TTL = 6 * 60 * 60

# Bulk-tradeable goods priced from the in-game currency exchange.
EXCHANGE_TYPES = [
    "Currency", "Fragment", "Scarab", "DivinationCard", "Essence", "Fossil", "Resonator",
    "Oil", "DeliriumOrb", "Omen", "Tattoo", "Artifact", "AllflameEmber",
]
# Items priced from public stash listings.
STASH_TYPES = [
    "UniqueWeapon", "UniqueArmour", "UniqueAccessory", "UniqueFlask", "UniqueJewel",
    "UniqueRelic", "UniqueTincture", "SkillGem", "ClusterJewel", "Map", "UniqueMap",
    "BlightedMap", "Invitation", "Beast", "Vial", "Wombgift",
]
# BaseType is ~4 MB; only loaded when asked for explicitly.
ON_DEMAND_TYPES = ["BaseType"]
ALL_TYPES = EXCHANGE_TYPES + STASH_TYPES + ON_DEMAND_TYPES

_memo: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_league_override: str | None = None


class NinjaError(RuntimeError):
    pass


def index_state() -> dict[str, Any]:
    return net.get(f"{BASE}/data/index-state", cache_ttl=INDEX_TTL)


def _is_trade_league(name: str) -> bool:
    lowered = name.lower()
    return not any(tag in lowered for tag in ("standard", "hardcore", "ssf", "ruthless")) and not lowered.startswith("hc ")


def _private(name: str) -> bool:
    return bool(re.search(r"\(PL\d+\)$", name))


def list_leagues(include_private: bool = False) -> dict[str, list[str]]:
    """Leagues poe.ninja tracks, split into ones with price data and ones with build/character data."""
    state = index_state()
    builds = [l["name"] for l in state.get("buildLeagues", [])]
    return {
        "economy": [l["name"] for l in state.get("economyLeagues", [])],
        "builds": builds if include_private else [n for n in builds if not _private(n)],
        "old_economy": [l["name"] for l in state.get("oldEconomyLeagues", [])],
    }


def set_league(name: str | None) -> str:
    """Switch the league every lookup uses for the rest of the session. ``None`` reverts to the default."""
    global _league_override
    if not name:
        _league_override = None
        return current_league()
    leagues = list_leagues(include_private=True)
    known = {n.lower(): n for group in leagues.values() for n in group}
    match = known.get(name.strip().lower())
    if match is None:
        raise NinjaError(f"Unknown league {name!r}. poe.ninja tracks: {', '.join(leagues['economy'] + leagues['builds'])}")
    _league_override = match
    return match


def current_league() -> str:
    """Session override, then POE_LEAGUE, then the current softcore trade challenge league."""
    if _league_override:
        return _league_override
    if settings.league:
        return settings.league
    for league in index_state().get("economyLeagues", []):
        if _is_trade_league(league["name"]):
            return league["name"]
    return "Standard"


def league_start(league: str | None = None) -> datetime | None:
    """When a league started, from GGG's public league list. None if unknown or the lookup fails."""
    try:
        league = league or current_league()
        entries = net.get(GGG_LEAGUES, params={"type": "main", "realm": settings.realm}, cache_ttl=LEAGUES_TTL)
    except Exception:  # optional context: callers fall back to no cutoff
        return None
    for entry in entries:
        if entry.get("id", "").lower() == league.lower() and entry.get("startAt"):
            return datetime.fromisoformat(entry["startAt"].replace("Z", "+00:00"))
    return None


def league_slug(league: str, kind: str = "buildLeagues") -> str:
    state = index_state()
    for entry in state.get(kind, []) + state.get("old" + kind[0].upper() + kind[1:], []):
        if entry["name"].lower() == league.lower():
            return entry["url"]
    raise NinjaError(f"poe.ninja does not track league {league!r}")


# ---------------------------------------------------------------------------
# Economy


def _normalize_exchange(category: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    names = {item["id"]: item for item in data.get("items", [])}
    divine_rate = (data.get("core", {}).get("rates") or {}).get("divine")
    out = []
    for line in data.get("lines", []):
        item = names.get(line["id"], {})
        chaos = line.get("primaryValue")
        out.append({
            "name": item.get("name", line["id"]),
            "category": category,
            "subcategory": item.get("category"),
            "chaos": chaos,
            "divine": round(chaos * divine_rate, 4) if chaos is not None and divine_rate else None,
            "volume_chaos_per_hour": line.get("volumePrimaryValue"),
            "change_7d_pct": (line.get("sparkline") or {}).get("totalChange"),
        })
    return out


def _normalize_stash(category: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for line in data.get("lines", []):
        row = {
            "name": line.get("name"),
            "category": category,
            "chaos": line.get("chaosValue"),
            "divine": line.get("divineValue"),
            "listings": line.get("listingCount"),
            "change_7d_pct": (line.get("sparkLine") or {}).get("totalChange"),
        }
        for src, dst in (
            ("baseType", "base_type"), ("variant", "variant"), ("links", "links"),
            ("gemLevel", "gem_level"), ("gemQuality", "gem_quality"), ("corrupted", "corrupted"),
            ("mapTier", "map_tier"), ("levelRequired", "level_required"),
        ):
            if line.get(src) not in (None, "", False):
                row[dst] = line[src]
        if (line.get("listingCount") or 0) < 5:
            row["low_confidence"] = True
        out.append(row)
    return out


def overview(category: str, league: str | None = None) -> list[dict[str, Any]]:
    """All priced entries for one poe.ninja category, normalized to chaos/divine values."""
    league = league or current_league()
    key = (league, category)
    cached = _memo.get(key)
    if cached and time.time() - cached[0] < PRICE_TTL:
        return cached[1]
    if category in EXCHANGE_TYPES:
        data = net.get(f"{BASE}/economy/exchange/current/overview",
                       params={"league": league, "type": category}, cache_ttl=PRICE_TTL)
        rows = _normalize_exchange(category, data)
    elif category in STASH_TYPES or category in ON_DEMAND_TYPES:
        data = net.get(f"{BASE}/economy/stash/current/item/overview",
                       params={"league": league, "type": category}, cache_ttl=PRICE_TTL)
        rows = _normalize_stash(category, data)
    else:
        raise NinjaError(f"Unknown category {category!r}; choose from {', '.join(ALL_TYPES)}")
    _memo[key] = (time.time(), rows)
    return rows


def divine_in_chaos(league: str | None = None) -> float:
    for row in overview("Currency", league):
        if row["name"] == "Divine Orb" and row["chaos"]:
            return float(row["chaos"])
    raise NinjaError("Divine Orb price unavailable")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def _match_score(query: str, name: str) -> float:
    q, n = _norm(query), _norm(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if q in n:
        return 0.9 - 0.3 * (len(n) - len(q)) / max(len(n), 1)
    return difflib.SequenceMatcher(None, q, n).ratio() * 0.8


def search_prices(
    query: str,
    categories: Iterable[str] | None = None,
    league: str | None = None,
    limit: int = 8,
    gem_level: int | None = None,
    gem_quality: int | None = None,
    corrupted: bool | None = None,
    links: int | None = None,
) -> list[dict[str, Any]]:
    """Fuzzy-search item prices by name across poe.ninja categories."""
    cats = list(categories) if categories else EXCHANGE_TYPES + STASH_TYPES
    scored: list[tuple[float, dict[str, Any]]] = []
    for cat in cats:
        try:
            rows = overview(cat, league)
        except Exception:  # a category missing this league (404) shouldn't sink the search
            continue
        for row in rows:
            if gem_level is not None and row.get("gem_level") not in (None, gem_level):
                continue
            if gem_quality is not None and row.get("gem_quality") not in (None, gem_quality):
                continue
            if corrupted is not None and bool(row.get("corrupted")) != corrupted and row.get("gem_level") is not None:
                continue
            if links is not None and row.get("links", 0) != links and cat in ("UniqueArmour", "UniqueWeapon"):
                continue
            score = max(_match_score(query, row["name"] or ""), _match_score(query, row.get("base_type") or "") * 0.7)
            if score >= 0.55:
                scored.append((score, row))
    scored.sort(key=lambda pair: (-pair[0], -(pair[1].get("listings") or pair[1].get("volume_chaos_per_hour") or 0)))
    return [row for _, row in scored[:limit]]


def market_movers(category: str, league: str | None = None, sort: str = "value", limit: int = 20,
                  min_chaos: float = 1.0) -> list[dict[str, Any]]:
    """Top entries in a category by value, 7-day change, or trade volume."""
    rows = [r for r in overview(category, league) if (r.get("chaos") or 0) >= min_chaos and not r.get("low_confidence")]
    keys = {
        "value": lambda r: r.get("chaos") or 0,
        "rising": lambda r: r.get("change_7d_pct") or 0,
        "falling": lambda r: -(r.get("change_7d_pct") or 0),
        "volume": lambda r: r.get("volume_chaos_per_hour") or r.get("listings") or 0,
    }
    if sort not in keys:
        raise NinjaError(f"sort must be one of {sorted(keys)}")
    return sorted(rows, key=keys[sort], reverse=True)[:limit]


# ---------------------------------------------------------------------------
# Characters indexed by poe.ninja builds


def ninja_account(account: str) -> str:
    """poe.ninja writes the account discriminator with a dash: ``Name#1234`` -> ``Name-1234``."""
    return account.strip().replace("#", "-")


def character(account: str, name: str, league: str | None = None) -> dict[str, Any]:
    league = league or current_league()
    slug = league_slug(league)
    version = next(
        (v for v in index_state().get("snapshotVersions", []) if v["url"] == slug and v["type"] == "exp"),
        None,
    )
    if version is None:
        raise NinjaError(f"No poe.ninja build snapshot for league {league!r}")
    try:
        return net.get(
            f"{BASE}/builds/{version['version']}/character",
            params={"account": ninja_account(account), "name": name, "overview": version["snapshotName"],
                    "type": 0, "timeMachine": ""},
            cache_ttl=5 * 60,
        )
    except net.httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise NinjaError(
                f"poe.ninja has not indexed {name!r} on {account!r} in {league}. poe.ninja only tracks ladder-"
                "eligible characters on public profiles; try a Path of Building code instead."
            ) from exc
        raise
