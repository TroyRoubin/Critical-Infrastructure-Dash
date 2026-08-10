#!/usr/bin/env python3
"""Apply the Critical Infrastructure Dashboard QLDTraffic KPI fix.

The existing public QLDTraffic GeoJSON feed remains the data source.
No API key or secret is introduced.

This script is idempotent:
- first run replaces the old closure/restriction-gated parse_roads();
- later runs validate the fix and make no duplicate changes.
"""
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
REFRESH = ROOT / "refresh.py"

MARKER = "# QLDTraffic dashboard filter: Hazard + Flooding only (2026-08-10)"
NEW_ROAD_BLOCK = '\n# QLDTraffic dashboard filter: Hazard + Flooding only (2026-08-10)\nQLDTRAFFIC_ALLOWED_EVENT_CATEGORIES = {"hazard", "flooding"}\n\n\ndef qldtraffic_event_category(properties: dict[str, Any]) -> str | None:\n    # GeoJSON event_type values are "Hazard" and "Flooding".\n    category = norm(properties.get("event_type"))\n    return category if category in QLDTRAFFIC_ALLOWED_EVENT_CATEGORIES else None\n\n\ndef qldtraffic_fallback_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:\n    # Never re-introduce cached road records that were not already verified\n    # as Hazard or Flooding.\n    filtered = []\n    for item in records:\n        if item.get("sector") != "roads":\n            continue\n        category = norm(\n            item.get("event_category")\n            or item.get("event_type")\n            or ""\n        )\n        if category in QLDTRAFFIC_ALLOWED_EVENT_CATEGORIES:\n            filtered.append(item)\n    return filtered\n\n\ndef parse_roads(payload: dict[str, Any], lgas: dict[str, Any]) -> list[dict[str, Any]]:\n    # Return active Published QLDTraffic Hazard/Flooding events only.\n    incidents = []\n\n    for feature in payload.get("features", []):\n        props = feature.get("properties") or {}\n\n        # Authoritative filter: QLDTraffic Current alerts -> Hazards / Flooding.\n        event_category = qldtraffic_event_category(props)\n        if event_category is None:\n            continue\n\n        # Preserve the dashboard\'s existing publication-state safeguard.\n        if norm(props.get("status")) not in {"", "active", "published"}:\n            continue\n\n        duration = props.get("duration") or {}\n        if not active_now(duration.get("start"), duration.get("end")):\n            continue\n\n        impact = props.get("impact") or {}\n        impact_type = clean(impact.get("impact_type"))\n        impact_subtype = clean(impact.get("impact_subtype"))\n        combined = norm(f"{impact_type} {impact_subtype}")\n\n        # Keep impact classification for display/compatibility only.\n        # It is deliberately NOT an inclusion gate.\n        if "closure" in combined or "closed" in combined:\n            subtype = "closure"\n        elif "lane" in combined or "restriction" in combined:\n            subtype = "restriction"\n        else:\n            subtype = "incident"\n\n        summary = props.get("road_summary") or {}\n        geometry = feature.get("geometry")\n        coords = representative_point(geometry)\n        lga = clean(summary.get("local_government_area")) or locate_lga(coords, lgas)\n        road = clean(summary.get("road_name")) or "Queensland road"\n\n        event_type = clean(props.get("event_type")) or title_case(event_category)\n        event_subtype = clean(props.get("event_subtype"))\n        event_due_to = clean(props.get("event_due_to"))\n        status = impact_subtype or impact_type or event_subtype or event_type\n\n        description_parts = []\n        for value in (\n            props.get("description"),\n            props.get("advice"),\n            props.get("information"),\n        ):\n            value = clean(value)\n            if value and value not in description_parts:\n                description_parts.append(value)\n\n        title = f"{road}: {event_type}"\n        if status and norm(status) != norm(event_type):\n            title += f" — {status}"\n\n        incidents.append({\n            "id": f"roads-{props.get(\'id\') or stable_id(road, event_type, duration.get(\'start\'))}",\n            "sector": "roads",\n            "subtype": subtype,\n            "event_category": event_category,\n            "event_type": event_type,\n            "event_subtype": event_subtype,\n            "event_due_to": event_due_to,\n            "title": title,\n            "description": ". ".join(description_parts),\n            "status": status,\n            "lga": lga,\n            "locality": clean(summary.get("locality")),\n            "coordinates": coords,\n            "geometry": geometry,\n            "customers": 0,\n            "planned": False,\n            "updated": iso(props.get("last_updated")) or NOW_ISO,\n            "source_name": "QLDTraffic",\n            "source_url": clean(props.get("url") or props.get("web_link")) or SOURCES["qldtraffic"]["url"],\n        })\n\n    return incidents\n\n\n'

PARSE_ROADS_PATTERN = re.compile(
    r"(?ms)^def parse_roads\([^\n]*\)"
    r"(?:\s*->\s*[^:\n]+)?"
    r":\n.*?"
    r"(?=^def [A-Za-z_][A-Za-z0-9_]*\(|\Z)"
)

FALLBACK_PATTERN = re.compile(
    r"(?m)^(?P<indent>[ \t]+)"
    r"fallback = previous_by_source\.get\(key, \[\]\)"
    r"[ \t]*$"
)


def replace_parse_roads(text: str) -> tuple[str, bool]:
    if MARKER in text:
        return text, False

    match = PARSE_ROADS_PATTERN.search(text)
    if not match:
        raise RuntimeError(
            "Could not find the current top-level parse_roads() function. "
            "refresh.py may have changed; refusing an unsafe patch."
        )

    return text[:match.start()] + NEW_ROAD_BLOCK + text[match.end():], True


def replace_qldtraffic_fallback(text: str) -> tuple[str, bool]:
    if "qldtraffic_fallback_records(previous_by_source.get(key, []))" in text:
        return text, False

    match = FALLBACK_PATTERN.search(text)
    if not match:
        raise RuntimeError(
            "Could not find the current cached-source fallback in main(). "
            "refusing an unsafe patch."
        )

    indent = match.group("indent")
    replacement = (
        f"{indent}fallback = (\n"
        f"{indent}    qldtraffic_fallback_records(previous_by_source.get(key, []))\n"
        f'{indent}    if key == "qldtraffic"\n'
        f"{indent}    else previous_by_source.get(key, [])\n"
        f"{indent})"
    )

    return text[:match.start()] + replacement + text[match.end():], True


def validate(text: str) -> None:
    if MARKER not in text:
        raise RuntimeError("Validation failed: QLDTraffic marker is missing.")

    road_start = text.index("def parse_roads(", text.index(MARKER))
    next_def = text.find("\ndef ", road_start + len("def parse_roads("))
    road_text = text[road_start:] if next_def < 0 else text[road_start:next_def]

    required = (
        "event_category = qldtraffic_event_category(props)",
        '"event_category": event_category',
        'subtype = "incident"',
        'planned": False',
    )
    for token in required:
        if token not in road_text:
            raise RuntimeError(
                f"Validation failed: patched parse_roads() is missing {token!r}."
            )

    # The old parser rejected every non-closure/non-restriction event.
    old_gate = (
        'elif "lane" in combined or "restriction" in combined:\n'
        '            subtype = "restriction"\n'
        "        else:\n"
        "            continue"
    )
    if old_gate in road_text:
        raise RuntimeError(
            "Validation failed: the old closure/restriction inclusion gate remains."
        )

    if "qldtraffic_fallback_records(previous_by_source.get(key, []))" not in text:
        raise RuntimeError(
            "Validation failed: safe QLDTraffic cached fallback is missing."
        )

    compile(text, str(REFRESH), "exec")


def apply_patch(text: str) -> tuple[str, bool]:
    text, roads_changed = replace_parse_roads(text)
    text, fallback_changed = replace_qldtraffic_fallback(text)
    validate(text)
    return text, roads_changed or fallback_changed


def main() -> int:
    if not REFRESH.exists():
        raise SystemExit(
            "refresh.py was not found beside apply_qldtraffic_fix.py."
        )

    original = REFRESH.read_text(encoding="utf-8")
    patched, changed = apply_patch(original)

    if changed:
        REFRESH.write_text(patched, encoding="utf-8")
        print(
            "Applied QLDTraffic filter: active Published Hazard + Flooding only."
        )
    else:
        print(
            "QLDTraffic Hazard + Flooding filter is already applied; no change."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
