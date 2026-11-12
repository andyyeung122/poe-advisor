# PoE Advisor

A chat agent for Path of Exile 1. Point it at your character and ask for your next cost-effective upgrade for
survivability and damage, or for the money-making strategies that pay right now. It answers from live data:

| Source | Used for | Notes |
|---|---|---|
| **poe.ninja** | Prices (currency, uniques, gems, scarabs, cards, essences…), market movers, indexed characters with DPS/EHP/max-hit | No key. Only characters poe.ninja has indexed (ladder-eligible, public profile). |
| **Official trade API** (pathofexile.com/trade) | Pricing rare upgrades with exact stat filters | No key. GGG rate limits it; the client tracks the `X-Rate-Limit-*` headers. poe.trade shut down in 2020. |
| **pathofexile.com character API** | Your gear and gems when poe.ninja hasn't indexed you | Needs a public Characters tab, or `POESESSID`. Gear only, no computed stats. |
| **Path of Building** codes / pobb.in / pastebin | The most accurate build input (PoB's own DPS/EHP numbers) | Paste the code or link in chat. |
| **Reddit** (RSS) | Knowledge base of strategy posts and top comments (the RAG part) | Anonymous RSS, about 10 requests/min. |
| **Claude web search/fetch** | Fresh guides and patch notes when the knowledge base is thin | Billed per search; turn off with `--no-web`. |

## How it works

```
you ──▶ Claude (claude-opus-5-5, tool use) ──▶ tools
                                               ├─ load_character / load_pob ──▶ normalized build + gap list
                                               ├─ price_check / market_overview ──▶ poe.ninja
                                               ├─ find_trade_stat / trade_search ──▶ pathofexile.com/trade
                                               ├─ search_knowledge ──▶ local BM25 index (data/knowledge.jsonl)
                                               ├─ reddit_search ──▶ Reddit RSS (also adds to the index)
                                               └─ web_search / web_fetch (server-side)
```

1. **Build snapshot.** poe.ninja, pathofexile.com and PoB data are normalized into one shape (`analysis.py`).
2. **Gap finder.** A deterministic pass flags objective weaknesses: uncapped resistances, negative chaos res, a low
   life pool or max hit for your level, uncapped suppression on evasion builds, missing flask immunities,
   under-levelled gems, small links, a missing anoint, rares with possible open affixes.
3. **Pricing.** Claude turns each gap into a concrete purchase. It prices uniques and gems on poe.ninja and rares
   on the trade site, keeping your current item's life and resistances as minimums so a swap doesn't open a
   new hole.
4. **Ranking.** It ranks options by improvement per divine and gives a top 3 for survivability and for damage, each
   with a trade link or craft steps.
5. **Retrieval.** The knowledge base ranks by BM25 blended with a 90-day half-life on post age, since a farming
   strategy from three leagues ago can be wrong now. BM25 suits the domain, where queries hinge on exact jargon
   ("T17", "Harvest", "Mageblood").

## Setup (Windows, Python 3.10+)

```bash
cd C:\Users\hqcql\Documents\Projects\poe-advisor
```

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

```bash
copy .env.example .env
```

Then edit `.env`: set `POE_ACCOUNT` (e.g. `Name#1234`), `POE_CHARACTER`, and `POE_ADVISOR_CONTACT`, plus
`ANTHROPIC_API_KEY` only if you use the API chat.

> HTTPS on this machine: Python's bundled certificates fail verification here, probably due to antivirus HTTPS
> scanning or a proxy. The app calls `truststore` to use the Windows certificate store, and pip 24.2+ does the
> same. The pip that ships with Python 3.10.0 is older, so if a fresh venv's first install fails with
> `CERTIFICATE_VERIFY_FAILED`, run `python scripts\export_windows_certs.py`, then
> `.venv\Scripts\python -m pip install --cert ca-bundle.pem --upgrade pip`, then retry the install.
> (The existing `.venv` is already upgraded.)

## Two ways to chat

| | Claude Code (uses your Claude Pro/Max plan) | `python -m poe_advisor chat` (API) |
|---|---|---|
| Pays with | Your subscription's usage limits, no extra charge | Anthropic API credit (pay per token) |
| Needs | Claude Code, this folder opened as a session | `ANTHROPIC_API_KEY` in `.env` |
| Tools | Same tools, through the `poe-advisor` MCP server | Same tools, called directly |

Both follow the same advisor rules (`poe_advisor/advisor_prompt.md`).

### Claude Code (Claude Pro)

1. Do the setup below. Leave `ANTHROPIC_API_KEY` empty; Claude Code doesn't need it.
2. In the Claude desktop app, open a new Code session in `C:\Users\hqcql\Documents\Projects\poe-advisor`.
   Claude Code reads `CLAUDE.md` and starts the MCP server listed in `.mcp.json`.
3. Approve the `poe-advisor` server when asked (or run `/mcp`), and choose "always allow" for its tools.
4. Ask: `Load my character and tell me my next cost-effective upgrades on a 5 divine budget.`

Optional: to skip those approval prompts and stop Claude Code from ever reading `.env`, create
`.claude/settings.json` with:

```json
{
  "enabledMcpjsonServers": ["poe-advisor"],
  "permissions": {
    "allow": ["mcp__poe-advisor"],
    "deny": ["Read(./.env)"]
  }
}
```

`.mcp.json` contains this machine's absolute path to `.venv`. Update it if you move the folder.

## Use it

Build the knowledge base once, then refresh it every few days during a league (takes about 10 minutes,
because of Reddit's rate limit):

```bash
.venv\Scripts\python -m poe_advisor ingest --preset money --preset builds
```

Chat:

```bash
.venv\Scripts\python -m poe_advisor chat
```

```bash
.venv\Scripts\python -m poe_advisor chat --pob https://pobb.in/XXXXXXXX
```

Things to ask:

- `What's my next cost-effective upgrade for survivability and damage? Budget 5 divines.`
- `Is a 21/20 of my main skill worth it compared to fixing my chaos res?`
- `What are the best ways to make currency right now for my build?`
- `Which scarabs are rising this week, and what strategy uses them?`
- `Price check Mageblood 4 flask` / `What does an Awakened Multistrike cost in Standard?`

Chat commands: `/league [name]`, `/leagues`, `/build`, `/kb`, `/reset`, `/quit`.

These commands work without an API key:

```bash
.venv\Scripts\python -m poe_advisor analyze --account "Name#1234" --character MyChar
```

```bash
.venv\Scripts\python -m poe_advisor price "Headhunter"
```

```bash
.venv\Scripts\python -m poe_advisor market Scarab --sort rising
```

```bash
.venv\Scripts\python -m poe_advisor kb search harvest profit
```

```bash
.venv\Scripts\python -m poe_advisor leagues
```

## Choosing the league

Every lookup uses one session league, picked in this order:

1. `/league <name>` in chat, or simply telling the agent ("check this in Standard"), which calls `set_league`
2. `--league "<name>"` on any command
3. `POE_LEAGUE` in `.env`
4. Otherwise, the current softcore trade challenge league reported by poe.ninja

Individual tools also accept a `league` argument, so the agent can compare one price across leagues without
switching. `leagues` lists what poe.ninja tracks (private leagues are hidden from the list but can still be
selected by name). SSF and Ruthless leagues have character data but no trade economy.

## Cost

In Claude Code, questions count against your Claude plan's usage limits; trade-heavy sessions use more.

The API chat uses `claude-opus-5-5` at `medium` effort by default ($4 / $20 per million input/output tokens; prompt caching
cuts the cost of repeated context). An upgrade question that loads a build and runs a few price and trade lookups
usually costs a few cents to a few tens of cents. Set `POE_ADVISOR_MODEL=claude-sonnet-5-5` or `--effort low` to
spend less.

## Limits

- The gap finder is rule-based and the agent's DPS-gain figures are estimates. Only Path of Building computes
  exact before/after numbers; paste a candidate item into PoB to confirm before a big purchase.
- poe.ninja `character` 404s mean it hasn't indexed you. Use a PoB code (PoB → Import → your account).
- Trade listings priced far below market are often stale or price-fixed; the agent is told to treat them carefully.
- Path of Exile 1 only. poe.ninja's PoE 2 endpoints are a different API.

## Ideas for later

- Run Path of Building headless (its Lua engine) to compute exact DPS/EHP for each candidate item.
- Reddit OAuth (100 requests/min) for faster ingestion, plus poewiki and patch notes as sources.
- Embedding search alongside BM25 once the knowledge base is large.

## Tests

```bash
.venv\Scripts\python -m pytest -q
```

The tests run offline: they cover PoB parsing, the gap rules, rate-limit math, retrieval ranking, the trade
query shape, league switching, the agent's tool loop (with a fake Claude client), and the MCP server protocol.
