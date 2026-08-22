#!/usr/bin/env python3
"""Refresh the data embedded in index.html.

Run locally with:  python refresh.py
Then commit/push index.html to the GitHub Pages branch.

This script uses only the Python standard library. Each source is isolated; if a
source fails, its last successfully embedded records are retained and marked as
fallback data.
"""
from __future__ import annotations

import difflib
import hashlib
import html
from html.parser import HTMLParser
import http.cookiejar
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
AEST = timezone(timedelta(hours=10))
NOW = datetime.now(AEST)
NOW_ISO = NOW.isoformat(timespec="seconds")

SOURCES = {
    "qldtraffic": {
        "name": "QLDTraffic",
        "url": "https://data.qldtraffic.qld.gov.au/events_v2.geojson",
    },
    "energex": {
        "name": "Energex",
        "url": "https://services.arcgis.com/bfVzktoY0OhzQCDj/ArcGIS/rest/services/VwEnergexOutages/FeatureServer/0/query",
    },
    "ergon": {
        "name": "Ergon Energy",
        "url": "https://services.arcgis.com/33eHbTVqo7gtiCE8/arcgis/rest/services/VwErgonOutages/FeatureServer/0/query",
    },
    "schools": {
        "name": "Queensland school closures",
        "url": "https://closures.qld.edu.au/DataFiles/plain.txt",
    },
    "rail": {
        "name": "Translink train disruptions",
        "url": "https://translink.com.au/service-updates/rss/train",
    },
    "marine": {
        "name": "Maritime Safety Queensland",
        "url": "https://qldmaritime.msq.qld.gov.au/",
    },
    "geography": {
        "name": "Queensland LGA boundaries",
        "url": "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/Basemaps/FoundationData/FeatureServer/7/query",
    },
}

SCHOOL_MAPSERVER = "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/Society/SchoolsAndSchoolCatchments/MapServer"
SCHOOL_LAYERS = (4, 5, 6, 7, 8, 9)
DATA_START = "/*DATA_START*/"
DATA_END = "/*DATA_END*/"
POWER_ARCGIS_PARAMS = {
    "where": "1=1",
    "outFields": "*",
    "returnGeometry": "true",
    "outSR": "4326",
    "f": "geojson",
}


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def title_case(value: Any) -> str:
    return " ".join(word if i and word in {"of", "and", "the"} else word.capitalize() for i, word in enumerate(clean(value).lower().split()))


def stable_id(*parts: Any) -> str:
    raw = "|".join(clean(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]


def get_bytes(url: str, params: dict[str, Any] | None = None, timeout: int = 30) -> bytes:
    """Retrieve a public feed using browser-like headers.

    Energy Queensland's web firewall rejects obvious script user agents with
    HTTP 403. For Energex and Ergon, establish a normal website session first,
    retain its cookies, then request the GeoJSON with the same headers used by
    a browser. Other feeds use the same opener without the warm-up request.
    """
    if params:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urllib.parse.urlencode(params)}"

    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    is_energy_qld = host in {"www.energex.com.au", "www.ergon.com.au"}
    site_root = f"{parsed.scheme}://{parsed.netloc}"
    referer = (
        f"{site_root}/outages/outage-finder"
        if host == "www.energex.com.au"
        else f"{site_root}/network/outages/outage-finder"
        if host == "www.ergon.com.au"
        else site_root + "/"
    )

    browser_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/geo+json, application/json, text/plain, application/xml, text/xml, */*",
        "Accept-Language": "en-AU,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    cookie_jar = http.cookiejar.CookieJar()
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPCookieProcessor(cookie_jar),
    )

    # Warm up the Energy Queensland session so any edge/WAF cookies issued by
    # the public outage page are sent with the subsequent GeoJSON request.
    if is_energy_qld:
        warm_headers = dict(browser_headers)
        warm_headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        })
        try:
            warm_request = urllib.request.Request(referer, headers=warm_headers)
            with opener.open(warm_request, timeout=timeout) as response:
                response.read(1024)
        except Exception:
            # The feed request may still succeed without the warm-up page.
            pass

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request_url = url
            if is_energy_qld:
                separator = "&" if "?" in request_url else "?"
                request_url = f"{request_url}{separator}_={int(time.time())}"
            request = urllib.request.Request(request_url, headers=browser_headers)
            with opener.open(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)

    hint = ""
    if is_energy_qld and "403" in str(last_error):
        hint = " (Energy Queensland blocked the GitHub runner despite browser headers)"
    raise RuntimeError(f"Unable to retrieve {url}: {last_error}{hint}")


def get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return json.loads(get_bytes(url, params).decode("utf-8-sig"))


def parse_date(value: Any) -> datetime | None:
    # The ArcGIS outage layers return START and EST_FIX_TIME as Unix epoch
    # milliseconds. Retain support for epoch seconds and the existing text dates.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000.0
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(AEST)
        except (OverflowError, OSError, ValueError):
            return None

    text = clean(value)
    if not text:
        return None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        try:
            timestamp = float(text)
            if timestamp > 10_000_000_000:
                timestamp /= 1000.0
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(AEST)
        except (OverflowError, OSError, ValueError):
            return None

    candidates = (text, text.replace("Z", "+00:00"))
    for candidate in candidates:
        try:
            result = datetime.fromisoformat(candidate)
            if result.tzinfo is None:
                result = result.replace(tzinfo=AEST)
            return result.astimezone(AEST)
        except ValueError:
            pass
    for fmt in ("%I:%M%p %d %b %Y", "%I:%M %p %d %b %Y", "%a, %d %b %Y %H:%M:%S %z", "%d/%m/%Y %H:%M"):
        try:
            result = datetime.strptime(text, fmt)
            if result.tzinfo is None:
                result = result.replace(tzinfo=AEST)
            return result.astimezone(AEST)
        except ValueError:
            pass
    return None


def iso(value: Any) -> str | None:
    parsed = value if isinstance(value, datetime) else parse_date(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def active_now(start: Any, end: Any) -> bool:
    start_dt, end_dt = parse_date(start), parse_date(end)
    return not ((start_dt and start_dt > NOW + timedelta(minutes=3)) or (end_dt and end_dt < NOW - timedelta(minutes=3)))


def all_points(geometry: dict[str, Any] | None) -> list[list[float]]:
    points: list[list[float]] = []
    if not geometry:
        return points
    if geometry.get("type") == "GeometryCollection":
        for item in geometry.get("geometries", []):
            points.extend(all_points(item))
        return points

    def walk(value: Any) -> None:
        if isinstance(value, list) and len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
            points.append([float(value[0]), float(value[1])])
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(geometry.get("coordinates"))
    return points


def representative_point(geometry: dict[str, Any] | None) -> list[float] | None:
    if not geometry:
        return None
    if geometry.get("type") == "GeometryCollection":
        for item in geometry.get("geometries", []):
            if item.get("type") == "Point":
                coords = item.get("coordinates") or []
                if len(coords) >= 2:
                    return [float(coords[0]), float(coords[1])]
    if geometry.get("type") == "Point":
        coords = geometry.get("coordinates") or []
        return [float(coords[0]), float(coords[1])] if len(coords) >= 2 else None
    points = all_points(geometry)
    if not points:
        return None
    return [round(sum(p[0] for p in points) / len(points), 6), round(sum(p[1] for p in points) / len(points), 6)]


def point_in_ring(point: list[float], ring: list[list[float]]) -> bool:
    x, y = point
    inside = False
    if len(ring) < 3:
        return False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][:2]
        xj, yj = ring[j][:2]
        if ((yi > y) != (yj > y)) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def point_in_geometry(point: list[float], geometry: dict[str, Any] | None) -> bool:
    if not geometry:
        return False
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    polygons = [coordinates] if kind == "Polygon" else coordinates if kind == "MultiPolygon" else []
    for polygon in polygons:
        if polygon and point_in_ring(point, polygon[0]) and not any(point_in_ring(point, hole) for hole in polygon[1:]):
            return True
    return False


def lga_name(properties: dict[str, Any]) -> str:
    lowered = {norm(key).replace(" ", "_"): value for key, value in properties.items()}
    value = lowered.get("adminareaname") or lowered.get("admin_area_name") or lowered.get("name") or lowered.get("display_name")
    return title_case(value) if value else "Unknown LGA"


def locate_lga(point: list[float] | None, lgas: dict[str, Any]) -> str | None:
    if not point:
        return None
    for feature in lgas.get("features", []):
        if point_in_geometry(point, feature.get("geometry")):
            return feature.get("properties", {}).get("display_name")
    return None


def fetch_lgas() -> dict[str, Any]:
    raw = get_json(
        SOURCES["geography"]["url"],
        {
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "geojson",
            "geometryPrecision": "5",
            "maxAllowableOffset": "0.0025",
        },
    )
    features = []
    for feature in raw.get("features", []):
        name = lga_name(feature.get("properties") or {})
        features.append({"type": "Feature", "geometry": feature.get("geometry"), "properties": {"display_name": name}})
    if not features:
        raise RuntimeError("LGA service returned no features")
    return {"type": "FeatureCollection", "features": features}


















































































































































































# QLDTraffic filter: Hazard + Flooding only
# Queensland-only road geography filter
QLDTRAFFIC_ALLOWED_EVENT_CATEGORIES = {"hazard", "flooding"}

# Fast coarse bounds used before the exact Queensland LGA check.
# Coordinates are GeoJSON order: [longitude, latitude].
QLDTRAFFIC_QLD_BOUNDS = {
    "min_lon": 137.8,
    "max_lon": 153.7,
    "min_lat": -29.25,
    "max_lat": -9.0,
}


def qldtraffic_event_category(properties: dict[str, Any]) -> str | None:
    category = norm(properties.get("event_type"))
    return category if category in QLDTRAFFIC_ALLOWED_EVENT_CATEGORIES else None


def qldtraffic_in_qld_bounds(point: list[float] | None) -> bool:
    if not point or len(point) < 2:
        return False
    lon, lat = float(point[0]), float(point[1])
    return (
        QLDTRAFFIC_QLD_BOUNDS["min_lon"] <= lon <= QLDTRAFFIC_QLD_BOUNDS["max_lon"]
        and QLDTRAFFIC_QLD_BOUNDS["min_lat"] <= lat <= QLDTRAFFIC_QLD_BOUNDS["max_lat"]
    )


def qldtraffic_coordinate_in_queensland(
    point: list[float] | None,
    lgas: dict[str, Any],
) -> tuple[bool, str | None]:
    if not qldtraffic_in_qld_bounds(point):
        return False, None

    # When the embedded Queensland LGA polygons are available, use
    # them as the authoritative state-boundary test.
    if lgas.get("features"):
        lga = locate_lga(point, lgas)
        return (lga is not None), lga

    # If LGA geometry is temporarily unavailable, retain the coarse
    # Queensland bounding-box safeguard rather than displaying
    # obviously interstate records.
    return True, None


def qldtraffic_location_in_queensland(
    geometry: dict[str, Any] | None,
    lgas: dict[str, Any],
) -> tuple[list[float] | None, str | None]:
    # Try the representative point first. For line/polygon events near
    # a border, also sample several actual geometry vertices so a
    # centroid outside Queensland does not discard a valid QLD event.
    candidates: list[list[float]] = []

    representative = representative_point(geometry)
    if representative:
        candidates.append(representative)

    points = all_points(geometry)
    if points:
        indexes = (
            0,
            len(points) // 4,
            len(points) // 2,
            (3 * len(points)) // 4,
            len(points) - 1,
        )
        for index in indexes:
            point = points[index]
            if point not in candidates:
                candidates.append(point)

    for point in candidates:
        inside, lga = qldtraffic_coordinate_in_queensland(point, lgas)
        if inside:
            return point, lga

    return None, None


def qldtraffic_fallback_records(
    records: list[dict[str, Any]],
    lgas: dict[str, Any],
) -> list[dict[str, Any]]:
    # Cached road records must satisfy BOTH the event-category filter
    # and the Queensland geography filter.
    filtered = []
    for item in records:
        category = norm(item.get("event_category") or item.get("event_type"))
        if category not in QLDTRAFFIC_ALLOWED_EVENT_CATEGORIES:
            continue

        inside, _ = qldtraffic_coordinate_in_queensland(
            item.get("coordinates"),
            lgas,
        )
        if inside:
            filtered.append(item)

    return filtered


def parse_roads(payload: dict[str, Any], lgas: dict[str, Any]) -> list[dict[str, Any]]:
    incidents = []

    for feature in payload.get("features", []):
        props = feature.get("properties") or {}

        # KPI inclusion filter: QLDTraffic Hazards + Flooding only.
        event_category = qldtraffic_event_category(props)
        if event_category is None:
            continue

        if norm(props.get("status")) not in {"", "active", "published"}:
            continue

        duration = props.get("duration") or {}
        if not active_now(duration.get("start"), duration.get("end")):
            continue

        summary = props.get("road_summary") or {}
        geometry = feature.get("geometry")

        # Map/state inclusion filter: Queensland only.
        coords, qld_lga = qldtraffic_location_in_queensland(geometry, lgas)
        if coords is None:
            continue

        impact = props.get("impact") or {}
        impact_type = clean(impact.get("impact_type"))
        impact_subtype = clean(impact.get("impact_subtype"))
        combined = norm(f"{impact_type} {impact_subtype}")

        if "closure" in combined or "closed" in combined:
            subtype = "closure"
        elif "lane" in combined or "restriction" in combined:
            subtype = "restriction"
        else:
            subtype = "incident"

        # Prefer the LGA proved by the Queensland polygon check.
        lga = qld_lga or clean(summary.get("local_government_area"))
        road = clean(summary.get("road_name")) or "Queensland road"

        event_type = clean(props.get("event_type")) or title_case(event_category)
        event_subtype = clean(props.get("event_subtype"))
        event_due_to = clean(props.get("event_due_to"))
        status = impact_subtype or impact_type or event_subtype or event_type

        incidents.append({
            "id": f"roads-{props.get('id') or stable_id(road, event_type, duration.get('start'))}",
            "sector": "roads",
            "subtype": subtype,
            "event_category": event_category,
            "event_type": event_type,
            "event_subtype": event_subtype,
            "event_due_to": event_due_to,
            "title": f"{road}: {status or event_type}",
            "description": clean(props.get("description")),
            "status": status,
            "lga": lga,
            "locality": clean(summary.get("locality")),
            "coordinates": coords,
            "geometry": geometry,
            "customers": 0,
            "planned": False,
            "updated": iso(props.get("last_updated")) or NOW_ISO,
            "source_name": "QLDTraffic",
            "source_url": clean(props.get("url") or props.get("web_link"))
                or SOURCES["qldtraffic"]["url"],
        })

    return incidents


def property_value(properties: dict[str, Any], *names: str) -> Any:
    index = {re.sub(r"[^a-z0-9]", "", str(key).lower()): value for key, value in properties.items()}
    for name in names:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if key in index:
            return index[key]
    return None


def parse_int(value: Any) -> int:
    match = re.search(r"[\d,]+", clean(value))
    return int(match.group(0).replace(",", "")) if match else 0


def power_geometry(geometry: dict[str, Any] | None) -> tuple[list[float] | None, dict[str, Any] | None]:
    if not geometry:
        return None, None
    if geometry.get("type") != "GeometryCollection":
        return representative_point(geometry), geometry if geometry.get("type") in {"Polygon", "MultiPolygon"} else None
    point = None
    polygons = []
    for item in geometry.get("geometries", []):
        if item.get("type") == "Point" and point is None:
            point = representative_point(item)
        elif item.get("type") in {"Polygon", "MultiPolygon"}:
            polygons.append(item)
    footprint = polygons[0] if len(polygons) == 1 else {"type": "GeometryCollection", "geometries": polygons} if polygons else None
    return point or representative_point(geometry), footprint


def parse_power(payload: dict[str, Any], provider: str, source_key: str, lgas: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("type") != "FeatureCollection":
        raise RuntimeError("Power source is not a GeoJSON FeatureCollection")
    incidents = []
    for feature in payload.get("features", []):
        props = feature.get("properties") or {}
        outage_type = clean(property_value(props, "TYPE", "OUTAGE_TYPE"))
        if outage_type and "unplanned" not in norm(outage_type):
            continue
        event_id = clean(property_value(props, "EVENT_ID", "ID")) or stable_id(provider, props, feature.get("geometry"))
        customers = parse_int(property_value(props, "CUSTOMERS_AFFECTED", "CUSTOMERS", "CUSTOMER_COUNT"))
        suburbs = clean(property_value(props, "SUBURBS", "SUBURB", "LOCALITIES", "LOCALITY"))
        locality = title_case(re.split(r"[,;/]", suburbs)[0]) if suburbs else "Queensland"
        coords, footprint = power_geometry(feature.get("geometry"))
        lga = locate_lga(coords, lgas)
        reason = clean(property_value(props, "REASON", "CAUSE"))
        streets = clean(property_value(props, "STREETS"))
        description = ". ".join(part for part in (reason, f"Affected streets: {streets}" if streets else "") if part)
        incidents.append({
            "id": f"power-{source_key}-{norm(event_id).replace(' ', '-')}",
            "sector": "power",
            "subtype": "unplanned",
            "title": f"{locality} unplanned power outage",
            "description": description,
            "status": clean(property_value(props, "STATUS")) or "Outage reported",
            "lga": lga,
            "locality": title_case(suburbs),
            "coordinates": coords,
            "geometry": footprint,
            "customers": customers,
            "planned": False,
            "updated": NOW_ISO,
            "estimated_restore": iso(property_value(props, "EST_FIX_TIME", "ESTIMATED_RESTORATION", "ETR")) or clean(property_value(props, "EST_FIX_TIME", "ESTIMATED_RESTORATION", "ETR")),
            "source_name": provider,
            "source_url": SOURCES[source_key]["url"],
        })
    return incidents


def parse_school_sections(text: str) -> list[tuple[str, str]]:
    headings = {"state school closures": "State", "independent school closures": "Independent", "catholic school closures": "Catholic"}
    sector = None
    result = []
    for raw in text.splitlines():
        line = clean(raw)
        key = norm(line)
        if key in headings:
            sector = headings[key]
            continue
        if key.startswith("early childhood"):
            sector = None
        if not sector or "there are no current closures" in key:
            continue
        if line.lstrip().startswith(("*", "•", "-")):
            item = clean(line.lstrip("*•- "))
            if item:
                result.append((sector, item))
    return result


def school_name(line: str) -> str:
    return clean(re.split(r"\s+[–—-]\s+(?:closed|closure|until|from|due|campus)", line, maxsplit=1, flags=re.I)[0])


def school_directory(lgas: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    lookup: dict[str, list[dict[str, Any]]] = {}
    for layer in SCHOOL_LAYERS:
        payload = get_json(
            f"{SCHOOL_MAPSERVER}/{layer}/query",
            {"where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": "4326", "f": "geojson"},
        )
        for feature in payload.get("features", []):
            props = {norm(key).replace(" ", "_"): value for key, value in (feature.get("properties") or {}).items()}
            name = clean(props.get("centre_name") or props.get("school_name") or props.get("name") or props.get("facility_name"))
            if not name:
                continue
            coords = representative_point(feature.get("geometry"))
            sector_text = norm(props.get("school_sector") or props.get("sector") or props.get("non_state_sector") or props.get("authority"))
            sector = "State" if layer != 9 else "Catholic" if "catholic" in sector_text else "Independent" if "independent" in sector_text else "Non-State"
            lookup.setdefault(norm(name), []).append({
                "name": name,
                "coordinates": coords,
                "lga": locate_lga(coords, lgas),
                "locality": clean(props.get("locality") or props.get("suburb") or props.get("town") or props.get("physical_suburb")),
                "sector": sector,
            })
    return lookup


def parse_schools(text: str, lgas: dict[str, Any]) -> list[dict[str, Any]]:
    closures = parse_school_sections(text)
    if not closures:
        return []
    directory = school_directory(lgas)
    incidents = []
    for sector, raw_line in closures:
        name = school_name(raw_line)
        key = norm(name)
        candidates = directory.get(key, [])
        if not candidates:
            match = difflib.get_close_matches(key, directory.keys(), n=1, cutoff=0.84)
            candidates = directory.get(match[0], []) if match else []
        matched = next((item for item in candidates if item["sector"] == sector or item["sector"] == "Non-State"), candidates[0] if candidates else None)
        incidents.append({
            "id": f"schools-{stable_id(sector, name)}",
            "sector": "schools",
            "subtype": "closure",
            "title": name,
            "description": raw_line,
            "status": "Closed",
            "lga": matched.get("lga") if matched else None,
            "locality": matched.get("locality") if matched else None,
            "coordinates": matched.get("coordinates") if matched else None,
            "geometry": None,
            "customers": 0,
            "planned": False,
            "updated": NOW_ISO,
            "source_name": "Queensland Department of Education",
            "source_url": "https://closures.qld.edu.au/",
            "school_sector": sector,
        })
    return incidents


def strip_html(value: str) -> str:
    return clean(html.unescape(re.sub(r"<[^>]+>", " ", value or "")))


def parse_rail(xml_data: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_data)
    incidents = []
    terms = ("closed", "closure", "suspended", "not running", "no trains", "replacement bus", "cancelled", "canceled", "major delay", "track work")
    for item in root.findall(".//item"):
        def value(tag: str) -> str:
            element = item.find(tag)
            return clean(element.text if element is not None else "")
        title = value("title") or "Rail service update"
        description = strip_html(value("description"))
        combined = norm(f"{title} {description}")
        if not any(term in combined for term in terms):
            continue
        closure = any(term in combined for term in ("closed", "closure", "suspended", "not running", "no trains"))
        link = value("link") or SOURCES["rail"]["url"]
        incidents.append({
            "id": f"rail-{stable_id(title, link)}",
            "sector": "rail",
            "subtype": "closure" if closure else "disruption",
            "title": title,
            "description": description,
            "status": "Closure / suspension" if closure else "Significant disruption",
            "lga": None,
            "locality": "Queensland rail network",
            "coordinates": None,
            "geometry": None,
            "customers": 0,
            "planned": "track work" in combined or "planned" in combined,
            "updated": iso(value("pubDate")) or NOW_ISO,
            "source_name": "Translink",
            "source_url": link,
        })
    return incidents






# Marine Warnings KPI: Maritime Safety Queensland v1
# Marine exact imsBulletin source v9
# The workflow captures Guardian's /dashboard/imsBulletin FeatureCollection and writes verified current records to /tmp/msq-current-warnings.json.
MSQ_BROWSER_SNAPSHOT = Path("/tmp/msq-current-warnings.json")

MSQ_QLD_BOUNDS = {"min_lon": 137.8, "max_lon": 154.2, "min_lat": -29.3, "max_lat": -9.0}
MSQ_LOCATION_POINTS = (
    (("rockhampton", "fitzroy river"), "Rockhampton / Fitzroy River", [150.510, -23.380]),
    (("port alma",), "Port Alma", [150.860, -23.590]),
    (("gladstone", "port curtis"), "Gladstone", [151.250, -23.840]),
    (("bundaberg", "burnett river"), "Bundaberg / Burnett River", [152.390, -24.760]),
    (("hervey bay",), "Hervey Bay", [152.900, -25.280]),
    (("wide bay", "tin can bay"), "Wide Bay", [152.780, -25.820]),
    (("mooloolah river", "mooloolaba"), "Mooloolaba / Mooloolah River", [153.126, -26.681]),
    (("maroochy river", "maroochydore"), "Maroochy River", [153.089, -26.650]),
    (("noosa",), "Noosa", [153.102, -26.382]),
    (("pumicestone passage", "golden beach", "bribie island"), "Pumicestone Passage", [153.112, -27.005]),
    (("moreton bay", "cape moreton", "moreton island"), "Moreton Bay", [153.250, -27.200]),
    (("port of brisbane", "brisbane pilotage", "brisbane port"), "Port of Brisbane", [153.170, -27.380]),
    (("redland bay", "weinam creek", "weinam"), "Redland Bay", [153.302, -27.612]),
    (("southport", "gold coast seaway", "gold coast"), "Southport / Gold Coast", [153.431, -27.967]),
    (("mackay",), "Mackay", [149.230, -21.120]), (("hay point",), "Hay Point", [149.300, -21.290]),
    (("whitsunday", "airlie beach"), "Whitsundays", [148.720, -20.270]), (("bowen",), "Bowen", [148.250, -20.010]),
    (("abbot point",), "Abbot Point", [148.084, -19.878]), (("townsville",), "Townsville", [146.830, -19.250]),
    (("lucinda", "hinchinbrook"), "Lucinda / Hinchinbrook", [146.330, -18.530]), (("mourilyan",), "Mourilyan", [146.120, -17.600]),
    (("cairns",), "Cairns", [145.780, -16.920]), (("port douglas",), "Port Douglas", [145.460, -16.480]),
    (("daintree river",), "Daintree River", [145.460, -16.290]), (("cooktown",), "Cooktown", [145.250, -15.460]),
    (("cape flattery",), "Cape Flattery", [145.310, -14.960]), (("weipa",), "Weipa", [141.870, -12.680]),
    (("amrun",), "Amrun", [141.650, -12.900]), (("skardon river",), "Skardon River", [142.050, -11.790]),
    (("thursday island", "torres strait"), "Thursday Island / Torres Strait", [142.220, -10.580]),
    (("karumba",), "Karumba", [140.830, -17.490]), (("gulf of carpentaria",), "Gulf of Carpentaria", [140.900, -16.500]),
)

def _msq_point_in_qld(point: list[float] | None) -> bool:
    if not point or len(point) < 2: return False
    try: lon, lat = float(point[0]), float(point[1])
    except (TypeError, ValueError): return False
    return MSQ_QLD_BOUNDS["min_lon"] <= lon <= MSQ_QLD_BOUNDS["max_lon"] and MSQ_QLD_BOUNDS["min_lat"] <= lat <= MSQ_QLD_BOUNDS["max_lat"]

def _msq_location_from_lga(value: str, lgas: dict[str, Any]) -> tuple[list[float] | None, str | None]:
    warning_key = norm(value); best: tuple[int, list[float], str] | None = None
    for feature in lgas.get("features", []):
        props = feature.get("properties") or {}; names: list[str] = []
        for prop_key, prop_value in props.items():
            field = str(prop_key).lower()
            if prop_value and ("lga" in field or "name" in field or "local_government" in field):
                name = clean(prop_value)
                if name: names.append(name)
        for name in names:
            name_key = norm(name)
            if len(name_key) < 4 or name_key not in warning_key: continue
            point = representative_point(feature.get("geometry"))
            if point and _msq_point_in_qld(point):
                score = len(name_key)
                if best is None or score > best[0]: best = (score, point, name)
    if best: return [round(float(best[1][0]), 6), round(float(best[1][1]), 6)], best[2]
    return None, None

def _msq_warning_location(value: str, lgas: dict[str, Any]) -> tuple[list[float] | None, str, str | None, str]:
    warning_key = norm(value)
    for aliases, locality, point in MSQ_LOCATION_POINTS:
        if any(norm(alias) in warning_key for alias in aliases):
            lga = locate_lga(point, lgas) if lgas.get("features") else None
            return list(point), locality, lga, "representative"
    point, lga = _msq_location_from_lga(value, lgas)
    if point: return point, lga or clean(value), lga, "lga"
    return None, clean(value) or "Area identified by MSQ", None, "unmapped"

def _msq_snapshot() -> dict[str, Any]:
    if not MSQ_BROWSER_SNAPSHOT.exists(): raise RuntimeError("verified MSQ browser snapshot was not created by the workflow")
    try: payload = json.loads(MSQ_BROWSER_SNAPSHOT.read_text(encoding="utf-8"))
    except Exception as exc: raise RuntimeError(f"verified MSQ browser snapshot is unreadable: {exc}") from exc
    if payload.get("schema") != "msq-browser-current-v2": raise RuntimeError("verified MSQ browser snapshot has an unexpected schema")
    if payload.get("status") != "current": raise RuntimeError(clean(payload.get("error")) or "MSQ browser source could not be verified")
    warnings = payload.get("warnings")
    if not isinstance(warnings, list): raise RuntimeError("verified MSQ browser snapshot does not contain a warning list")
    if not warnings and not bool(payload.get("verified_zero")): raise RuntimeError("MSQ snapshot contains zero records without an explicit verified no-warning state")
    return payload

def parse_msq_marine_warnings(lgas: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _msq_snapshot(); incidents: list[dict[str, Any]] = []
    for item in payload.get("warnings", []):
        if not isinstance(item, dict): continue
        title = clean(item.get("title")); area = clean(item.get("area")); description = clean(item.get("description"))
        if not title or not area: raise RuntimeError("verified MSQ warning is missing its rendered title or area")
        method = clean(item.get("source_method"))
        if method not in {"msq-imsBulletin-feature", "rendered-visible-card", "rendered-visible-text-window", "structured-browser-network"}:
            raise RuntimeError("MSQ warning was not sourced from the verified imsBulletin/browser evidence path")
        source_point = item.get("coordinates")
        if _msq_point_in_qld(source_point):
            coordinates = [round(float(source_point[0]), 6), round(float(source_point[1]), 6)]
            locality = area
            lga = locate_lga(coordinates, lgas) if lgas.get("features") else None
            location_precision = clean(item.get("location_precision")) or "source-area-centroid"
        else:
            coordinates, locality, lga, location_precision = _msq_warning_location(" ".join(filter(None, (area, title, description))), lgas)
        incident_id = clean(item.get("feature_id")) or stable_id(area, title)
        incidents.append({
            "id": f"marine-{incident_id}", "sector": "marine", "subtype": "warning", "event_category": "maritime warning",
            "title": title[:220], "description": description[:3500], "status": clean(item.get("status")) or "Active maritime warning",
            "marine_alert_phase": clean(item.get("alert_phase")) or None, "marine_warning_level": clean(item.get("warning_level")) or None,
            "marine_action": clean(item.get("action")) or None, "marine_issued_text": clean(item.get("issued_text")) or None,
            "lga": lga, "locality": locality, "coordinates": coordinates, "geometry": None, "location_precision": location_precision,
            "customers": 0, "planned": False,
            "updated": clean(item.get("updated_iso")) or clean(payload.get("published")) or clean(payload.get("captured_at")) or NOW_ISO,
            "source_name": "Maritime Safety Queensland", "source_url": clean(item.get("source_url")) or SOURCES["marine"]["url"],
            "source_method": "msq-imsBulletin-current-warning", "marine_feature_id": clean(item.get("feature_id")) or None,
        })
    return incidents


def read_embedded() -> dict[str, Any]:
    text = INDEX.read_text(encoding="utf-8")
    start, end = text.index(DATA_START) + len(DATA_START), text.index(DATA_END)
    return json.loads(text[start:end].strip())


def write_embedded(data: dict[str, Any]) -> None:
    text = INDEX.read_text(encoding="utf-8")
    start, end = text.index(DATA_START) + len(DATA_START), text.index(DATA_END)
    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    INDEX.write_text(text[:start] + compact + text[end:], encoding="utf-8")


def source_result(name: str, status: str, count: int, error: str | None = None) -> dict[str, Any]:
    return {
        "name": SOURCES[name]["name"],
        "url": SOURCES[name]["url"],
        "status": status,
        "count": count,
        "retrieved_at": NOW_ISO,
        "error": clean(error)[:300] if error else None,
    }


# KPI trend history v2 (includes Marine)
HISTORY_RETENTION = timedelta(days=7)
HISTORY_MIN_SPACING = timedelta(minutes=10)
HISTORY_MAX_POINTS = 700


def _kpi_history_incident_lgas(incident: dict[str, Any], official_names: set[str]) -> set[str]:
    """Return official Queensland LGA labels represented by one incident."""
    aliases = {"Moreton Bay Regional": "City of Moreton Bay"}
    names: set[str] = set()
    raw = clean(incident.get("lga"))
    if not raw:
        return names
    for part in re.split(r"\s*/\s*|\s*;\s*", raw):
        name = aliases.get(clean(part), clean(part))
        if name and (not official_names or name in official_names):
            names.add(name)
    return names


def calculate_kpi_snapshot(incidents: list[dict[str, Any]], lgas: dict[str, Any], at: str) -> dict[str, Any]:
    """Create compact statewide values used by the KPI trend visuals."""
    roads = [item for item in incidents if item.get("sector") == "roads"]
    power = [item for item in incidents if item.get("sector") == "power"]
    rail = [item for item in incidents if item.get("sector") == "rail"]
    schools = [item for item in incidents if item.get("sector") == "schools"]
    marine = [item for item in incidents if item.get("sector") == "marine"]
    official_names = {
        clean(feature.get("properties", {}).get("display_name"))
        for feature in lgas.get("features", [])
        if clean(feature.get("properties", {}).get("display_name"))
    }
    affected_lgas: set[str] = set()
    for incident in incidents:
        affected_lgas.update(_kpi_history_incident_lgas(incident, official_names))
    return {
        "at": at,
        "roads": len(roads),
        "power_customers": sum(int(item.get("customers") or 0) for item in power),
        "power_outages": len(power),
        "rail": len(rail),
        "schools": len(schools),
        "marine": len(marine),
        "lgas": len(affected_lgas),
    }


def update_kpi_history(previous: dict[str, Any], incidents: list[dict[str, Any]], lgas: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve, append and prune KPI history across scheduled refreshes."""
    history = [
        item for item in previous.get("history", [])
        if isinstance(item, dict) and item.get("at")
    ]
    if not history and previous.get("generated_at"):
        history.append(
            calculate_kpi_snapshot(
                previous.get("incidents", []),
                lgas,
                clean(previous.get("generated_at")),
            )
        )

    current = calculate_kpi_snapshot(incidents, lgas, NOW_ISO)
    last_at = parse_date(history[-1].get("at")) if history else None
    if last_at and NOW - last_at < HISTORY_MIN_SPACING:
        history[-1] = current
    else:
        history.append(current)

    cutoff = NOW - HISTORY_RETENTION
    retained: list[dict[str, Any]] = []
    for item in history:
        item_at = parse_date(item.get("at"))
        if item_at and item_at >= cutoff:
            retained.append(item)
    return retained[-HISTORY_MAX_POINTS:]

def main() -> int:
    if not INDEX.exists():
        print("index.html was not found beside refresh.py", file=sys.stderr)
        return 1
    previous = read_embedded()
    previous_incidents = previous.get("incidents", [])
    previous_by_source = {
        key: [item for item in previous_incidents if item.get("source_key") == key]
        for key in SOURCES
    }
    sources: dict[str, Any] = {}

    # LGA boundaries change rarely. Reuse the last embedded copy so the
    # 15-minute refresh only downloads them on the first successful run.
    lgas = previous.get("lgas") or {"type": "FeatureCollection", "features": []}
    if lgas.get("features"):
        sources["geography"] = source_result("geography", "current", len(lgas.get("features", [])))
    else:
        try:
            lgas = fetch_lgas()
            sources["geography"] = source_result("geography", "current", len(lgas.get("features", [])))
        except Exception as exc:  # noqa: BLE001
            sources["geography"] = source_result("geography", "error", 0, str(exc))

    incidents: list[dict[str, Any]] = []

    jobs = [
        ("qldtraffic", lambda: parse_roads(get_json(SOURCES["qldtraffic"]["url"]), lgas)),
        ("energex", lambda: parse_power(get_json(SOURCES["energex"]["url"], POWER_ARCGIS_PARAMS), "Energex", "energex", lgas)),
        ("ergon", lambda: parse_power(get_json(SOURCES["ergon"]["url"], POWER_ARCGIS_PARAMS), "Ergon Energy", "ergon", lgas)),
        ("schools", lambda: parse_schools(get_bytes(SOURCES["schools"]["url"]).decode("utf-8-sig"), lgas)),
        ("rail", lambda: parse_rail(get_bytes(SOURCES["rail"]["url"]))),
        ("marine", lambda: parse_msq_marine_warnings(lgas)),
    ]

    for key, job in jobs:
        try:
            records = job()
            for record in records:
                record["source_key"] = key
            incidents.extend(records)
            sources[key] = source_result(key, "current", len(records))
            print(f"{SOURCES[key]['name']}: {len(records)} records")
        except Exception as exc:  # noqa: BLE001
            fallback = (
                qldtraffic_fallback_records(previous_by_source.get(key, []), lgas)
                if key == "qldtraffic"
                else []
                if key == "marine"
                else previous_by_source.get(key, [])
            )
            incidents.extend(fallback)
            sources[key] = source_result(key, "fallback" if fallback else "error", len(fallback), str(exc))
            print(f"{SOURCES[key]['name']}: ERROR - {exc}")

    history = update_kpi_history(previous, incidents, lgas)

    data = {
        "generated_at": NOW_ISO,
        "notice": "Snapshot refreshed automatically by GitHub. Scheduled runs occur every 15 minutes; individual source publication times may differ.",
        "incidents": incidents,
        "history": history,
        "lgas": lgas,
        "sources": sources,
    }
    write_embedded(data)
    print(f"Updated {INDEX.name}: {len(incidents)} total incidents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
