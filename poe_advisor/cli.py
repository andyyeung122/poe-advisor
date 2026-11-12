"""Command line: `python -m poe_advisor chat|analyze|price|market|ingest|kb|leagues`."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

from . import analysis, feeds, ninja, pob
from . import account as ggg
from .config import settings
from .rag import KnowledgeBase

console = Console()


def _apply_league(args: argparse.Namespace) -> None:
    if getattr(args, "league", None):
        console.print(f"[dim]League: {ninja.set_league(args.league)}[/dim]")


def _load_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    if args.pob:
        return pob.load(args.pob)
    acct, char = args.account or settings.account, args.character or settings.character
    if not (acct and char):
        sys.exit("Give --account and --character (or set POE_ACCOUNT/POE_CHARACTER), or --pob CODE.")
    try:
        return analysis.from_ninja(ninja.character(acct, char))
    except ninja.NinjaError as exc:
        console.print(f"[yellow]{exc}[/yellow]\nFalling back to pathofexile.com...")
        return analysis.from_ggg(ggg.get_items(acct, char), acct)


def cmd_leagues(args: argparse.Namespace) -> None:
    console.print_json(data={"current": ninja.current_league(), **ninja.list_leagues()})


def cmd_analyze(args: argparse.Namespace) -> None:
    _apply_league(args)
    snap = _load_snapshot(args)
    if args.json:
        console.print_json(data=analysis.summary(snap))
        return
    d = snap.get("defence") or {}
    main = snap.get("main_skill") or {}
    console.print(f"[bold]{snap.get('character') or 'PoB build'}[/bold]  {snap.get('ascendancy') or snap.get('class')} "
                  f"lvl {snap.get('level')}  ({snap['source']}, {snap.get('league') or ninja.current_league()})")
    dps = main.get("dps") or 0
    dot = main.get("dot_dps") or 0
    console.print(f"Main skill: {main.get('name')}  hit DPS {dps:,.0f}  DoT DPS {dot:,.0f}")
    keys = ["life", "energy_shield", "ehp", "fire_res", "cold_res", "lightning_res", "chaos_res", "suppression",
            "block", "spell_block", "armour", "evasion", "max_hit_physical", "max_hit_fire"]
    console.print("  ".join(f"{k}={d[k]}" for k in keys if k in d))
    if snap.get("defence_note"):
        console.print(f"[dim]{snap['defence_note']}[/dim]")
    table = Table("priority", "area", "finding", "typical fix", show_lines=False)
    for gap in analysis.find_gaps(snap):
        table.add_row(gap["priority"], gap["area"], gap["finding"], gap["typical_fix"])
    console.print(table)


def cmd_price(args: argparse.Namespace) -> None:
    _apply_league(args)
    rows = ninja.search_prices(args.query, [args.category] if args.category else None, limit=args.limit)
    divine = ninja.divine_in_chaos()
    table = Table("name", "category", "detail", "chaos", "divine", "listings/vol")
    for r in rows:
        detail = " ".join(str(x) for x in (r.get("variant"), r.get("links") and f"{r['links']}L",
                                           r.get("gem_level") and f"lvl {r['gem_level']}/{r.get('gem_quality', 0)}q",
                                           "corrupted" if r.get("corrupted") else None) if x)
        chaos = r.get("chaos") or 0
        table.add_row(r["name"], r["category"], detail, f"{chaos:,.1f}", f"{chaos / divine:,.2f}",
                      str(r.get("listings") or r.get("volume_chaos_per_hour") or ""))
    console.print(f"[dim]{ninja.current_league()} · 1 divine = {divine:,.0f}c[/dim]")
    console.print(table)


def cmd_market(args: argparse.Namespace) -> None:
    _apply_league(args)
    rows = ninja.market_movers(args.category, sort=args.sort, limit=args.limit, min_chaos=args.min_chaos)
    table = Table("name", "chaos", "7d change %", "volume/listings")
    for r in rows:
        table.add_row(r["name"], f"{r.get('chaos') or 0:,.1f}", f"{r.get('change_7d_pct') or 0:+.1f}",
                      str(r.get("volume_chaos_per_hour") or r.get("listings") or ""))
    console.print(f"[dim]{ninja.current_league()} · {args.category} by {args.sort}[/dim]")
    console.print(table)


def cmd_ingest(args: argparse.Namespace) -> None:
    kb = KnowledgeBase(settings.knowledge_path)
    queries: list[tuple[str, str, str]] = []
    for preset in args.preset or []:
        queries += feeds.PRESETS[preset]
    for q in args.query or []:
        queries.append((args.subreddit, q, args.time))
    if not queries:
        queries = feeds.PRESETS["money"]
    console.print(f"Ingesting {len(queries)} Reddit queries (about 3s per request to respect rate limits)...")
    added = feeds.ingest(kb, queries, with_comments=not args.no_comments, per_query=args.per_query,
                         log=console.print)
    console.print(f"[green]{added} documents added/updated.[/green] Knowledge base: {kb.stats()}")


def cmd_kb(args: argparse.Namespace) -> None:
    kb = KnowledgeBase(settings.knowledge_path)
    if args.kb_command == "stats":
        console.print_json(data=kb.stats())
        return
    for hit in kb.search(" ".join(args.query), k=args.k):
        console.print(f"[bold]{hit['title']}[/bold] [dim]({hit['age_days']}d, {hit['source']}, score {hit['score']})"
                      f"[/dim]\n{hit['url']}\n{hit['text'][:500]}\n")


HELP = """Commands: /league [name] (show or switch league) · /leagues · /build · /reset · /kb · /quit
Examples:
  load my character Name#1234 / CharName
  what's my next cost-effective upgrade for survivability and damage on a 5 divine budget?
  what are the best currency farming strategies right now?"""


def cmd_chat(args: argparse.Namespace) -> None:
    from .agent import Advisor  # imported lazily so non-chat commands don't need an API key

    _apply_league(args)
    advisor = Advisor(model=args.model, effort=args.effort, web_search=not args.no_web)
    console.print(f"[bold]PoE Advisor[/bold] · model {advisor.model} · league {ninja.current_league()} · "
                  f"knowledge base {len(advisor.toolbox.kb)} docs\n{HELP}\n")

    def on_tool(name: str, tool_args: dict[str, Any], is_error: bool) -> None:
        shown = json.dumps(tool_args, ensure_ascii=False)
        console.print(f"[dim]  {'✗' if is_error else '→'} {name} {shown[:140]}[/dim]")

    def on_status(text: str) -> None:
        console.print(f"[dim]  … {text}[/dim]")

    first: str | None = None
    if args.pob:
        first = f"Load this Path of Building build and summarize it with its weakest points: {args.pob}"
    elif (args.account or settings.account) and (args.character or settings.character):
        first = (f"Load my character {args.account or settings.account} / {args.character or settings.character} "
                 "and summarize it with its weakest points.")

    while True:
        if first:
            text, first = first, None
            console.print(f"[bold cyan]you>[/bold cyan] {text}")
        else:
            try:
                text = console.input("[bold cyan]you>[/bold cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/help":
            console.print(HELP)
            continue
        if text == "/reset":
            advisor.reset()
            console.print("[dim]Conversation cleared (active build kept).[/dim]")
            continue
        if text == "/leagues":
            console.print_json(data={"current": ninja.current_league(), **ninja.list_leagues()})
            continue
        if text.startswith("/league"):
            name = text[len("/league"):].strip()
            try:
                current = ninja.set_league(name) if name else ninja.current_league()
                console.print(f"[dim]League: {current}[/dim]")
            except ninja.NinjaError as exc:
                console.print(f"[red]{exc}[/red]")
            continue
        if text == "/build":
            console.print_json(data=advisor.toolbox.active_build())
            continue
        if text == "/kb":
            console.print_json(data=advisor.toolbox.kb.stats())
            continue
        console.print("[bold green]advisor>[/bold green] ", end="")
        try:
            advisor.ask(text, on_text=lambda s: console.out(s, end="", highlight=False),
                        on_status=on_status, on_tool=on_tool)
        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted.[/dim]")
        except Exception as exc:
            console.print(f"\n[red]{type(exc).__name__}: {exc}[/red]")
            if "authentication" in str(exc).lower() or "api key" in str(exc).lower():
                console.print("[yellow]Set ANTHROPIC_API_KEY in .env (see .env.example).[/yellow]")
        console.print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="poe-advisor", description="Path of Exile upgrade and economy advisor")
    sub = parser.add_subparsers(dest="command")

    def add_league(p: argparse.ArgumentParser) -> None:
        p.add_argument("--league", help="League to query (default: POE_LEAGUE or current challenge league)")

    def add_build(p: argparse.ArgumentParser) -> None:
        p.add_argument("--account", help="Account name, e.g. Name#1234")
        p.add_argument("--character")
        p.add_argument("--pob", help="Path of Building code or pobb.in/pastebin link")

    p = sub.add_parser("chat", help="Talk to the advisor (default)")
    add_league(p); add_build(p)
    p.add_argument("--model", help=f"Claude model (default {settings.model})")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--no-web", action="store_true", help="Disable Claude's web search/fetch tools")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("analyze", help="Print a build's stats and gaps (no LLM)")
    add_league(p); add_build(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("price", help="poe.ninja price lookup")
    add_league(p)
    p.add_argument("query")
    p.add_argument("--category", choices=ninja.ALL_TYPES)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_price)

    p = sub.add_parser("market", help="Top items in a poe.ninja category")
    add_league(p)
    p.add_argument("category", choices=ninja.ALL_TYPES)
    p.add_argument("--sort", choices=["value", "rising", "falling", "volume"], default="value")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--min-chaos", type=float, default=1.0)
    p.set_defaults(func=cmd_market)

    p = sub.add_parser("ingest", help="Pull Reddit posts into the knowledge base")
    p.add_argument("--preset", action="append", choices=sorted(feeds.PRESETS))
    p.add_argument("--query", action="append", help="Custom search (repeatable)")
    p.add_argument("--subreddit", default="pathofexile")
    p.add_argument("--time", default="month", choices=["day", "week", "month", "year", "all"])
    p.add_argument("--per-query", type=int, default=10)
    p.add_argument("--no-comments", action="store_true")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("kb", help="Inspect the knowledge base")
    kb_sub = p.add_subparsers(dest="kb_command", required=True)
    kb_sub.add_parser("stats")
    s = kb_sub.add_parser("search")
    s.add_argument("query", nargs="+")
    s.add_argument("-k", type=int, default=5)
    p.set_defaults(func=cmd_kb)

    p = sub.add_parser("leagues", help="List leagues poe.ninja tracks")
    p.set_defaults(func=cmd_leagues)

    args = parser.parse_args(argv)
    if not args.command:
        args = parser.parse_args(["chat", *(argv or sys.argv[1:])])
    args.func(args)


if __name__ == "__main__":
    main()
