"""Normalize characters from poe.ninja / pathofexile.com / Path of Building and flag upgrade gaps.

The gap list is a deterministic first pass: it finds what is objectively weak (uncapped
resistances, under-levelled gems, missing links) so the LLM spends its effort pricing
fixes and comparing value instead of re-deriving arithmetic.
"""

from __future__ import annotations

import re
from typing import Any

SLOT_NAMES = {
    "helm": "helmet", "helmet": "helmet", "bodyarmour": "body", "body armour": "body",
    "weapon": "weapon", "weapon 1": "weapon", "offhand": "offhand", "weapon 2": "offhand",
    "weapon2": "weapon_swap", "offhand2": "offhand_swap", "weapon 1 swap": "weapon_swap",
    "weapon 2 swap": "offhand_swap", "gloves": "gloves", "boots": "boots", "amulet": "amulet",
    "ring": "ring1", "ring 1": "ring1", "ring2": "ring2", "ring 2": "ring2", "belt": "belt",
}
PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
RARITY = {0: "Normal", 1: "Magic", 2: "Rare", 3: "Unique", 9: "Relic"}


def _slot(name: str | None) -> str:
    return SLOT_NAMES.get((name or "").strip().lower(), (name or "unknown").strip().lower())


def _mod_text(mod: Any) -> str:
    return mod.get("description", str(mod)) if isinstance(mod, dict) else str(mod)


def _max_links(sockets: list[dict[str, Any]] | None) -> int:
    groups: dict[int, int] = {}
    for sock in sockets or []:
        groups[sock.get("group", 0)] = groups.get(sock.get("group", 0), 0) + 1
    return max(groups.values(), default=0)


def _ggg_item(data: dict[str, Any]) -> dict[str, Any]:
    """GGG's item JSON (used by pathofexile.com and mirrored by poe.ninja) -> snapshot item."""
    item: dict[str, Any] = {
        "slot": _slot(data.get("inventoryId")),
        "rarity": data.get("rarity") or data.get("frameTypeId") or RARITY.get(data.get("frameType"), ""),
        "name": data.get("name") or data.get("typeLine"),
        "base": data.get("baseType") or data.get("typeLine"),
        "ilvl": data.get("ilvl"),
    }
    if data.get("corrupted"):
        item["corrupted"] = True
    for src, dst in (("implicitMods", "implicit"), ("explicitMods", "explicit"), ("craftedMods", "crafted"),
                     ("fracturedMods", "fractured"), ("enchantMods", "enchant")):
        mods = [_mod_text(m) for m in data.get(src) or []]
        if mods:
            item[dst] = mods
    links = _max_links(data.get("sockets"))
    if links:
        item["max_links"] = links
    gems = [s for s in data.get("socketedItems") or [] if s.get("frameType") == 4 or "support" in s]
    if gems:
        item["socketed_gems"] = [s.get("typeLine") for s in gems]
    return {k: v for k, v in item.items() if v not in (None, "", [])}


def _is_support(name: str) -> bool:
    return name.endswith("Support") or name.startswith("Awakened ")


def from_ninja(data: dict[str, Any]) -> dict[str, Any]:
    ds = data.get("defensiveStats") or {}
    defence = {
        "life": ds.get("life"), "energy_shield": ds.get("energyShield"), "mana": ds.get("mana"),
        "ward": ds.get("ward"), "ehp": ds.get("effectiveHealthPool"),
        "armour": ds.get("armour"), "evasion": ds.get("evasionRating"),
        "block": ds.get("blockChance"), "spell_block": ds.get("spellBlockChance"),
        "suppression": ds.get("spellSuppressionChance"), "movement_speed": ds.get("movementSpeed"),
        "life_regen": ds.get("lifeRegen"),
        "phys_reduction": ((ds.get("damageReductions") or {}).get("physical") or {}).get("value"),
        "str": ds.get("strength"), "dex": ds.get("dexterity"), "int": ds.get("intelligence"),
    }
    for element in ("fire", "cold", "lightning", "chaos"):
        defence[f"{element}_res"] = ds.get(f"{element}Resistance")
        defence[f"{element}_res_max"] = ds.get(f"{element}ResistanceMax")
        defence[f"{element}_overcap"] = ds.get(f"{element}ResistanceOverCap")
        defence[f"max_hit_{element}"] = ds.get(f"{element}MaximumHitTaken")
    defence["max_hit_physical"] = ds.get("physicalMaximumHitTaken")

    skills = []
    for group in data.get("skills") or []:
        gems = [{"name": g.get("name"), "level": g.get("level"), "quality": g.get("quality"),
                 "support": _is_support(g.get("name") or "")} for g in group.get("allGems") or []]
        best = max(group.get("dps") or [{}], key=lambda d: (d.get("dps") or 0) + (d.get("dotDps") or 0))
        skills.append({
            "slot": _slot(str(group.get("itemSlot"))), "gems": gems,
            "dps": best.get("dps"), "dot_dps": best.get("dotDps"), "dps_skill": best.get("name"),
        })
    main = max(skills, key=lambda s: (s.get("dps") or 0) + (s.get("dot_dps") or 0), default=None)
    main_skill = None
    if main:
        main_skill = {"name": main.get("dps_skill") or next((g["name"] for g in main["gems"] if not g["support"]), None),
                      "gems": main["gems"], "dps": main.get("dps"), "dot_dps": main.get("dot_dps")}
        others = [s for s in skills if s is not main and ((s.get("dps") or 0) + (s.get("dot_dps") or 0)) > 0]
        if others:
            main_skill["other_damage_skills"] = [
                {"name": s.get("dps_skill"), "dps": s.get("dps"), "dot_dps": s.get("dot_dps")} for s in others]

    return {
        "source": "poe.ninja",
        "account": data.get("account"), "character": data.get("name"), "league": data.get("league"),
        "class": data.get("baseClass"), "ascendancy": data.get("class"), "level": data.get("level"),
        "bandit": data.get("banditChoice"), "pantheon": [data.get("pantheonMajor"), data.get("pantheonMinor")],
        "keystones": [k.get("name") for k in data.get("keyStones") or [] if isinstance(k, dict)],
        "defence": {k: v for k, v in defence.items() if v is not None},
        "main_skill": main_skill, "skills": skills,
        "items": [_ggg_item(i["itemData"]) for i in data.get("items") or [] if i.get("itemData")],
        "flasks": [_ggg_item(i["itemData"]) for i in data.get("flasks") or [] if i.get("itemData")],
        "jewels": [_ggg_item(i["itemData"]) for i in data.get("jewels") or [] if i.get("itemData")],
        "pob_export": data.get("pathOfBuildingExport"),
    }


_RES_PATTERNS = [
    (re.compile(r"([+-]\d+)% to all Elemental Resistances", re.I), ("fire", "cold", "lightning")),
    (re.compile(r"([+-]\d+)% to Fire and Cold Resistances", re.I), ("fire", "cold")),
    (re.compile(r"([+-]\d+)% to Fire and Lightning Resistances", re.I), ("fire", "lightning")),
    (re.compile(r"([+-]\d+)% to Cold and Lightning Resistances", re.I), ("cold", "lightning")),
    (re.compile(r"([+-]\d+)% to Fire and Chaos Resistances", re.I), ("fire", "chaos")),
    (re.compile(r"([+-]\d+)% to Cold and Chaos Resistances", re.I), ("cold", "chaos")),
    (re.compile(r"([+-]\d+)% to Lightning and Chaos Resistances", re.I), ("lightning", "chaos")),
    (re.compile(r"([+-]\d+)% to (Fire|Cold|Lightning|Chaos) Resistance$", re.I), None),
]
_LIFE = re.compile(r"([+-]\d+) to maximum Life$", re.I)


def gear_totals(items: list[dict[str, Any]]) -> dict[str, int]:
    """Resistance and flat life sums from gear mods only (no tree, no auras): a floor, not a final number."""
    totals = {"fire_res_from_gear": 0, "cold_res_from_gear": 0, "lightning_res_from_gear": 0,
              "chaos_res_from_gear": 0, "flat_life_from_gear": 0}
    for item in items:
        for mod in [m for key in ("implicit", "explicit", "crafted", "fractured", "enchant") for m in item.get(key, [])]:
            life = _LIFE.search(mod)
            if life:
                totals["flat_life_from_gear"] += int(life.group(1))
                continue
            for pattern, elements in _RES_PATTERNS:
                match = pattern.search(mod)
                if match:
                    for element in elements or (match.group(2).lower(),):
                        totals[f"{element}_res_from_gear"] += int(match.group(1))
                    break
    return totals


def from_ggg(data: dict[str, Any], account: str) -> dict[str, Any]:
    char = data.get("character") or {}
    items, flasks, jewels = [], [], []
    for raw in data.get("items") or []:
        item = _ggg_item(raw)
        inv = raw.get("inventoryId", "")
        if inv == "Flask":
            flasks.append(item)
        elif inv == "PassiveJewels":
            jewels.append(item)
        elif inv not in ("MainInventory", "Cursor", "ExpandedMainInventory"):
            items.append(item)
    skills = []
    for raw in data.get("items") or []:
        socketed = raw.get("socketedItems") or []
        if not socketed:
            continue
        by_group: dict[int, list[dict[str, Any]]] = {}
        for gem in socketed:
            level = next((p["values"][0][0] for p in gem.get("properties", []) if p.get("name") == "Level"), None)
            quality = next((p["values"][0][0] for p in gem.get("properties", []) if p.get("name") == "Quality"), None)
            group = (raw.get("sockets") or [{}] * 6)[gem.get("socket", 0)].get("group", 0) if raw.get("sockets") else 0
            by_group.setdefault(group, []).append({
                "name": gem.get("typeLine"),
                "level": int(re.sub(r"\D", "", str(level))) if level else None,
                "quality": int(re.sub(r"\D", "", str(quality))) if quality else None,
                "support": bool(gem.get("support")),
            })
        for gems in by_group.values():
            skills.append({"slot": _slot(raw.get("inventoryId")), "gems": gems})
    main = max(skills, key=lambda s: len(s["gems"]), default=None)
    return {
        "source": "pathofexile.com",
        "account": account, "character": char.get("name"), "league": char.get("league"),
        "class": char.get("class"), "level": char.get("level"),
        "defence": gear_totals(items),
        "defence_note": "pathofexile.com gives gear only; resistances/life here are summed from item mods, "
                        "excluding passive tree, auras and ascendancy. Ask for a PoB code for exact numbers.",
        "main_skill": {"name": next((g["name"] for g in main["gems"] if not g["support"]), None),
                       "gems": main["gems"]} if main else None,
        "skills": skills, "items": items, "flasks": flasks, "jewels": jewels,
    }


def _gap(area: str, priority: str, finding: str, fix: str) -> dict[str, str]:
    return {"area": area, "priority": priority, "finding": finding, "typical_fix": fix}


def find_gaps(snap: dict[str, Any]) -> list[dict[str, str]]:
    d = snap.get("defence") or {}
    level = snap.get("level") or 0
    has_stats = snap.get("source") != "pathofexile.com"
    keystones = {k.lower() for k in snap.get("keystones") or []}
    is_ci = "chaos inoculation" in keystones or (d.get("life") == 1)
    gaps: list[dict[str, str]] = []

    if has_stats:
        for element in ("fire", "cold", "lightning"):
            res, cap = d.get(f"{element}_res"), d.get(f"{element}_res_max") or 75
            if res is not None and res < cap:
                gaps.append(_gap("survivability", "critical", f"{element.title()} resistance {res}% (cap {cap}%).",
                                 f"Cap {element} res first: re-roll/craft a res suffix, a res ring/belt, or a resist jewel."))
            overcap = d.get(f"{element}_overcap")
            if res is not None and res >= cap and overcap is not None and overcap - res < 15 and level >= 80:
                gaps.append(_gap("survivability", "low", f"Only {overcap - res}% {element} overcap.",
                                 "Elemental Weakness / exposure map mods will uncap you; aim for ~30% overcap."))
        chaos = d.get("chaos_res")
        if chaos is not None and not is_ci:
            if chaos < 0:
                gaps.append(_gap("survivability", "high", f"Chaos resistance {chaos}%.",
                                 "Chaos res suffixes on rings/belt/boots, Amethyst flask, or chaos res via tree."))
            elif chaos < 30 and level >= 80:
                gaps.append(_gap("survivability", "medium", f"Chaos resistance only {chaos}%.",
                                 "Raise toward 40-75% with chaos res suffixes; cheap on rare rings/amulets."))

        pool = (d.get("life") or 0) + (d.get("energy_shield") or 0)
        if level >= 85 and pool:
            if pool < 4500:
                gaps.append(_gap("survivability", "high", f"Life+ES pool {pool} at level {level}.",
                                 "Prioritise +maximum life on every armour slot and %life on tree/jewels."))
            elif pool < 6000:
                gaps.append(_gap("survivability", "medium", f"Life+ES pool {pool} at level {level}.",
                                 "More +life on gear, life-% jewels, or a better body armour."))

        phys_hit = d.get("max_hit_physical")
        if phys_hit and level >= 85:
            if phys_hit < 5000:
                gaps.append(_gap("survivability", "high", f"Physical max hit only {phys_hit}.",
                                 "Stack armour (Determination, Granite flask), fortify, endurance charges, or more pool."))
            elif phys_hit < 8000:
                gaps.append(_gap("survivability", "medium", f"Physical max hit {phys_hit}.",
                                 "Armour/phys-taken-as-elemental, Fortify, or a larger pool."))
        ele_hits = [d.get(f"max_hit_{e}") for e in ("fire", "cold", "lightning") if d.get(f"max_hit_{e}")]
        if ele_hits and level >= 85 and min(ele_hits) < 8000:
            gaps.append(_gap("survivability", "medium", f"Lowest elemental max hit {min(ele_hits)}.",
                             "Max-res nodes/Purity aura, more pool, or elemental damage taken reduction."))

        evasion, armour, supp = d.get("evasion") or 0, d.get("armour") or 0, d.get("suppression")
        if supp is not None and evasion > max(armour, 8000) and supp < 100:
            pri = "high" if supp >= 50 else "medium"
            gaps.append(_gap("survivability", pri, f"Spell suppression {supp}% on an evasion build.",
                             "Cap to 100%: suppression on boots/gloves/body/helmet and tree clusters near Acrobatics."))
        block, spell_block = d.get("block") or 0, d.get("spell_block") or 0
        if block >= 50 and spell_block < 40:
            gaps.append(_gap("survivability", "medium", f"Attack block {block}% but spell block {spell_block}%.",
                             "Add spell block (shield/tree/Glancing Blows choice) since attack block is already invested."))
    else:
        totals = gear_totals(snap.get("items") or [])
        for element in ("fire", "cold", "lightning"):
            # Capping needs 75% plus the -60% post-Kitava penalty; most builds get the bulk (~100%+) from gear.
            if totals[f"{element}_res_from_gear"] < 100:
                gaps.append(_gap("survivability", "medium",
                                 f"Only {totals[f'{element}_res_from_gear']}% {element} res from gear (tree not counted).",
                                 "Verify in Path of Building; if uncapped, add res suffixes."))

    flask_text = " ".join(m.lower() for f in snap.get("flasks") or [] for k in ("explicit", "implicit", "crafted")
                          for m in f.get(k, []))
    if snap.get("flasks"):
        for ailment, words in (("bleeding/corrupted blood", ("bleed",)), ("freeze", ("freez", "chill")),
                               ("curses", ("curse",)), ("poison", ("poison",))):
            if not any(w in flask_text for w in words):
                gaps.append(_gap("survivability", "medium" if ailment != "poison" else "low",
                                 f"No flask removes or grants immunity to {ailment}.",
                                 f"Swap a utility flask suffix to one granting {ailment} immunity (cheap; alt/craft)."))

    boots = next((i for i in snap.get("items") or [] if i.get("slot") == "boots"), None)
    if boots and not any("movement speed" in m.lower() for k in ("explicit", "implicit", "crafted", "enchant")
                         for m in boots.get(k, [])) and boots.get("rarity") != "Unique":
        gaps.append(_gap("utility", "low", "Boots have no movement speed.",
                         "Movement speed boots are cheap and speed up both mapping and dodging."))

    main = snap.get("main_skill") or {}
    gems = main.get("gems") or []
    actives = [g for g in gems if not g.get("support")]
    supports = [g for g in gems if g.get("support")]
    if actives:
        top = actives[0]
        if (top.get("level") or 0) < 20:
            gaps.append(_gap("damage", "high", f"Main skill {top['name']} is level {top.get('level')}.",
                             "Level it to 20 (or buy a 20/20); +1 level is usually 5-10% more damage."))
        elif (top.get("quality") or 0) < 20:
            gaps.append(_gap("damage", "medium", f"Main skill {top['name']} has {top.get('quality') or 0}% quality.",
                             "Gemcutter's Prisms to 20% quality, or buy a 20/20 copy."))
        elif level >= 90 and (top.get("level") or 0) == 20:
            gaps.append(_gap("damage", "low", f"Main skill {top['name']} is 20, not 21.",
                             "Vaal a spare 20/20 for a 21, or add +1 socketed/all gem levels on weapon/helmet/amulet."))
    if gems and len(gems) < 6:
        # Some builds (RF + Scorching Ray, aura stackers, trigger setups) put the top DPS skill in a small link on purpose.
        bigger = [s for s in snap.get("skills") or [] if len(s.get("gems") or []) > len(gems)]
        gaps.append(_gap("damage", "medium" if bigger else "high",
                         f"Main skill {main.get('name')} is in a {len(gems)}-gem group"
                         + (f" (another group has {max(len(s['gems']) for s in bigger)} gems)." if bigger else "."),
                         "More links/supports for the top skill: 6-link body, +socket helmet/gloves crafts, or move "
                         "it to the biggest link if the build allows."))
    low_supports = [g for g in supports if (g.get("level") or 0) < 20 and not g["name"].startswith("Awakened")]
    if low_supports:
        gaps.append(_gap("damage", "medium",
                         "Supports below level 20: " + ", ".join(f"{g['name']} ({g.get('level')})" for g in low_supports),
                         "Buy or level them to 20; very cheap per % damage."))
    if level >= 90 and supports and not any(g["name"].startswith("Awakened") for g in supports):
        gaps.append(_gap("damage", "low", "No Awakened support gems in the main link.",
                         "Awakened versions of your best supports are a common late-game damage step."))

    amulet = next((i for i in snap.get("items") or [] if i.get("slot") == "amulet"), None)
    # GGG reports anoints as enchants; Path of Building lists them among implicits.
    if amulet and not any(m.lower().startswith("allocates") for k in ("enchant", "implicit", "crafted")
                          for m in amulet.get(k, [])):
        gaps.append(_gap("damage", "medium", "Amulet is not anointed.",
                         "Anoint a strong notable with oils; one of the cheapest power spikes in the game."))

    for item in snap.get("items") or []:
        if item.get("rarity") == "Rare" and not item.get("crafted") and len(item.get("explicit", [])) < 6 \
                and not item.get("corrupted") and item.get("slot") not in ("weapon_swap", "offhand_swap"):
            gaps.append(_gap("either", "low", f"{item['slot']} ({item.get('name')}) has no crafted mod and "
                             f"{len(item.get('explicit', []))} explicit mods; it may have an open affix.",
                             "Check in-game; bench-craft life/res/damage into the open prefix or suffix for a few chaos."))

    gaps.sort(key=lambda g: PRIORITY_ORDER[g["priority"]])
    return gaps


def summary(snap: dict[str, Any]) -> dict[str, Any]:
    """Compact view for the LLM: everything except the raw PoB export."""
    out = {k: v for k, v in snap.items() if k not in ("pob_export", "skills")}
    out["other_skill_groups"] = [
        " + ".join(g["name"] for g in s["gems"] if g.get("name"))
        for s in snap.get("skills") or []
        if s.get("gems") and (snap.get("main_skill") or {}).get("gems") != s["gems"]
    ]
    out["gaps"] = find_gaps(snap)
    return out
