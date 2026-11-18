"""Allocated passive tree for a character, from pathofexile.com plus GGG's public skill-tree export.

``get-passive-skills`` returns only node hashes; the node names and stats come from GGG's
``skilltree-export`` data.json (cached for a day by ``net``). The atlas tree is not exposed by any
character-window endpoint we could find, so it is not covered here.
"""

from __future__ import annotations

from typing import Any

from . import account, net

TREE_URL = "https://raw.githubusercontent.com/grindinggear/skilltree-export/master/data.json"


def _tree_nodes() -> tuple[dict[str, Any], set[int]]:
    data = net.get(TREE_URL, cache_ttl=24 * 3600)
    return data.get("nodes") or {}, set(data.get("jewelSlots") or [])


def summarize(raw: dict[str, Any], nodes: dict[str, Any], jewel_slots: set[int]) -> dict[str, Any]:
    """Turn a get-passive-skills response into names the advisor can reason about."""
    hashes = [int(h) for h in (raw.get("hashes") or []) + (raw.get("hashes_ex") or [])]
    keystones, notables, ascendancy, masteries = [], [], [], []
    small = unknown = jewel_sockets = 0
    for h in hashes:
        node = nodes.get(str(h))
        if node is None:
            unknown += 1
            continue
        name = node.get("name")
        if h in jewel_slots:
            jewel_sockets += 1
        elif node.get("isMastery"):
            continue  # reported below with the chosen effect
        elif node.get("isKeystone"):
            keystones.append(name)
        elif node.get("ascendancyName"):
            ascendancy.append(name)
        elif node.get("isNotable"):
            notables.append(name)
        else:
            small += 1
    for node_id, effect_id in (raw.get("mastery_effects") or {}).items():
        node = nodes.get(str(node_id)) or {}
        effect = next((e for e in node.get("masteryEffects") or [] if e.get("effect") == int(effect_id)), {})
        masteries.append({"mastery": node.get("name"), "effect": effect.get("stats") or [f"effect {effect_id}"]})
    out = {
        "points_allocated": len(hashes), "keystones": keystones, "notables": notables,
        "ascendancy_nodes": ascendancy, "masteries": masteries,
        "small_passives": small, "jewel_sockets_allocated": jewel_sockets,
        "cluster_and_timeless_jewels": raw.get("jewel_data") or None,
        "skill_overrides": raw.get("skill_overrides") or None,
        "atlas_tree": "not available: no character-window endpoint exposes it; ask for a screenshot or the build's atlas notes",
    }
    if unknown:
        out["unknown_node_hashes"] = unknown  # e.g. cluster jewel nodes, which the static tree export lacks
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def get_passive_tree(acct: str, character: str) -> dict[str, Any]:
    raw = account.get_passive_skills(acct, character)
    nodes, jewel_slots = _tree_nodes()
    return summarize(raw, nodes, jewel_slots)
