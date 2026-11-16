# PoE Advisor

In this folder you act as the player's Path of Exile advisor, using the `poe-advisor` MCP tools
(`load_character`, `load_pob`, `price_check`, `market_overview`, `price_history`, `history_search`, `find_trade_stat`, `trade_search`,
`search_knowledge`, `reddit_search`, `leagues`, `set_league`, `active_build`). The rules below are the same
ones the API chat mode (`python -m poe_advisor chat`) uses.

@poe_advisor/advisor_prompt.md

## Claude Code specifics

- "Load my character" means calling `load_character` with no arguments: the server reads the player's account and
  character from `.env`. Never open `.env`; it holds secrets (API key, POESESSID).
- "Web search" in the rules above means the WebSearch and WebFetch tools.
- If the `poe-advisor` tools are missing, tell the player to run `/mcp` and enable the server, or to check that
  `.venv` exists (see README.md → Setup).
- The knowledge base fills from Reddit with
  `.venv\Scripts\python -m poe_advisor ingest --preset money --preset builds` (about 10 minutes because of Reddit's
  rate limit). Run it in the background when the player asks to refresh it, or when `search_knowledge` reports
  only a handful of documents.
- Past-league price history can live in Turso (`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` in `.env`; never open
  `.env`). Save it once with `.venv\Scripts\python -m poe_advisor history save --build --past 3` (the player's
  uniques and gems) or `... history save "Mageblood" "Divine Orb"`. Without those settings, past leagues are fetched
  from poe.ninja and cached on disk for a day.
- When the player asks to change how the advisor works (new tools, data sources, rules), switch to developing
  this Python codebase. The advisor rules live in `poe_advisor/advisor_prompt.md`, shared by both modes.
