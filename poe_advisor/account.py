"""pathofexile.com character-window endpoints (your own account).

These need the profile's Characters tab set to public, or a POESESSID cookie for a
private profile. GGG returns 403 when neither holds. They give gear and gems but no
computed stats; for DPS/EHP numbers use poe.ninja or a Path of Building code.
"""

from __future__ import annotations

from typing import Any

from . import net
from .config import settings

BASE = "https://www.pathofexile.com/character-window"


class AccountError(RuntimeError):
    pass


def _call(path: str, params: dict[str, str]) -> Any:
    cookies = {"POESESSID": settings.poesessid} if settings.poesessid else None
    try:
        return net.post(f"{BASE}/{path}", None, params={**params, "realm": settings.realm},
                        cookies=cookies, policy="character-window")
    except net.httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise AccountError(
                "pathofexile.com refused the request (403). Make the Characters tab public under "
                "Privacy Settings on pathofexile.com, or set POESESSID in .env."
            ) from exc
        if exc.response.status_code == 404:
            raise AccountError("Account or character not found. Use the full account name, e.g. Name#1234.") from exc
        raise


def list_characters(account: str) -> list[dict[str, Any]]:
    chars = _call("get-characters", {"accountName": account})
    return [{"name": c.get("name"), "league": c.get("league"), "class": c.get("class"), "level": c.get("level")}
            for c in chars]


def get_items(account: str, character: str) -> dict[str, Any]:
    return _call("get-items", {"accountName": account, "character": character})
