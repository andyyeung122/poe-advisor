"""Reddit ingestion via public RSS feeds (Reddit's unauthenticated JSON API returns 403)."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote_plus

from . import net
from .rag import KnowledgeBase, doc_id

ATOM = {"a": "http://www.w3.org/2005/Atom"}
# Anonymous RSS allows roughly 10 requests/minute per IP; faster bursts get 429s.
MIN_INTERVAL = 7.0
BACKOFF = 60.0
_last_call = 0.0

# Preset queries for `poe-advisor ingest --preset ...`.
PRESETS: dict[str, list[tuple[str, str, str]]] = {
    "money": [
        ("pathofexile", "currency farming strategy", "month"),
        ("pathofexile", "money making", "month"),
        ("pathofexile", "divines per hour", "month"),
        ("pathofexile", "profit atlas strategy", "month"),
        ("pathofexile", "crafting profit", "month"),
        ("pathofexile", "flipping currency", "month"),
        ("pathofexile", "best farming strategy league", "year"),
        ("PathOfExileBuilds", "farming build league starter", "month"),
    ],
    "builds": [
        ("PathOfExileBuilds", "league starter", "month"),
        ("PathOfExileBuilds", "budget upgrade path", "month"),
        ("PathOfExileBuilds", "defences tankiness", "month"),
        ("pathofexile", "build guide", "month"),
    ],
    "news": [
        ("pathofexile", "", "week"),
    ],
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("p", "br", "li", "tr", "h1", "h2", "h3", "h4", "pre"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(unescape(html or ""))
    text = "".join(parser.parts)
    text = re.sub(r"submitted by\s+/u/\S+.*$", "", text, flags=re.S)  # RSS footer boilerplate
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _fetch(url: str, backoff: bool = False) -> ET.Element:
    global _last_call
    wait = MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    try:
        text = net.get(url, parse="text", headers={"Accept": "application/atom+xml"}, cache_ttl=30 * 60)
    except net.RateLimited:
        if not backoff:
            raise
        time.sleep(BACKOFF)
        return _fetch(url, backoff=False)
    finally:
        _last_call = time.monotonic()
    return ET.fromstring(text)


def _entries(root: ET.Element) -> list[dict[str, Any]]:
    out = []
    for entry in root.findall("a:entry", ATOM):
        link = entry.find("a:link", ATOM)
        author = entry.find("a:author/a:name", ATOM)
        category = entry.find("a:category", ATOM)
        out.append({
            "id": (entry.findtext("a:id", default="", namespaces=ATOM) or "").strip(),
            "title": (entry.findtext("a:title", default="", namespaces=ATOM) or "").strip(),
            "url": link.get("href") if link is not None else "",
            "author": author.text if author is not None else "",
            "subreddit": category.get("term") if category is not None else "",
            "published": entry.findtext("a:published", default=None, namespaces=ATOM)
                         or entry.findtext("a:updated", default=None, namespaces=ATOM),
            "text": html_to_text(entry.findtext("a:content", default="", namespaces=ATOM)),
        })
    return out


def search(query: str, subreddit: str = "pathofexile", time_range: str = "month",
           sort: str = "top", limit: int = 15, patient: bool = False) -> list[dict[str, Any]]:
    """``patient`` waits out a 429 once (batch ingest); interactive callers fail fast instead."""
    if query:
        url = (f"https://www.reddit.com/r/{subreddit}/search.rss?q={quote_plus(query)}"
               f"&restrict_sr=on&sort={sort}&t={time_range}&limit={limit}")
    else:
        url = f"https://www.reddit.com/r/{subreddit}/top/.rss?t={time_range}&limit={limit}"
    return [e for e in _entries(_fetch(url, backoff=patient)) if e["id"].startswith("t3_")]


def comments(post_url: str, limit: int = 12, patient: bool = False) -> list[dict[str, Any]]:
    url = post_url.rstrip("/") + f"/.rss?limit={limit}&sort=top"
    return [e for e in _entries(_fetch(url, backoff=patient)) if e["id"].startswith("t1_") and len(e["text"]) > 40]


def to_document(post: dict[str, Any], top_comments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    body = post["text"]
    if top_comments:
        body += "\n\nTop comments:\n\n" + "\n\n".join(f"- {c['text']}" for c in top_comments)
    return {
        "id": doc_id("reddit", post["id"]),
        "title": post["title"],
        "url": post["url"],
        "source": f"reddit/r/{post.get('subreddit') or 'pathofexile'}",
        "published": post.get("published"),
        "text": f"{post['title']}\n\n{body}",
    }


def ingest(kb: KnowledgeBase, queries: list[tuple[str, str, str]], with_comments: bool = True,
           per_query: int = 10, log=print) -> int:
    """Fetch posts (and their top comments) for each (subreddit, query, time) and add them to the KB."""
    added = 0
    for subreddit, query, time_range in queries:
        try:
            posts = search(query, subreddit, time_range, limit=per_query, patient=True)
        except Exception as exc:  # one failing feed shouldn't abort the whole ingest
            log(f"  ! r/{subreddit} '{query}': {exc}")
            continue
        docs = []
        for post in posts[:per_query]:
            top = []
            if with_comments:
                try:
                    top = comments(post["url"], patient=True)
                except Exception as exc:
                    log(f"  ! comments for {post['url']}: {exc}")
            docs.append(to_document(post, top))
        new = kb.add(docs)
        added += new
        log(f"  r/{subreddit} '{query or 'top'}' ({time_range}): {len(posts)} posts, {new} new/updated")
    return added
