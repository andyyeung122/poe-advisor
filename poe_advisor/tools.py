"""Tools the agent can call. Each returns JSON text; errors come back as tool errors, not exceptions."""

from __future__ import annotations

import json
from typing import Any, Callable

import jsonschema

from . import account as ggg
from . import analysis, feeds, ninja, pob, trade
from .config import settings
from .rag import KnowledgeBase, parse_date

MAX_RESULT_CHARS = 14000

LEAGUE_PROP = {"type": "string", "description": "League name. Omit to use the session's current league."}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "leagues",
        "description": "List leagues poe.ninja tracks and show which league this session is using.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "set_league",
        "description": "Switch the league used by every later lookup (prices, trade, characters). "
                       "Call this when the user names a different league (e.g. Standard, Hardcore, last league).",
        "input_schema": {
            "type": "object",
            "properties": {"league": {"type": "string", "description": "League name, or empty to reset to default."}},
            "required": ["league"], "additionalProperties": False,
        },
    },
    {
        "name": "load_character",
        "description": "Load a character's gear, gems, and (from poe.ninja) computed DPS/EHP/resistances/max hits, plus a "
                       "pre-computed list of upgrade gaps. Makes it the active build for later questions. "
                       "source=auto tries poe.ninja first, then pathofexile.com. Omit account/character to load the "
                       "player's own character configured in .env (POE_ACCOUNT / POE_CHARACTER).",
        "input_schema": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Account name with discriminator, e.g. Name#1234."},
                "character": {"type": "string"},
                "source": {"type": "string", "enum": ["auto", "poe.ninja", "pathofexile.com"]},
                "league": LEAGUE_PROP,
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_characters",
        "description": "List characters on a pathofexile.com account (needs a public profile or POESESSID).",
        "input_schema": {"type": "object", "properties": {"account": {"type": "string"}},
                         "required": ["account"], "additionalProperties": False},
    },
    {
        "name": "load_pob",
        "description": "Load a Path of Building code or a pobb.in / pastebin / poe.ninja pob link as the active build. "
                       "Most accurate source: includes PoB's computed DPS, EHP, and max hit numbers.",
        "input_schema": {"type": "object", "properties": {"code_or_url": {"type": "string"}},
                         "required": ["code_or_url"], "additionalProperties": False},
    },
    {
        "name": "active_build",
        "description": "Re-read the active build summary and gaps (use after the conversation has moved on).",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "price_check",
        "description": "Look up poe.ninja prices by item name (uniques, gems, currency, fragments, scarabs, cards, "
                       "essences, maps, ...). Values are in chaos and divine. Fuzzy-matches the name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string", "enum": ninja.ALL_TYPES,
                             "description": "Optional poe.ninja category to restrict the search."},
                "gem_level": {"type": "integer"}, "gem_quality": {"type": "integer"},
                "corrupted": {"type": "boolean"}, "links": {"type": "integer", "description": "5 or 6 for linked uniques"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "league": LEAGUE_PROP,
            },
            "required": ["query"], "additionalProperties": False,
        },
    },
    {
        "name": "market_overview",
        "description": "Rank a poe.ninja category by value, 7-day price change (rising/falling), or trade volume. "
                       "Use for money-making questions: what farmable loot is worth the most or trending up.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ninja.ALL_TYPES},
                "sort": {"type": "string", "enum": ["value", "rising", "falling", "volume"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 40},
                "min_chaos": {"type": "number"},
                "league": LEAGUE_PROP,
            },
            "required": ["category"], "additionalProperties": False,
        },
    },
    {
        "name": "find_trade_stat",
        "description": "Resolve mod text such as '+# to maximum Life' or '#% chance to Suppress Spell Damage' to "
                       "official trade stat ids. Prefer pseudo.* ids (they total a stat across all mods).",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 15}},
            "required": ["text"], "additionalProperties": False,
        },
    },
    {
        "name": "trade_search",
        "description": "Search live listings on the official trade site (pathofexile.com/trade), cheapest first. Use to "
                       "price rare upgrades with specific stats. Rate limited by GGG: batch your thinking and search "
                       "deliberately (a few searches per answer). Returns a trade_url the user can open.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": trade.CATEGORIES},
                "rarity": {"type": "string", "enum": ["normal", "magic", "rare", "unique", "nonunique"]},
                "name": {"type": "string", "description": "Unique item name, e.g. 'Tabula Rasa'."},
                "base_type": {"type": "string", "description": "Base type, e.g. 'Simple Robe'."},
                "stats": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "stat": {"type": "string", "description": "Trade stat id or mod text."},
                            "min": {"type": "number"}, "max": {"type": "number"},
                        },
                        "required": ["stat"], "additionalProperties": False,
                    },
                },
                "max_price_chaos": {"type": "number"},
                "links_min": {"type": "integer", "minimum": 1, "maximum": 6},
                "status": {"type": "string", "enum": ["online", "securable", "any"],
                           "description": "online (default) or securable (instant buyout only)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "league": LEAGUE_PROP,
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "search_knowledge",
        "description": "Retrieve passages from the local knowledge base of ingested Reddit posts and guides "
                       "(money-making strategies, build advice). Results include age and, when the league start "
                       "is known, before_league_start: those posts describe an older patch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 12},
                "max_age_days": {"type": "number", "description": "Drop passages older than this."},
                "current_league_only": {"type": "boolean",
                                        "description": "Only posts published since the session league started."},
            },
            "required": ["query"], "additionalProperties": False,
        },
    },
    {
        "name": "reddit_search",
        "description": "Search Reddit live (r/pathofexile, r/PathOfExileBuilds, ...) and add the results to the "
                       "knowledge base. Use when the knowledge base is thin or stale for a question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "subreddit": {"type": "string"},
                "time_range": {"type": "string", "enum": ["day", "week", "month", "year", "all"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 15},
                "include_comments": {"type": "boolean", "description": "Also fetch top comments (slower)."},
            },
            "required": ["query"], "additionalProperties": False,
        },
    },
]

_SCHEMA_BY_NAME = {t["name"]: t["input_schema"] for t in TOOL_SCHEMAS}


class Toolbox:
    def __init__(self, kb: KnowledgeBase | None = None):
        self.kb = kb or KnowledgeBase(settings.knowledge_path)
        self.build: dict[str, Any] | None = None
        self._handlers: dict[str, Callable[..., Any]] = {
            "leagues": self.leagues, "set_league": self.set_league,
            "load_character": self.load_character, "list_characters": self.list_characters,
            "load_pob": self.load_pob, "active_build": self.active_build,
            "price_check": self.price_check, "market_overview": self.market_overview,
            "find_trade_stat": self.find_trade_stat, "trade_search": self.trade_search,
            "search_knowledge": self.search_knowledge, "reddit_search": self.reddit_search,
        }

    def definitions(self) -> list[dict[str, Any]]:
        return [{**t, "eager_input_streaming": True} for t in TOOL_SCHEMAS]

    def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Execute a tool. Returns (content, is_error)."""
        handler = self._handlers.get(name)
        if handler is None:
            return f"Unknown tool {name}", True
        try:
            # Eager input streaming means the server no longer validates inputs; we do.
            jsonschema.validate(args, _SCHEMA_BY_NAME[name])
            result = handler(**args)
        except jsonschema.ValidationError as exc:
            return f"Invalid input: {exc.message}", True
        except Exception as exc:  # surface any API failure to the model so it can adapt
            return f"{type(exc).__name__}: {exc}", True
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + ' ... [truncated; narrow the query for more]'
        return text, False

    # -- league -------------------------------------------------------------

    def leagues(self) -> dict[str, Any]:
        return {"current": ninja.current_league(), **ninja.list_leagues()}

    def set_league(self, league: str) -> dict[str, Any]:
        return {"current": ninja.set_league(league or None)}

    # -- builds -------------------------------------------------------------

    def _activate(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.build = snapshot
        return analysis.summary(snapshot)

    def load_character(self, account: str | None = None, character: str | None = None, source: str = "auto",
                       league: str | None = None) -> dict[str, Any]:
        account = account or settings.account
        character = character or settings.character
        if not (account and character):
            raise ValueError("No account/character given and POE_ACCOUNT/POE_CHARACTER are not set in .env. "
                             "Ask the player for them (account looks like Name#1234).")
        errors = []
        if source in ("auto", "poe.ninja"):
            try:
                return self._activate(analysis.from_ninja(ninja.character(account, character, league)))
            except Exception as exc:
                errors.append(f"poe.ninja: {exc}")
        if source in ("auto", "pathofexile.com"):
            try:
                return self._activate(analysis.from_ggg(ggg.get_items(account, character), account))
            except Exception as exc:
                errors.append(f"pathofexile.com: {exc}")
        raise RuntimeError("; ".join(errors) + " | Ask the user for a Path of Building code as a fallback.")

    def list_characters(self, account: str) -> list[dict[str, Any]]:
        return ggg.list_characters(account)

    def load_pob(self, code_or_url: str) -> dict[str, Any]:
        return self._activate(pob.load(code_or_url))

    def active_build(self) -> dict[str, Any]:
        if not self.build:
            return {"active_build": None, "hint": "Ask for account + character name, or a PoB code."}
        return analysis.summary(self.build)

    # -- prices -------------------------------------------------------------

    def price_check(self, query: str, category: str | None = None, limit: int = 8,
                    league: str | None = None, **filters: Any) -> dict[str, Any]:
        rows = ninja.search_prices(query, [category] if category else None, league=league, limit=limit, **filters)
        return {"league": league or ninja.current_league(), "divine_in_chaos": ninja.divine_in_chaos(league),
                "matches": rows}

    def market_overview(self, category: str, sort: str = "value", limit: int = 20, min_chaos: float = 1.0,
                        league: str | None = None) -> dict[str, Any]:
        return {"league": league or ninja.current_league(), "divine_in_chaos": ninja.divine_in_chaos(league),
                "rows": ninja.market_movers(category, league, sort, limit, min_chaos)}

    def find_trade_stat(self, text: str, limit: int = 6) -> list[dict[str, str]]:
        return trade.find_stats(text, limit)

    def trade_search(self, limit: int = 10, league: str | None = None, **query: Any) -> dict[str, Any]:
        result = trade.search(league=league, limit=limit, **query)
        result["divine_in_chaos"] = ninja.divine_in_chaos(league)
        return result

    # -- knowledge ----------------------------------------------------------

    def search_knowledge(self, query: str, k: int = 6, max_age_days: float | None = None,
                         current_league_only: bool = False) -> dict[str, Any]:
        start = ninja.league_start()
        hits = self.kb.search(query, k=k, max_age_days=max_age_days, since=start if current_league_only else None)
        if start:
            for hit in hits:
                published = parse_date(hit["published"])
                hit["before_league_start"] = published is None or published < start
        hints = []
        if current_league_only and start is None:
            hints.append("League start date unavailable, so results are not filtered by league.")
        if not hits:
            hints.append("No posts since the league started; try reddit_search with time_range week or day, "
                         "or web_search." if current_league_only and start else
                         "No matches; try reddit_search or web_search.")
        return {"documents_in_kb": len(self.kb), "league_started": start.isoformat() if start else None,
                "results": hits, "hint": " ".join(hints) or None}

    def reddit_search(self, query: str, subreddit: str = "pathofexile", time_range: str = "month",
                      limit: int = 8, include_comments: bool = False) -> dict[str, Any]:
        posts = feeds.search(query, subreddit, time_range, limit=limit)
        docs = []
        for post in posts:
            top = feeds.comments(post["url"], limit=8) if include_comments else []
            docs.append(feeds.to_document(post, top))
        added = self.kb.add(docs)
        return {
            "added_to_kb": added,
            "posts": [{"title": d["title"], "url": d["url"], "published": d["published"],
                       "text": d["text"][:1500]} for d in docs],
        }
