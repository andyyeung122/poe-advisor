"""Offline tests: no network, no API key."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from poe_advisor import analysis, feeds, net, ninja, pob, trade
from poe_advisor.rag import KnowledgeBase, chunk

POB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build className="Ranger" ascendClassName="Deadeye" level="92" mainSocketGroup="1">
    <PlayerStat stat="Life" value="4200"/>
    <PlayerStat stat="FireResist" value="68"/>
    <PlayerStat stat="ColdResist" value="75"/>
    <PlayerStat stat="LightningResist" value="75"/>
    <PlayerStat stat="ChaosResist" value="-20"/>
    <PlayerStat stat="Evasion" value="30000"/>
    <PlayerStat stat="Armour" value="2000"/>
    <PlayerStat stat="EffectiveSpellSuppressionChance" value="62"/>
    <PlayerStat stat="TotalDPS" value="1500000"/>
  </Build>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill slot="Body Armour" enabled="true">
        <Gem nameSpec="Tornado Shot" level="19" quality="20" skillId="TornadoShot" gemId="Metadata/Items/Gems/SkillGemTornadoShot"/>
        <Gem nameSpec="Mirage Archer" level="20" quality="0" skillId="SupportMirageArcher" gemId="Metadata/Items/Gems/SupportGemMirageArcher"/>
        <Gem nameSpec="Elemental Damage with Attacks" level="18" quality="0" skillId="SupportElementalDamageWithAttacks" gemId="Metadata/Items/Gems/SupportGemElementalDamageWithAttacks"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1">
    <Item id="1">Rarity: RARE
Doom Visor
Lion Pelt
Item Level: 84
Sockets: G-G-R-B
Implicits: 0
+90 to maximum Life
+30% to Fire Resistance
{crafted}+25% to Cold Resistance</Item>
    <Item id="2">Rarity: RARE
Ghoul Beads
Onyx Amulet
Implicits: 1
+12 to all Attributes
+70 to maximum Life</Item>
    <ItemSet id="1">
      <Slot name="Helmet" itemId="1"/>
      <Slot name="Amulet" itemId="2"/>
    </ItemSet>
  </Items>
</PathOfBuilding>"""


def test_pob_roundtrip_and_parse():
    snap = pob.parse(pob.decode(pob.encode(POB_XML)))
    assert snap["ascendancy"] == "Deadeye" and snap["level"] == 92
    assert snap["defence"]["fire_res"] == 68 and snap["defence"]["suppression"] == 62
    assert snap["main_skill"]["name"] == "Tornado Shot"
    assert snap["main_skill"]["dps"] == 1500000
    helmet = next(i for i in snap["items"] if i["slot"] == "helmet")
    assert helmet["base"] == "Lion Pelt" and helmet["max_links"] == 4
    assert helmet["crafted"] == ["+25% to Cold Resistance"]
    assert "+90 to maximum Life" in helmet["explicit"]


def test_gaps_prioritise_uncapped_resistance():
    gaps = analysis.find_gaps(pob.parse(POB_XML))
    assert gaps[0]["priority"] == "critical" and "Fire resistance" in gaps[0]["finding"]
    findings = " ".join(g["finding"] for g in gaps)
    assert "Chaos resistance -20%" in findings
    assert "Spell suppression 62%" in findings
    assert "Tornado Shot is level 19" in findings
    assert "Amulet is not anointed" in findings
    assert "3-gem group" in findings


def test_gear_totals_sum_hybrid_resistances():
    items = [{"explicit": ["+20% to Fire and Cold Resistances", "+10% to all Elemental Resistances",
                           "+15% to Chaos Resistance", "+60 to maximum Life"]}]
    totals = analysis.gear_totals(items)
    assert totals["fire_res_from_gear"] == 30 and totals["cold_res_from_gear"] == 30
    assert totals["lightning_res_from_gear"] == 10 and totals["chaos_res_from_gear"] == 15
    assert totals["flat_life_from_gear"] == 60


def test_rate_limiter_waits_when_window_full():
    limiter = net.RateLimiter()
    limiter._rules["p"] = [(3, 10)]
    limiter._hits["p"].extend([100.0, 101.0])
    assert limiter.wait_time("p", now=102.0) == pytest.approx(10 - 2 + 0.25)
    assert limiter.wait_time("p", now=111.0) == 0.0


def test_rate_limiter_parses_ggg_headers():
    limiter = net.RateLimiter()
    headers = net.httpx.Headers({"x-rate-limit-rules": "Ip", "x-rate-limit-ip": "5:10:60,15:60:300"})
    limiter.update("trade-search", headers)
    assert limiter._rules["trade-search"] == [(5, 10), (15, 60)]


def test_knowledge_base_prefers_recent(tmp_path):
    now = datetime.now(timezone.utc)
    kb = KnowledgeBase(tmp_path / "kb.jsonl")
    kb.add([
        {"id": "old", "title": "Harvest farming", "url": "u1", "source": "reddit",
         "published": (now - timedelta(days=400)).isoformat(), "text": "harvest farming strategy profit lifeforce"},
        {"id": "new", "title": "Harvest farming", "url": "u2", "source": "reddit",
         "published": (now - timedelta(days=3)).isoformat(), "text": "harvest farming strategy profit lifeforce"},
    ])
    hits = kb.search("harvest farming profit")
    assert [h["url"] for h in hits] == ["u2", "u1"]
    assert kb.search("harvest", max_age_days=30)[0]["url"] == "u2"
    assert len(KnowledgeBase(tmp_path / "kb.jsonl")) == 2  # persisted


def test_search_knowledge_current_league_only(tmp_path, monkeypatch):
    from poe_advisor.tools import Toolbox

    now = datetime.now(timezone.utc)
    kb = KnowledgeBase(tmp_path / "kb.jsonl")
    kb.add([
        {"id": "pre", "title": "Scarab farming", "url": "u1", "source": "reddit",
         "published": (now - timedelta(days=10)).isoformat(), "text": "atlas farming strategy profit"},
        {"id": "post", "title": "Thread farming", "url": "u2", "source": "reddit",
         "published": (now - timedelta(days=1)).isoformat(), "text": "atlas farming strategy profit"},
        {"id": "undated", "title": "Farming", "url": "u3", "source": "guide", "published": None,
         "text": "atlas farming strategy profit"},
    ])
    toolbox = Toolbox(kb)
    monkeypatch.setattr(ninja, "league_start", lambda league=None: now - timedelta(days=3))

    filtered = toolbox.search_knowledge("atlas farming", current_league_only=True)
    assert [h["url"] for h in filtered["results"]] == ["u2"]
    assert filtered["results"][0]["before_league_start"] is False

    unfiltered = {h["url"]: h["before_league_start"] for h in toolbox.search_knowledge("atlas farming")["results"]}
    assert unfiltered == {"u1": True, "u2": False, "u3": True}

    monkeypatch.setattr(ninja, "league_start", lambda league=None: None)
    fallback = toolbox.search_knowledge("atlas farming", current_league_only=True)
    assert len(fallback["results"]) == 3 and "not filtered" in fallback["hint"]


def test_item_history_compares_current_and_past_leagues(monkeypatch):
    def row(item_id, chaos=10.0):
        return {"item_id": item_id, "name": "Divine Orb", "category": "Currency", "chaos": chaos, "volume_chaos_per_hour": 5}

    def fake_overview(category, league=None):
        return [row("divine-orb")] if league != "Ghost" else []

    def fake_get(url, **kwargs):
        league = kwargs["params"]["league"]
        if league == "Now":  # 10 days, price doubles in week 2
            hist = [{"timestamp": f"2026-11-{d:02d}T00:00:00Z", "rate": 100 if d < 8 else 200} for d in range(1, 11)]
        else:  # past league, newest first like the real API: 100 -> 50
            hist = [{"timestamp": f"2026-03-{d:02d}T00:00:00Z", "rate": 100 if d < 8 else 50} for d in range(14, 0, -1)]
        return {"pairs": [{"id": "chaos", "history": hist}, {"id": "divine", "history": []}]}

    monkeypatch.setattr(ninja, "search_prices", lambda *a, **k: [row("divine-orb")])
    monkeypatch.setattr(ninja, "overview", fake_overview)
    monkeypatch.setattr(ninja, "list_leagues", lambda *a, **k: {"old_economy": ["Ghost", "Hardcore Old", "Then"]})
    monkeypatch.setattr(net, "get", fake_get)

    out = ninja.item_history("Divine Orb", league="Now", past_leagues=3)
    now, ghost, then = out["leagues"]  # Hardcore leagues are skipped
    assert now["status"] == "requested" and now["days_of_data"] == 10
    assert [w["pct_vs_week1"] for w in now["weekly"]] == [0.0, 100.0]
    assert now["peak"]["week"] == 2 and now["trough"]["week"] == 1
    assert "not priced" in ghost["error"]
    assert then["league"] == "Then" and then["weekly"][-1]["pct_vs_week1"] == -50.0
    assert then["trough"]["week"] == 2


class FakeTurso:
    """Stands in for Turso's HTTP API: runs the statements on in-memory SQLite and answers in Hrana format."""

    def __init__(self):
        import sqlite3

        self.db = sqlite3.connect(":memory:")
        self.requests = 0
        self.auth: list[str] = []

    def post(self, url, json, headers):
        assert url.endswith("/v2/pipeline") and url.startswith("https://")
        self.auth.append(headers["Authorization"])
        self.requests += 1
        results = []
        for req in json["requests"]:
            if req["type"] != "execute":
                continue
            args = [None if a["type"] == "null" else int(a["value"]) if a["type"] == "integer" else a["value"]
                    for a in req["stmt"]["args"]]
            cur = self.db.execute(req["stmt"]["sql"], args)
            cols = [{"name": d[0]} for d in cur.description or []]

            def cell(v):
                if v is None:
                    return {"type": "null"}
                if isinstance(v, int):
                    return {"type": "integer", "value": str(v)}
                if isinstance(v, float):
                    return {"type": "float", "value": v}
                return {"type": "text", "value": v}

            rows = [[cell(v) for v in row] for row in cur.fetchall()]
            results.append({"type": "ok", "response": {"type": "execute", "result": {"cols": cols, "rows": rows}}})
        self.db.commit()

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"results": results + [{"type": "ok", "response": {"type": "close"}}]}

        return Resp()


def _history_fixture(monkeypatch, fetches):
    row = {"item_id": "divine-orb", "name": "Divine Orb", "category": "Currency", "chaos": 10.0}
    ttls: list[tuple[str, float]] = []

    def fake_get(url, **kwargs):
        league = kwargs["params"]["league"]
        fetches.append(league)
        ttls.append((league, kwargs.get("cache_ttl", 0)))
        hist = [{"timestamp": f"2026-03-{d:02d}T00:00:00Z", "rate": 100 if d < 8 else 50} for d in range(1, 15)]
        return {"pairs": [{"id": "chaos", "history": hist}]}

    monkeypatch.setattr(ninja, "search_prices", lambda *a, **k: [row])
    monkeypatch.setattr(ninja, "overview", lambda category, league=None: [row])
    monkeypatch.setattr(ninja, "list_leagues", lambda *a, **k: {"old_economy": ["Then"]})
    monkeypatch.setattr(net, "get", fake_get)
    return ttls


def test_past_league_history_is_saved_then_read_from_store(monkeypatch):
    from poe_advisor.history_store import HistoryStore

    fetches: list[str] = []
    ttls = _history_fixture(monkeypatch, fetches)
    fake = FakeTurso()
    store = HistoryStore("libsql://db-org.turso.io", "secret", http=fake)

    first = ninja.item_history("Divine Orb", league="Now", past_leagues=1, store=store)
    assert fetches == ["Now", "Then"] and store.count() == 1
    assert ("Then", 0) in ttls  # the store holds it, so no disk copy
    assert set(fake.auth) == {"Bearer secret"}

    fetches.clear()
    second = ninja.item_history("Divine Orb", league="Now", past_leagues=1, store=store)
    assert fetches == ["Now"]  # "Then" came from Turso: no poe.ninja call
    assert second["leagues"][1]["source"] == "turso"
    assert second["leagues"][1]["weekly"] == first["leagues"][1]["weekly"]
    assert second["leagues"][1]["peak"] == first["leagues"][1]["peak"]


def test_past_league_history_is_disk_cached_without_a_store(monkeypatch):
    fetches: list[str] = []
    ttls = _history_fixture(monkeypatch, fetches)
    ninja.item_history("Divine Orb", league="Now", past_leagues=1)
    assert ("Then", ninja.HISTORY_TTL_PAST) in ttls


def test_history_store_search_filters_by_shape():
    from poe_advisor.history_store import HistoryStore

    store = HistoryStore("https://db.turso.io", "t", http=FakeTurso())

    def summary(league, pcts):
        weekly = [{"week": i + 1, "median": 100 + p, "days": 7, "pct_vs_week1": p} for i, p in enumerate(pcts)]
        top = max(weekly, key=lambda w: w["median"])
        low = min(weekly, key=lambda w: w["median"])
        return {"league": league, "unit": "chaos", "days_of_data": 7 * len(pcts), "latest": 1.0,
                "peak": {"week": top["week"], "median": top["median"]},
                "trough": {"week": low["week"], "median": low["median"]}, "weekly": weekly}

    store.save("Crasher", "Currency", None, summary("A", [0, 80, 40, -30, -50]))
    store.save("Climber", "Currency", None, summary("A", [0, 20, 50, 90, 160]))
    store.save("Thin", "Currency", None, summary("A", [0, 10]))  # under min_weeks

    early = store.search(peak_by_week=3, final_pct_max=-30)
    assert [r["item"] for r in early] == ["Crasher"]
    late = store.search(peak_after_week=3, final_pct_min=100, category="Currency")
    assert [r["item"] for r in late] == ["Climber"] and late[0]["peak_pct"] == 160
    assert store.count() == 3 and store.get("A", "Currency", "Thin", None)["weekly"][1]["pct_vs_week1"] == 10


def test_store_error_response_and_outage_fall_back_to_poe_ninja(monkeypatch):
    from poe_advisor.history_store import HistoryStore

    class Down:
        def post(self, *a, **k):
            raise RuntimeError("503 service unavailable")

    fetches: list[str] = []
    ttls = _history_fixture(monkeypatch, fetches)
    out = ninja.item_history("Divine Orb", league="Now", past_leagues=1,
                             store=HistoryStore("https://db.turso.io", "t", http=Down()))
    assert "error" not in out["leagues"][1] and any("store read failed" in n for n in out["notes"])
    assert ("Then", ninja.HISTORY_TTL_PAST) in ttls  # store dropped, so the disk cache applies again


def test_history_search_requires_configuration(monkeypatch, tmp_path):
    from poe_advisor import history_store
    from poe_advisor.tools import Toolbox

    monkeypatch.setattr(history_store, "configured", lambda: False)
    text, is_error = Toolbox(KnowledgeBase(tmp_path / "kb.jsonl")).run("history_search", {})
    assert is_error and "not configured" in text


def test_item_history_stash_series_uses_days_ago(monkeypatch):
    monkeypatch.setattr(net, "get", lambda url, **k: [{"count": 1, "value": 30, "daysAgo": 0},
                                                      {"count": 1, "value": 10, "daysAgo": 2}])
    unit, points = ninja._series("UniqueAccessory", 1, "Now", past=False)
    assert unit == "chaos" and [(p["day"], p["value"]) for p in points] == [(0, 10), (2, 30)]


def test_chunking_splits_long_text():
    pieces = chunk("\n\n".join(["word " * 100] * 10), size=1200)
    assert len(pieces) > 1 and all(len(p) <= 1300 for p in pieces)


def test_reddit_html_cleanup():
    html = "&lt;p&gt;Run &lt;b&gt;T17&lt;/b&gt; maps&lt;/p&gt; submitted by /u/someone [link] [comments]"
    assert feeds.html_to_text(html) == "Run T17 maps"


def test_trade_query_shape():
    body = trade.build_query(category="armour.helmet", rarity="rare", max_price_chaos=50,
                             stats=[{"stat": "pseudo.pseudo_total_life", "min": 80}])
    q = body["query"]
    assert q["filters"]["type_filters"]["filters"]["category"] == {"option": "armour.helmet"}
    assert q["filters"]["trade_filters"]["filters"]["price"] == {"max": 50}
    assert q["stats"][0]["filters"] == [{"id": "pseudo.pseudo_total_life", "value": {"min": 80}}]
    with pytest.raises(trade.TradeError):
        trade.build_query(category="armour.hat")


def test_set_league(monkeypatch):
    state = {"economyLeagues": [{"name": "Allflame", "url": "allflame"}, {"name": "Standard", "url": "standard"}],
             "buildLeagues": [{"name": "Allflame", "url": "allflame"}, {"name": "Private (PL123)", "url": "x"}]}
    monkeypatch.setattr(ninja, "index_state", lambda: state)
    assert ninja.current_league() in ("Allflame", ninja.settings.league)
    assert ninja.set_league("standard") == "Standard"
    assert ninja.current_league() == "Standard"
    assert "Private (PL123)" not in ninja.list_leagues()["builds"]
    with pytest.raises(ninja.NinjaError):
        ninja.set_league("Nope")
    ninja.set_league(None)


def test_tool_input_validation():
    from poe_advisor.tools import Toolbox

    content, is_error = Toolbox().run("price_check", {"query": "x", "limit": 999})
    assert is_error and "Invalid input" in content


class _FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for block in self.message.content:
            if block.type == "text":
                yield SimpleNamespace(type="text", text=block.text)

    def get_final_message(self):
        return self.message


def test_agent_loop_runs_tools_and_keeps_history_valid(monkeypatch):
    from poe_advisor.agent import Advisor

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(ninja, "current_league", lambda: "Allflame")
    turns = [
        SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="text", text="Checking."),
            SimpleNamespace(type="tool_use", id="t1", name="leagues", input={}),
            SimpleNamespace(type="tool_use", id="t2", name="search_knowledge", input={"query": "farming"}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=" Done.")]),
    ]
    advisor = Advisor(web_search=False)
    sent = []

    def fake_stream(**kwargs):
        sent.append(json.loads(json.dumps(kwargs["messages"], default=lambda o: "<block>")))
        return _FakeStream(turns.pop(0))

    monkeypatch.setattr(advisor.client.beta.messages, "stream", fake_stream)
    monkeypatch.setattr(advisor.toolbox, "_handlers", {
        **advisor.toolbox._handlers, "leagues": lambda: {"current": "Allflame"},
    })
    out = []
    advisor.ask("hi", on_text=out.append)
    assert "".join(out) == "Checking. Done."
    roles = [m["role"] for m in advisor.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    results = advisor.messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]  # both results in one message
    assert sent[0][0]["content"].startswith("[session: date")


def test_mcp_server_protocol():
    import io

    from poe_advisor.mcp_server import Server
    from poe_advisor.tools import Toolbox

    toolbox = Toolbox()
    toolbox._handlers["leagues"] = lambda: {"current": "Allflame"}
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "leagues", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "price_check", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "bogus"},
    ]
    stdin = io.BytesIO("".join(json.dumps(r) + "\n" for r in requests).encode())
    stdout = io.BytesIO()
    Server(toolbox, stdout).serve(stdin)
    replies = {r["id"]: r for r in map(json.loads, stdout.getvalue().decode().splitlines())}
    assert set(replies) == {1, 2, 3, 4, 5}  # the notification got no reply
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert any(t["name"] == "trade_search" and "inputSchema" in t for t in replies[2]["result"]["tools"])
    assert replies[3]["result"] == {"content": [{"type": "text", "text": '{"current": "Allflame"}'}], "isError": False}
    assert replies[4]["result"]["isError"] is True
    assert replies[5]["error"]["code"] == -32601
