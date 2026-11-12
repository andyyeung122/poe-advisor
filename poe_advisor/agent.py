"""Claude agent loop: streams answers, runs tools, keeps history append-only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any, Callable

import anthropic

from . import net  # noqa: F401  (installs the OS trust store before the Anthropic client is built)
from . import ninja
from .config import settings
from .tools import Toolbox

MAX_STEPS = 30

# Shared with Claude Code (CLAUDE.md imports the same file), so both front ends follow one set of rules.
SYSTEM_PROMPT = (Path(__file__).with_name("advisor_prompt.md")).read_text(encoding="utf-8").strip()


class Advisor:
    def __init__(
        self,
        toolbox: Toolbox | None = None,
        model: str | None = None,
        effort: str | None = None,
        web_search: bool | None = None,
        fallbacks: bool | None = None,
    ):
        self.toolbox = toolbox or Toolbox()
        self.model = model or settings.model
        self.effort = effort or settings.effort
        self.web_search = settings.web_search if web_search is None else web_search
        use_fallbacks = settings.refusal_fallbacks if fallbacks is None else fallbacks
        # Server-side fallback routes a declined request to another model; Haiku has no fallback target.
        self.fallbacks = use_fallbacks and not self.model.startswith("claude-haiku")
        self.client = anthropic.Anthropic()
        self.messages: list[dict[str, Any]] = []

    def tools(self) -> list[dict[str, Any]]:
        tools = self.toolbox.definitions()
        if self.web_search:
            tools += [
                {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
            ]
        return tools

    def reset(self) -> None:
        self.messages = []

    def _context_line(self) -> str:
        build = self.toolbox.build
        build_label = (f"{build.get('character') or 'PoB build'} ({build.get('ascendancy') or build.get('class')}, "
                       f"level {build.get('level')})") if build else "none loaded"
        try:
            league = ninja.current_league()
        except Exception:
            league = "unknown (poe.ninja unreachable)"
        return f"[session: date {date.today().isoformat()}; league {league}; active build {build_label}]"

    def _stream_once(self, on_text: Callable[[str], None], on_status: Callable[[str], None]):
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=64000,
            system=SYSTEM_PROMPT,
            tools=self.tools(),
            messages=self.messages,
            cache_control={"type": "ephemeral"},
            output_config={"effort": self.effort},
        )
        if self.fallbacks:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        with self.client.beta.messages.stream(**kwargs) as stream:
            for event in stream:
                if event.type == "text":
                    on_text(event.text)
                elif event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "server_tool_use":
                        on_status(f"{block.name}...")
                    elif block.type == "tool_use":
                        on_status(f"{block.name}...")
            return stream.get_final_message()

    def ask(
        self,
        text: str,
        on_text: Callable[[str], None] = lambda s: print(s, end="", flush=True),
        on_status: Callable[[str], None] = lambda s: None,
        on_tool: Callable[[str, dict[str, Any], bool], None] = lambda name, args, err: None,
    ) -> None:
        self.messages.append({"role": "user", "content": f"{self._context_line()}\n\n{text}"})
        json_retries = 0
        for _ in range(MAX_STEPS):
            try:
                message = self._stream_once(on_text, on_status)
            except ValueError:
                # Eager input streaming: a tool input that is not valid JSON surfaces here. Retry the turn once.
                json_retries += 1
                if json_retries > 1:
                    on_text("\n[The model produced an unreadable tool call twice; please rephrase.]\n")
                    return
                continue

            if message.stop_reason == "refusal":
                on_text("\n[The request was declined.]\n")
                self.messages.append({"role": "assistant", "content": message.content})
                return
            tool_uses = [b for b in message.content if b.type == "tool_use"]
            if message.stop_reason == "max_tokens" and tool_uses:
                # Truncated tool call: never run it. Drop the partial turn so history stays valid.
                on_text("\n[Response hit the length limit mid tool call; ask a narrower question.]\n")
                return

            self.messages.append({"role": "assistant", "content": message.content})
            if message.stop_reason == "pause_turn":
                continue  # a server tool (web search) paused a long turn; resend to let it continue
            if not tool_uses:
                return

            with ThreadPoolExecutor(max_workers=4) as pool:
                outcomes = list(pool.map(lambda b: self.toolbox.run(b.name, dict(b.input)), tool_uses))
            results = []
            for block, (content, is_error) in zip(tool_uses, outcomes):
                on_tool(block.name, dict(block.input), is_error)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content,
                                "is_error": is_error})
            self.messages.append({"role": "user", "content": results})  # all results in one message
        on_text("\n[Stopped after too many tool steps.]\n")
