"""Past-league price history in Turso (libSQL, a hosted SQLite).

Finished leagues never change, so each (league, category, item, variant) is fetched from poe.ninja once and kept
here. Talks to Turso's HTTP API (Hrana "pipeline") with httpx, so there is no extra dependency. Pattern questions
("which currencies peak early and crash?") are plain SQL over the stored summary columns.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import net
from .config import settings

TABLE = "price_history"
_VARIANT_KEYS = ("variant", "links", "gem_level", "gem_quality", "corrupted", "map_tier", "base_type")
_SCHEMA_DONE: set[str] = set()  # databases whose table exists, checked once per process
SCHEMA = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    league TEXT NOT NULL, category TEXT NOT NULL, item TEXT NOT NULL, variant TEXT NOT NULL,
    unit TEXT, days_of_data INTEGER, weeks INTEGER, latest REAL,
    peak_week INTEGER, peak_median REAL, peak_pct REAL,
    trough_week INTEGER, trough_median REAL, final_pct REAL,
    weekly TEXT NOT NULL, saved_at TEXT NOT NULL,
    PRIMARY KEY (league, category, item, variant)
)"""


def configured() -> bool:
    return bool(settings.turso_url and settings.turso_token)


def variant_key(variant: dict[str, Any] | None) -> str:
    return ",".join(f"{k}={variant[k]}" for k in _VARIANT_KEYS if variant and variant.get(k) is not None)


def _arg(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _cell(cell: dict[str, Any]) -> Any:
    kind = cell.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(cell["value"])
    if kind == "float":
        return float(cell["value"])
    return cell.get("value")


class HistoryStore:
    def __init__(self, url: str, token: str, http: Any = None):
        # The dashboard hands out libsql:// URLs; the HTTP API lives on the same host over https.
        self.base = url.strip().rstrip("/").replace("libsql://", "https://", 1)
        self.token = token
        self._http = http

    @classmethod
    def from_settings(cls) -> HistoryStore | None:
        """The configured Turso store, or None when TURSO_* are not set."""
        return cls(settings.turso_url, settings.turso_token) if configured() else None

    def _post(self, statements: list[tuple[str, list[Any]]]) -> list[list[dict[str, Any]]]:
        requests = [{"type": "execute", "stmt": {"sql": sql, "args": [_arg(a) for a in args]}}
                    for sql, args in statements] + [{"type": "close"}]
        client = self._http or net.client()
        resp = client.post(f"{self.base}/v2/pipeline", json={"requests": requests},
                           headers={"Authorization": f"Bearer {self.token}"})
        resp.raise_for_status()
        out = []
        for result in resp.json()["results"][: len(statements)]:
            if result.get("type") == "error":
                raise RuntimeError(result["error"].get("message", "Turso error"))
            body = result["response"]["result"]
            names = [c["name"] for c in body.get("cols", [])]
            out.append([dict(zip(names, (_cell(c) for c in row))) for row in body.get("rows", [])])
        return out

    def _query(self, sql: str, args: list[Any] | None = None) -> list[dict[str, Any]]:
        if self.base not in _SCHEMA_DONE:
            self._post([(SCHEMA, [])])
            _SCHEMA_DONE.add(self.base)
        return self._post([(sql, args or [])])[0]

    def get(self, league: str, category: str, name: str, variant: dict[str, Any] | None = None) -> dict[str, Any] | None:
        rows = self._query(f"SELECT * FROM {TABLE} WHERE league=? AND category=? AND item=? AND variant=?",
                           [league, category, name, variant_key(variant)])
        if not rows:
            return None
        r = rows[0]
        return {
            "league": r["league"], "status": "past", "unit": r["unit"], "days_of_data": r["days_of_data"],
            "latest": r["latest"], "source": "turso",
            "peak": {"week": r["peak_week"], "median": r["peak_median"]},
            "trough": {"week": r["trough_week"], "median": r["trough_median"]},
            "weekly": json.loads(r["weekly"]),
        }

    def save(self, name: str, category: str, variant: dict[str, Any] | None, summary: dict[str, Any]) -> None:
        weekly = summary["weekly"]
        peak_pct = next((w["pct_vs_week1"] for w in weekly if w["week"] == summary["peak"]["week"]), None)
        self._query(
            f"INSERT OR REPLACE INTO {TABLE} (league, category, item, variant, unit, days_of_data, weeks, latest, "
            "peak_week, peak_median, peak_pct, trough_week, trough_median, final_pct, weekly, saved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [summary["league"], category, name, variant_key(variant), summary["unit"], summary["days_of_data"],
             len(weekly), summary["latest"], summary["peak"]["week"], summary["peak"]["median"], peak_pct,
             summary["trough"]["week"], summary["trough"]["median"], weekly[-1]["pct_vs_week1"],
             json.dumps(weekly, separators=(",", ":")), datetime.now(timezone.utc).isoformat()])

    def search(self, league: str | None = None, category: str | None = None, peak_by_week: int | None = None,
               peak_after_week: int | None = None, final_pct_min: float | None = None,
               final_pct_max: float | None = None, min_weeks: int = 4, limit: int = 15) -> list[dict[str, Any]]:
        """Saved curves filtered by shape. Percentages are change versus week 1."""
        where, args = ["weeks >= ?"], [min_weeks]
        for clause, value in (("league = ?", league), ("category = ?", category),
                              ("peak_week <= ?", peak_by_week), ("peak_week > ?", peak_after_week),
                              ("final_pct >= ?", final_pct_min), ("final_pct <= ?", final_pct_max)):
            if value is not None:
                where.append(clause)
                args.append(value)
        return self._query(
            f"SELECT item, category, league, unit, weeks, peak_week, peak_pct, trough_week, final_pct FROM {TABLE} "
            f"WHERE {' AND '.join(where)} ORDER BY peak_pct DESC LIMIT ?", [*args, limit])

    def count(self) -> int:
        return self._query(f"SELECT COUNT(*) AS n FROM {TABLE}")[0]["n"]
