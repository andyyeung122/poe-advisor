"""Official Path of Exile trade API (pathofexile.com/trade). poe.trade itself shut down in 2020."""

from __future__ import annotations

import difflib
import re
from typing import Any
from urllib.parse import quote

from . import net, ninja
from .config import settings

BASE = "https://www.pathofexile.com/api/trade"
DATA_TTL = 24 * 3600
FETCH_BATCH = 10

# Categories accepted by the trade site's type filter (subset most useful for gearing).
CATEGORIES = [
    "weapon", "weapon.one", "weapon.onemelee", "weapon.twomelee", "weapon.bow", "weapon.claw",
    "weapon.dagger", "weapon.runedagger", "weapon.oneaxe", "weapon.onesword", "weapon.onemace",
    "weapon.sceptre", "weapon.staff", "weapon.warstaff", "weapon.twoaxe", "weapon.twomace",
    "weapon.twosword", "weapon.wand", "armour.chest", "armour.boots", "armour.gloves",
    "armour.helmet", "armour.shield", "armour.quiver", "accessory.amulet", "accessory.belt",
    "accessory.ring", "jewel", "jewel.base", "jewel.abyss", "jewel.cluster", "flask", "gem",
    "gem.activegem", "gem.supportgem", "map",
]


class TradeError(RuntimeError):
    pass


def _cookies() -> dict[str, str] | None:
    return {"POESESSID": settings.poesessid} if settings.poesessid else None


def stats_index() -> list[dict[str, str]]:
    data = net.get(f"{BASE}/data/stats", cache_ttl=DATA_TTL)
    return [entry for group in data["result"] for entry in group["entries"]]


def _norm_stat(text: str) -> str:
    text = re.sub(r"[+-]?\d+(\.\d+)?", "#", text.lower())
    text = re.sub(r"[^a-z# %]+", " ", text)
    return re.sub(r"\s+", " ", text).replace("+#", "#").strip()


def find_stats(text: str, limit: int = 5, kind: str | None = None) -> list[dict[str, str]]:
    """Resolve human mod text (``+80 to maximum Life``) to trade stat ids. Pseudo stats rank first on ties."""
    target = _norm_stat(text)
    scored = []
    for entry in stats_index():
        if kind and entry.get("type") != kind:
            continue
        cand = _norm_stat(entry["text"])
        score = difflib.SequenceMatcher(None, target, cand).ratio()
        if target in cand:
            score = max(score, 0.85 + 0.1 * len(target) / max(len(cand), 1))
        if entry.get("type") == "pseudo":
            score += 0.03
        scored.append((score, entry))
    scored.sort(key=lambda pair: -pair[0])
    return [{"id": e["id"], "text": e["text"], "type": e.get("type", "")} for s, e in scored[:limit] if s > 0.5]


def _resolve_stat(stat: str) -> str:
    if "." in stat and " " not in stat:  # already an id like pseudo.pseudo_total_life
        return stat
    matches = find_stats(stat, limit=1)
    if not matches:
        raise TradeError(f"No trade stat matches {stat!r}; call find_trade_stat to look one up")
    return matches[0]["id"]


def _currency_names() -> dict[str, str]:
    data = net.get(f"{BASE}/data/static", cache_ttl=DATA_TTL)
    return {e["id"]: e["text"] for group in data["result"] for e in group["entries"] if e.get("text")}


def to_chaos(amount: float, currency: str, league: str) -> float | None:
    if currency == "chaos":
        return amount
    name = _currency_names().get(currency)
    if not name:
        return None
    for row in ninja.overview("Currency", league):
        if row["name"] == name and row["chaos"]:
            return round(amount * float(row["chaos"]), 1)
    return None


def build_query(
    category: str | None = None,
    rarity: str | None = None,
    name: str | None = None,
    base_type: str | None = None,
    stats: list[dict[str, Any]] | None = None,
    max_price_chaos: float | None = None,
    links_min: int | None = None,
    status: str = "online",
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    type_filters: dict[str, Any] = {}
    if category:
        if category not in CATEGORIES:
            raise TradeError(f"Unknown category {category!r}")
        type_filters["category"] = {"option": category}
    if rarity:
        type_filters["rarity"] = {"option": rarity}
    if type_filters:
        filters["type_filters"] = {"filters": type_filters}
    if max_price_chaos is not None:
        # No currency option = "Chaos Orb Equivalent": divine-priced listings are converted server-side.
        filters["trade_filters"] = {"filters": {"price": {"max": max_price_chaos}}}
    if links_min:
        filters["socket_filters"] = {"filters": {"links": {"min": links_min}}}

    stat_filters = []
    for stat in stats or []:
        value = {k: stat[k] for k in ("min", "max") if stat.get(k) is not None}
        stat_filters.append({"id": _resolve_stat(stat["stat"]), "value": value})

    query: dict[str, Any] = {"status": {"option": status}, "stats": [{"type": "and", "filters": stat_filters}]}
    if name:
        query["name"] = name
    if base_type:
        query["type"] = base_type
    if filters:
        query["filters"] = filters
    return {"query": query, "sort": {"price": "asc"}}


def _mod_text(mod: Any) -> str:
    return mod.get("description", str(mod)) if isinstance(mod, dict) else str(mod)


def _summarize_listing(entry: dict[str, Any], league: str) -> dict[str, Any]:
    item, listing = entry.get("item", {}), entry.get("listing", {})
    price = listing.get("price") or {}
    out: dict[str, Any] = {
        "name": " ".join(p for p in (item.get("name"), item.get("typeLine")) if p),
        "ilvl": item.get("ilvl"),
        "corrupted": item.get("corrupted", False),
        "price": f"{price.get('amount')} {price.get('currency')}" if price else "no price",
        "price_chaos": to_chaos(price["amount"], price["currency"], league) if price else None,
        "indexed": listing.get("indexed"),
    }
    for field in ("implicitMods", "explicitMods", "craftedMods", "fracturedMods", "enchantMods"):
        mods = item.get(field)
        if mods:
            out[field.replace("Mods", "")] = [_mod_text(m) for m in mods]
    sockets = item.get("sockets") or []
    if sockets:
        groups: dict[int, int] = {}
        for sock in sockets:
            groups[sock.get("group", 0)] = groups.get(sock.get("group", 0), 0) + 1
        out["max_links"] = max(groups.values())
    return out


def search(league: str | None = None, limit: int = 10, **query_args: Any) -> dict[str, Any]:
    """Search live listings, cheapest first. Returns a trade-site link the user can open."""
    league = league or ninja.current_league()
    body = build_query(**query_args)
    result = net.post(f"{BASE}/search/{quote(league)}", body, policy="trade-search", cookies=_cookies())
    if result.get("error"):
        raise TradeError(str(result["error"]))
    ids = result.get("result", [])[: max(1, min(limit, 30))]
    listings = []
    for i in range(0, len(ids), FETCH_BATCH):
        batch = ",".join(ids[i : i + FETCH_BATCH])
        fetched = net.get(f"{BASE}/fetch/{batch}", params={"query": result["id"]},
                          policy="trade-fetch", cookies=_cookies())
        listings.extend(_summarize_listing(e, league) for e in fetched.get("result", []) if e)
    return {
        "league": league,
        "total_matches": result.get("total", 0),
        "trade_url": f"https://www.pathofexile.com/trade/search/{quote(league)}/{result['id']}",
        "listings": listings,
    }
