"""Local knowledge base for retrieval: BM25 over chunked guides/posts, weighted toward recent content.

Path of Exile's economy and meta reset every league, so a farming strategy from three
leagues ago can be actively wrong. Scores are blended with an age decay (90-day half-life).
BM25 also suits the domain: retrieval hinges on exact jargon ("Harvest", "T17", "Mageblood")
that embedding models tend to blur.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from rank_bm25 import BM25Plus

HALF_LIFE_DAYS = 90
CHUNK_CHARS = 1200
STOPWORDS = set(
    "a an and are as at be but by for from has have i if in into is it its me my of on or so that the their them "
    "then there these they this to was we were what when which who will with you your just like can do does dont "
    "get got im ive not no yes all any some more most very".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9'+]*", text.lower()) if t not in STOPWORDS]


def chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        while len(para) > size:  # hard-split very long paragraphs
            chunks.append((current + "\n\n" + para[:size]).strip())
            current, para = "", para[size:]
        if len(current) + len(para) > size and current:
            chunks.append(current.strip())
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current.strip():
        chunks.append(current.strip())
    return chunks or [text[:size]]


def parse_date(published: str | None) -> datetime | None:
    if not published:
        return None
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_days(published: str | None) -> float | None:
    dt = parse_date(published)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)


@dataclass
class Chunk:
    doc_id: str
    title: str
    url: str
    source: str
    published: str | None
    text: str


class KnowledgeBase:
    def __init__(self, path: Path):
        self.path = path
        self.docs: dict[str, dict[str, Any]] = {}
        self._chunks: list[Chunk] = []
        self._bm25: BM25Plus | None = None
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    doc = json.loads(line)
                    self.docs[doc["id"]] = doc  # later lines win, so re-ingesting refreshes a doc

    def __len__(self) -> int:
        return len(self.docs)

    def add(self, docs: Iterable[dict[str, Any]]) -> int:
        """Add or refresh documents ({id, title, url, source, published, text}). Returns how many changed."""
        changed = []
        for doc in docs:
            if not doc.get("text", "").strip():
                continue
            old = self.docs.get(doc["id"])
            if old and old.get("text") == doc["text"]:
                continue
            self.docs[doc["id"]] = doc
            changed.append(doc)
        if changed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                for doc in changed:
                    fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
            self._bm25 = None
        return len(changed)

    def _index(self) -> None:
        self._chunks = []
        for doc in self.docs.values():
            for piece in chunk(doc["text"]):
                self._chunks.append(Chunk(doc["id"], doc.get("title", ""), doc.get("url", ""),
                                          doc.get("source", ""), doc.get("published"), piece))
        corpus = [tokenize(f"{c.title} {c.title} {c.text}") for c in self._chunks]  # title counted twice
        self._bm25 = BM25Plus(corpus) if corpus else None

    def search(self, query: str, k: int = 6, max_age_days: float | None = None,
               since: datetime | None = None) -> list[dict[str, Any]]:
        """BM25 search blended with recency. ``since`` drops passages published before it, and undated ones."""
        if self._bm25 is None:
            self._index()
        if self._bm25 is None:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = []
        for score, piece in zip(scores, self._chunks):
            if score <= 0:
                continue
            age = _age_days(piece.published)
            if max_age_days is not None and age is not None and age > max_age_days:
                continue
            if since is not None:
                published = parse_date(piece.published)
                if published is None or published < since:
                    continue
            decay = 0.5 ** (age / HALF_LIFE_DAYS) if age is not None else 0.5
            ranked.append((score * (0.5 + 0.5 * decay), age, piece))
        ranked.sort(key=lambda r: -r[0])

        results, per_doc = [], {}
        for score, age, piece in ranked:
            if per_doc.get(piece.doc_id, 0) >= 2:  # keep results diverse across posts
                continue
            per_doc[piece.doc_id] = per_doc.get(piece.doc_id, 0) + 1
            results.append({
                "title": piece.title, "url": piece.url, "source": piece.source,
                "published": piece.published, "age_days": round(age) if age is not None else None,
                "score": round(score, 2), "text": piece.text,
            })
            if len(results) >= k:
                break
        return results

    def stats(self) -> dict[str, Any]:
        sources: dict[str, int] = {}
        for doc in self.docs.values():
            sources[doc.get("source", "?")] = sources.get(doc.get("source", "?"), 0) + 1
        dates = sorted(d["published"] for d in self.docs.values() if d.get("published"))
        return {"documents": len(self.docs), "by_source": sources,
                "oldest": dates[0] if dates else None, "newest": dates[-1] if dates else None}


def doc_id(*parts: str) -> str:
    return ":".join(parts)
