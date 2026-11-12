"""Path of Building import: decode share codes / pobb.in / pastebin links into a build snapshot."""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
import zlib
from typing import Any

from . import net
from .analysis import SLOT_NAMES

_URL_PATTERNS = [
    (re.compile(r"pobb\.in/(?:u/[^/]+/)?([A-Za-z0-9_-]+)"), "https://pobb.in/{}/raw"),
    (re.compile(r"pastebin\.com/(?:raw/)?([A-Za-z0-9]+)"), "https://pastebin.com/raw/{}"),
    (re.compile(r"poe\.ninja/(?:poe1/)?pob/([A-Za-z0-9_-]+)"), "https://poe.ninja/poe1/pob/raw/{}"),
]

# PlayerStat names written by Path of Building -> snapshot keys.
STAT_MAP = {
    "Life": "life", "EnergyShield": "energy_shield", "Mana": "mana", "Ward": "ward",
    "TotalEHP": "ehp", "Armour": "armour", "Evasion": "evasion",
    "FireResist": "fire_res", "ColdResist": "cold_res", "LightningResist": "lightning_res", "ChaosResist": "chaos_res",
    "FireResistOverCap": "fire_overcap", "ColdResistOverCap": "cold_overcap",
    "LightningResistOverCap": "lightning_overcap", "ChaosResistOverCap": "chaos_overcap",
    "EffectiveSpellSuppressionChance": "suppression", "SpellSuppressionChance": "suppression",
    "EffectiveBlockChance": "block", "BlockChance": "block",
    "EffectiveSpellBlockChance": "spell_block", "SpellBlockChance": "spell_block",
    "PhysicalDamageReduction": "phys_reduction", "LifeRegenRecovery": "life_regen",
    "EffectiveMovementSpeedMod": "movement_speed_mod",
    "PhysicalMaximumHitTaken": "max_hit_physical", "FireMaximumHitTaken": "max_hit_fire",
    "ColdMaximumHitTaken": "max_hit_cold", "LightningMaximumHitTaken": "max_hit_lightning",
    "ChaosMaximumHitTaken": "max_hit_chaos",
    "Str": "str", "Dex": "dex", "Int": "int",
}
DPS_KEYS = ("FullDPS", "CombinedDPS", "TotalDPS", "TotalDotDPS")

_ITEM_META = {
    "unique id", "item level", "quality", "sockets", "levelreq", "level req", "league", "radius",
    "limited to", "selected variant", "has alt variant", "catalyst", "catalystquality", "prefix",
    "suffix", "armour", "evasion", "energy shield", "ward", "variant", "talisman tier", "cluster jewel skill",
    "cluster jewel node count", "armourbasepercentile", "evasionbasepercentile", "energyshieldbasepercentile",
    "requires class", "crafted", "item class",
}


class PobError(ValueError):
    pass


def fetch_code(code_or_url: str) -> str:
    text = code_or_url.strip()
    for pattern, raw_url in _URL_PATTERNS:
        match = pattern.search(text)
        if match:
            return net.get(raw_url.format(match.group(1)), parse="text", cache_ttl=3600).strip()
    return text


def decode(code: str) -> str:
    code = re.sub(r"\s+", "", code)
    try:
        raw = base64.urlsafe_b64decode(code + "=" * (-len(code) % 4))
        return zlib.decompress(raw).decode("utf-8")
    except (ValueError, zlib.error) as exc:
        raise PobError("Not a valid Path of Building code") from exc


def encode(xml_text: str) -> str:
    return base64.urlsafe_b64encode(zlib.compress(xml_text.encode("utf-8"))).decode()


def _num(value: str | None) -> float | int | None:
    if value is None:
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    return int(f) if f.is_integer() else round(f, 2)


def _max_links(sockets: str) -> int:
    return max((len(group.split("-")) for group in sockets.split()), default=0)


def parse_item_text(text: str) -> dict[str, Any]:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    item: dict[str, Any] = {"implicit": [], "explicit": [], "crafted": [], "fractured": [], "enchant": []}
    if not lines:
        return item
    idx = 0
    if lines[0].lower().startswith("rarity:"):
        item["rarity"] = lines[0].split(":", 1)[1].strip().title()
        idx = 1
    if item.get("rarity") in ("Rare", "Unique", "Relic") and len(lines) > idx + 1:
        item["name"], item["base"] = lines[idx], lines[idx + 1]
        idx += 2
    elif len(lines) > idx:
        item["name"] = item["base"] = lines[idx]
        idx += 1

    implicit_left = 0
    for line in lines[idx:]:
        key, _, value = line.partition(":")
        lowered = key.strip().lower()
        if lowered == "implicits":
            implicit_left = int(value.strip() or 0)
            continue
        if lowered == "item level":
            item["ilvl"] = _num(value.strip())
            continue
        if lowered == "sockets":
            item["max_links"] = _max_links(value.strip())
            continue
        if line.lower() == "corrupted":
            item["corrupted"] = True
            continue
        if value and lowered in _ITEM_META:
            continue
        tags = re.findall(r"\{([^}]*)\}", line)
        mod = re.sub(r"\{[^}]*\}", "", line).strip()
        if not mod:
            continue
        if implicit_left > 0:
            implicit_left -= 1
            bucket = "enchant" if any(t.startswith("enchant") for t in tags) else "implicit"
        elif any(t == "crafted" for t in tags):
            bucket = "crafted"
        elif any(t == "fractured" for t in tags):
            bucket = "fractured"
        else:
            bucket = "explicit"
        item[bucket].append(mod)
    return {k: v for k, v in item.items() if v not in ([], None)}


def _skill_groups(root: ET.Element) -> list[ET.Element]:
    skills = root.find("Skills")
    if skills is None:
        return []
    active = skills.get("activeSkillSet")
    sets = skills.findall("SkillSet")
    if sets:
        chosen = next((s for s in sets if s.get("id") == active), sets[0])
        return chosen.findall("Skill")
    return skills.findall("Skill")


def parse(xml_text: str) -> dict[str, Any]:
    """Turn Path of Building XML into the common snapshot format."""
    root = ET.fromstring(xml_text)
    build = root.find("Build")
    if build is None:
        raise PobError("Path of Building XML has no <Build> element")

    stats: dict[str, Any] = {}
    dps: dict[str, Any] = {}
    for el in build.findall("PlayerStat"):
        name, value = el.get("stat"), _num(el.get("value"))
        if name in STAT_MAP and STAT_MAP[name] not in stats:
            stats[STAT_MAP[name]] = value
        if name in DPS_KEYS:
            dps[name] = value

    groups = []
    for skill in _skill_groups(root):
        gems = [
            {
                "name": gem.get("nameSpec") or gem.get("skillId"),
                "level": _num(gem.get("level")),
                "quality": _num(gem.get("quality")),
                "support": "Support" in (gem.get("gemId") or "") or (gem.get("skillId") or "").startswith("Support"),
                "enabled": gem.get("enabled") != "false",
            }
            for gem in skill.findall("Gem")
        ]
        groups.append({"slot": skill.get("slot"), "label": skill.get("label"),
                       "enabled": skill.get("enabled") != "false", "gems": gems})

    main_idx = int(build.get("mainSocketGroup") or 1) - 1
    main_group = groups[main_idx] if 0 <= main_idx < len(groups) else (groups[0] if groups else None)
    main_skill = None
    if main_group:
        actives = [g for g in main_group["gems"] if not g["support"]]
        main_skill = {
            "name": (actives[0]["name"] if actives else main_group["label"]) or "unknown",
            "gems": main_group["gems"],
            "slot": main_group["slot"],
            "dps": dps.get("TotalDPS"),
            "dot_dps": dps.get("TotalDotDPS"),
            "combined_dps": dps.get("FullDPS") or dps.get("CombinedDPS"),
        }

    items_el = root.find("Items")
    items: list[dict[str, Any]] = []
    flasks: list[dict[str, Any]] = []
    jewels: list[dict[str, Any]] = []
    if items_el is not None:
        by_id = {el.get("id"): parse_item_text(el.text or "") for el in items_el.findall("Item")}
        sets = items_el.findall("ItemSet")
        active = items_el.get("activeItemSet")
        slot_parent = next((s for s in sets if s.get("id") == active), sets[0] if sets else items_el)
        for slot in slot_parent.findall("Slot"):
            item = by_id.get(slot.get("itemId"))
            if not item or slot.get("itemId") == "0":
                continue
            name = slot.get("name") or ""
            entry = {"slot": SLOT_NAMES.get(name.lower(), name.lower()), **item}
            if name.startswith("Flask"):
                flasks.append(entry)
            elif "Jewel" in name or "Abyssal" in name:
                jewels.append(entry)
            else:
                items.append(entry)
        tree = root.find("Tree")
        if tree is not None:
            for spec in tree.findall("Spec"):
                for socket in spec.iter("Socket"):
                    item = by_id.get(socket.get("itemId"))
                    if item and socket.get("itemId") != "0":
                        jewels.append({"slot": f"Tree socket {socket.get('nodeId')}", **item})
                break

    return {
        "source": "path_of_building",
        "class": build.get("className"),
        "ascendancy": build.get("ascendClassName"),
        "level": _num(build.get("level")),
        "bandit": build.get("bandit"),
        "pantheon": [build.get("pantheonMajorGod"), build.get("pantheonMinorGod")],
        "defence": stats,
        "main_skill": main_skill,
        "skills": groups,
        "items": items,
        "flasks": flasks,
        "jewels": jewels,
    }


def load(code_or_url: str) -> dict[str, Any]:
    return parse(decode(fetch_code(code_or_url)))
