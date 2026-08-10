#!/usr/bin/env python3
"""Patch refresh.py so QLDTraffic mirrors the Hazard + Flooding website filters.

The source remains the public events_v2 GeoJSON feed. The primary event_type is
used as the only category gate: every current Hazard/Flooding incident is kept,
and Roadworks, Crash, Congestion and Special event are excluded.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
REFRESH = ROOT / "refresh.py"

OLD_VERSION = 'SCRIPT_VERSION = "2026-08-10-qldtraffic-strict-hazards-flooding-v6"'
NEW_VERSION = 'SCRIPT_VERSION = "2026-08-10-qldtraffic-hazards-flooding-all-v7"'

NEW_ROAD_BLOCK = r'''
QLDTRAFFIC_ALLOWED_EVENT_CATEGORIES = {"hazard", "flooding"}


def qldtraffic_event_category(properties: dict[str, Any]) -> str | None:
    """Return only the primary QLDTraffic category selected by the dashboard."""
    event_type = norm(properties.get("event_type"))
    return event_type if event_type in QLDTRAFFIC_ALLOWED_EVENT_CATEGORIES else None


def qldtraffic_fallback_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain only previously verified Hazard/Flooding road incidents."""
    return [
        item
        for item in records
        if item.get("sector") == "roads"
        and norm(item.get("event_category")) in QLDTRAFFIC_ALLOWED_EVENT_CATEGORIES
    ]


def parse_roads(payload: dict[str, Any], lgas: dict[str, Any]) -> list[dict[str, Any]]:
    """Publish every current QLDTraffic incident under Hazard or Flooding.

    No closure/restriction gate is applied. The primary event_type is
    authoritative: Hazard and Flooding are included; Roadworks, Crash,
    Congestion and Special event are excluded.
    """
    incidents = []
    for feature in payload.get("features", []):
        props = feature.get("properties") or {}

        if norm(props.get("status")) not in {"", "active", "published"}:
            continue

        event_category = qldtraffic_event_category(props)
        if event_category is None:
            continue

        duration = props.get("duration") or {}
        if not active_now(duration.get("start"), duration.get("end")):
            continue

        impact = props.get("impact") or {}
        summary = props.get("road_summary") or {}
        geometry = feature.get("geometry")
        coords = representative_point(geometry)
        lga = clean(summary.get("local_government_area")) or locate_lga(coords, lgas)
        road = clean(summary.get("road_name")) or "Queensland road"

        event_type_label = clean(props.get("event_type")) or title_case(event_category)
        event_subtype = clean(props.get("event_subtype"))
        event_due_to = clean(props.get("event_due_to"))
        impact_type = clean(impact.get("impact_type"))
        impact_subtype = clean(impact.get("impact_subtype"))
        status = event_subtype or impact_subtype or impact_type or event_type_label

        narrative_parts = []
        for value in (props.get("description"), props.get("advice"), props.get("information")):
            value = clean(value)
            if value and value not in narrative_parts:
                narrative_parts.append(value)

        title = f"{road}: {event_type_label}"
        if event_subtype and norm(event_subtype) != norm(event_type_label):
            title += f" — {event_subtype}"

        incidents.append({
            "id": f"roads-{props.get('id') or stable_id(road, event_type_label, duration.get('start'))}",
            "sector": "roads",
            "subtype": event_category,
            "event_category": event_category,
            "event_type": event_type_label,
            "event_subtype": event_subtype,
            "event_due_to": event_due_to,
            "title": title,
            "description": ". ".join(narrative_parts),
            "status": status,
            "lga": lga,
            "locality": clean(summary.get("locality")),
            "coordinates": coords,
            "geometry": geometry,
            "customers": 0,
            "planned": False,
            "updated": iso(props.get("last_updated")) or NOW_ISO,
            "source_name": "QLDTraffic",
            "source_url": OFFICIAL_SOURCE_URLS["qldtraffic"],
        })

    return incidents

'''

UI_PATCH_FUNCTION = r'''
def patch_dashboard_road_kpi() -> None:
    """Keep the road KPI aligned with the Hazard/Flooding incident layer."""
    text = INDEX.read_text(encoding="utf-8")

    text = text.replace(
        '<div class="metric-label">Road closures</div>',
        '<div class="metric-label">Road incidents</div>',
        1,
    )
    text = text.replace(
        '<div class="metric-detail" id="roadsDetail">0 restrictions</div>',
        '<div class="metric-detail" id="roadsDetail">0 hazards · 0 flooding</div>',
        1,
    )
    text = text.replace(
        "roads:{elementId:'roadsTrend',upField:'roads_up',downField:'roads_down',hours:6,label:'Road closures',unit:'closures'}",
        "roads:{elementId:'roadsTrend',upField:'roads_up',downField:'roads_down',hours:6,label:'Road incidents',unit:'incidents'}",
        1,
    )

    old_summary = (
        "      const closures = context.filter(i => i.sector === 'roads' && i.subtype === 'closure');\n"
        "      const restrictions = context.filter(i => i.sector === 'roads' && i.subtype === 'restriction');\n"
        "      const power = context.filter(i => i.sector === 'power');"
    )
    new_summary = (
        "      const roads = context.filter(i => i.sector === 'roads');\n"
        "      const roadHazards = roads.filter(i => String(i.event_category || i.subtype || '').toLowerCase() === 'hazard');\n"
        "      const roadFlooding = roads.filter(i => String(i.event_category || i.subtype || '').toLowerCase() === 'flooding');\n"
        "      const power = context.filter(i => i.sector === 'power');"
    )
    if old_summary in text:
        text = text.replace(old_summary, new_summary, 1)

    old_values = (
        "      document.getElementById('roadsValue').textContent = fmt(closures.length);\n"
        "      document.getElementById('roadsDetail').textContent = `${fmt(restrictions.length)} restrictions`;"
    )
    new_values = (
        "      document.getElementById('roadsValue').textContent = fmt(roads.length);\n"
        "      document.getElementById('roadsDetail').textContent = `${fmt(roadHazards.length)} hazards · ${fmt(roadFlooding.length)} flooding`;"
    )
    if old_values in text:
        text = text.replace(old_values, new_values, 1)

    INDEX.write_text(text, encoding="utf-8")

'''


def apply_patch(text: str) -> str:
    if NEW_VERSION in text:
        return text
    if OLD_VERSION not in text:
        raise RuntimeError("refresh.py is not the expected v6 base; refusing an unsafe patch")

    text = text.replace(OLD_VERSION, NEW_VERSION, 1)

    block_start = text.index("QLDTRAFFIC_ALLOWED_EVENT_CATEGORIES =")
    block_end = text.index("\ndef property_value(", block_start)
    text = text[:block_start] + NEW_ROAD_BLOCK + text[block_end + 1:]

    old_kpi = '"roads": sum(1 for item in roads if item.get("subtype") == "closure"),'
    if old_kpi not in text:
        raise RuntimeError("Expected road KPI snapshot calculation was not found")
    text = text.replace(old_kpi, '"roads": len(roads),', 1)

    old_diag = (
        '                closure_count = sum(record.get("subtype") == "closure" for record in records)\n'
        '                restriction_count = sum(record.get("subtype") == "restriction" for record in records)\n'
        '                print(\n'
        '                    "QLDTraffic filtered records: "\n'
        '                    f"{len(records)} "\n'
        '                    f"(hazards={hazard_count}, flooding={flooding_count}, "\n'
        '                    f"closures={closure_count}, restrictions={restriction_count})"\n'
        '                )'
    )
    new_diag = (
        '                print(\n'
        '                    "QLDTraffic Hazard/Flooding incidents: "\n'
        '                    f"{len(records)} "\n'
        '                    f"(hazards={hazard_count}, flooding={flooding_count})"\n'
        '                )'
    )
    if old_diag not in text:
        raise RuntimeError("Expected QLDTraffic diagnostics block was not found")
    text = text.replace(old_diag, new_diag, 1)

    text = text.replace(
        '"Hazard/Flooding closure/restriction records."',
        '"verified Hazard/Flooding incidents."',
        1,
    )

    insert_before = "\ndef source_result("
    if insert_before not in text:
        raise RuntimeError("Could not locate source_result() insertion point")
    text = text.replace(insert_before, UI_PATCH_FUNCTION + insert_before, 1)

    old_write = (
        '    write_embedded(data)\n'
        '    print(f"Updated {INDEX.name}: {len(incidents)} total incidents")'
    )
    new_write = (
        '    write_embedded(data)\n'
        '    patch_dashboard_road_kpi()\n'
        '    print(f"Updated {INDEX.name}: {len(incidents)} total incidents")'
    )
    if old_write not in text:
        raise RuntimeError("Could not locate final dashboard write")
    text = text.replace(old_write, new_write, 1)

    compile(text, str(REFRESH), "exec")
    return text


def main() -> int:
    if not REFRESH.exists():
        raise SystemExit("refresh.py was not found beside this patch script")
    original = REFRESH.read_text(encoding="utf-8")
    REFRESH.write_text(apply_patch(original), encoding="utf-8")
    print("QLDTraffic mode: all current Hazard + Flooding incidents only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
