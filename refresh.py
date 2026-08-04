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

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None  # type: ignore[assignment]

SCRIPT_VERSION = "2026-08-05-operational-change-trends-v3"

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
AEST = timezone(timedelta(hours=10))
ESSENTIAL_TZ = ZoneInfo("Australia/Sydney") if ZoneInfo is not None else AEST
NOW = datetime.now(AEST)
NOW_ISO = NOW.isoformat(timespec="seconds")

# Exact working Essential Energy KML feed used by the supplied ZIP pipeline.
ESSENTIAL_ENERGY_KML_URL = "https://www.essentialenergy.com.au/Assets/kmz/current.kml?dFdLgoAP"

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
    "essential": {
        "name": "Essential Energy",
        "url": ESSENTIAL_ENERGY_KML_URL,
    },
    "schools": {
        "name": "Queensland school closures",
        "url": "https://closures.qld.edu.au/DataFiles/plain.txt",
    },
    "rail": {
        "name": "Translink train disruptions",
        "url": "https://translink.com.au/service-updates/rss/train",
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

# Human-readable public pages used by incident hyperlinks. Feed/API URLs remain
# in SOURCES for retrieval and source-health reporting only.
OFFICIAL_SOURCE_URLS = {
    "qldtraffic": "https://qldtraffic.qld.gov.au/?tab=incident",
    "energex": "https://www.energex.com.au/outages/outage-finder/emergency-outages-text-view/",
    "ergon": "https://www.ergon.com.au/network/outages/outage-finder/outage-finder-text-view",
    "essential": "https://www.essentialenergy.com.au/outages-and-faults/power-outages",
    "schools": "https://closures.qld.edu.au/",
}

# KPI history is embedded beside the current snapshot so GitHub Pages remains
# self-contained. At a 15-minute refresh cadence, seven days is about 672 small
# records and adds only a modest amount to index.html.
HISTORY_RETENTION = timedelta(days=7)
HISTORY_MIN_SPACING = timedelta(minutes=10)
HISTORY_MAX_POINTS = 700
CHANGE_HISTORY_VERSION = 3
LGA_ALIASES = {"Moreton Bay Regional": "City of Moreton Bay"}


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


def get_essential_kml(url: str = ESSENTIAL_ENERGY_KML_URL, timeout: int = 30) -> bytes:
    """Retrieve Essential Energy's live KML using the working ZIP's headers."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/vnd.google-earth.kml+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-AU,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.essentialenergy.com.au/",
    }
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
                payload = response.read()
            if b"<kml" not in payload[:5000].lower() and b"<placemark" not in payload.lower():
                raise RuntimeError("Essential Energy response was not KML")
            return payload
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Unable to retrieve Essential Energy KML: {last_error}")


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


def parse_roads(payload: dict[str, Any], lgas: dict[str, Any]) -> list[dict[str, Any]]:
    incidents = []
    for feature in payload.get("features", []):
        props = feature.get("properties") or {}
        if norm(props.get("status")) not in {"", "active", "published"}:
            continue
        impact = props.get("impact") or {}
        combined = norm(f"{impact.get('impact_type')} {impact.get('impact_subtype')}")
        if "closure" in combined or "closed" in combined:
            subtype = "closure"
        elif "lane" in combined or "restriction" in combined:
            subtype = "restriction"
        else:
            continue
        duration = props.get("duration") or {}
        if not active_now(duration.get("start"), duration.get("end")):
            continue
        summary = props.get("road_summary") or {}
        geometry = feature.get("geometry")
        coords = representative_point(geometry)
        lga = clean(summary.get("local_government_area")) or locate_lga(coords, lgas)
        road = clean(summary.get("road_name")) or "Queensland road"
        status = clean(impact.get("impact_subtype") or impact.get("impact_type")) or title_case(subtype)
        incidents.append({
            "id": f"roads-{props.get('id') or stable_id(road, status, duration.get('start'))}",
            "sector": "roads",
            "subtype": subtype,
            "title": f"{road}: {status}",
            "description": clean(props.get("description")),
            "status": status,
            "lga": lga,
            "locality": clean(summary.get("locality")),
            "coordinates": coords,
            "geometry": geometry,
            "customers": 0,
            "planned": norm(props.get("event_type")) == "roadworks",
            "updated": iso(props.get("last_updated")) or NOW_ISO,
            "source_name": "QLDTraffic",
            "source_url": OFFICIAL_SOURCE_URLS["qldtraffic"],
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
            "source_url": OFFICIAL_SOURCE_URLS[source_key],
        })
    return incidents



def parse_kml_coordinates(value: str) -> list[list[float]]:
    """Convert KML lon,lat[,alt] coordinate text to GeoJSON positions."""
    coordinates: list[list[float]] = []
    for token in re.split(r"\s+", clean(value)):
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            coordinates.append([float(parts[0]), float(parts[1])])
        except ValueError:
            continue
    return coordinates


def kml_placemark_geometry(placemark: ET.Element, namespace: dict[str, str]) -> dict[str, Any] | None:
    """Extract the most useful GeoJSON geometry from an Essential Energy placemark."""
    geometries: list[dict[str, Any]] = []

    for polygon in placemark.findall(".//kml:Polygon", namespace):
        rings: list[list[list[float]]] = []
        outer = polygon.find("./kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", namespace)
        if outer is not None and outer.text:
            ring = parse_kml_coordinates(outer.text)
            if ring:
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                rings.append(ring)
        for inner in polygon.findall("./kml:innerBoundaryIs/kml:LinearRing/kml:coordinates", namespace):
            if inner.text:
                ring = parse_kml_coordinates(inner.text)
                if ring:
                    if ring[0] != ring[-1]:
                        ring.append(ring[0])
                    rings.append(ring)
        if rings:
            geometries.append({"type": "Polygon", "coordinates": rings})

    for point in placemark.findall(".//kml:Point/kml:coordinates", namespace):
        if point.text:
            coordinates = parse_kml_coordinates(point.text)
            if coordinates:
                geometries.append({"type": "Point", "coordinates": coordinates[0]})

    for line in placemark.findall(".//kml:LineString/kml:coordinates", namespace):
        if line.text:
            coordinates = parse_kml_coordinates(line.text)
            if coordinates:
                geometries.append({"type": "LineString", "coordinates": coordinates})

    if not geometries:
        return None
    if len(geometries) == 1:
        return geometries[0]
    return next((item for item in geometries if item.get("type") == "Polygon"), {
        "type": "GeometryCollection",
        "geometries": geometries,
    })


def decode_essential_html(value: Any) -> str:
    """Decode the repeatedly escaped HTML stored in Essential Energy KML."""
    text = clean(value)
    text = text.replace("&amp;amp;lt;", "&amp;lt;").replace("&amp;amp;gt;", "&amp;gt;").replace("&amp;amp;amp;", "&amp;amp;")
    for _ in range(5):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def essential_plain_text(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"</(?:div|p|h[1-6])\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return "\n".join(clean(line) for line in html.unescape(text).splitlines() if clean(line))


def essential_label_value(decoded_html: str, label: str) -> str:
    pattern = re.compile(
        r"<span[^>]*>\s*" + re.escape(label) + r"\s*</span>\s*(.*?)\s*</div>",
        flags=re.I | re.S,
    )
    match = pattern.search(decoded_html)
    return clean(essential_plain_text(match.group(1))) if match else ""


def parse_essential_date(value: Any) -> datetime | None:
    """Parse Essential Energy's NSW-local human-readable KML timestamps."""
    text = clean(value)
    if not text:
        return None
    normalized = re.sub(r"\s+", " ", text)
    normalized = re.sub(r"\ba\.m\.\b", "AM", normalized, flags=re.I)
    normalized = re.sub(r"\bp\.m\.\b", "PM", normalized, flags=re.I)
    normalized = re.sub(r"\bam\b", "AM", normalized, flags=re.I)
    normalized = re.sub(r"\bpm\b", "PM", normalized, flags=re.I)
    formats = (
        "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M %p",
        "%d/%m/%y %I:%M:%S %p", "%d/%m/%y %I:%M %p",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
        "%d/%m/%y %H:%M:%S", "%d/%m/%y %H:%M",
        "%d %b %Y %I:%M %p", "%d %b %Y %H:%M",
        "%I:%M%p %d %b %Y", "%I:%M %p %d %b %Y", "%H:%M %d %b %Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=ESSENTIAL_TZ).astimezone(AEST)
        except ValueError:
            pass
    return parse_date(normalized)


def essential_fields(name: str, description: str) -> dict[str, Any]:
    decoded = decode_essential_html(description)
    plain = essential_plain_text(decoded)
    heading_match = re.search(r"<h2[^>]*>(.*?)</h2>", decoded, flags=re.I | re.S)
    incident = clean(essential_plain_text(heading_match.group(1))) if heading_match else clean(name)

    labelled = {
        "time_off": essential_label_value(decoded, "Time Off:"),
        "estimated_restore": essential_label_value(decoded, "Est. Time On:"),
        "customers": essential_label_value(decoded, "No. of Customers affected:"),
        "reason": essential_label_value(decoded, "Reason:"),
        "last_updated": essential_label_value(decoded, "Last Updated:"),
    }
    pairs: dict[str, str] = {}
    for line in plain.splitlines():
        match = re.match(r"^([^:]{2,80})\s*:\s*(.+)$", line)
        if match:
            pairs[norm(match.group(1))] = clean(match.group(2))

    def first(*keys: str) -> str:
        for key in keys:
            value = pairs.get(norm(key))
            if value:
                return value
        return ""

    time_off = labelled["time_off"] or first("time off", "start", "started", "start time", "outage start")
    estimated_restore = labelled["estimated_restore"] or first(
        "est time on", "estimated restoration", "estimated restore", "etr", "finish", "end time"
    )
    customers_text = labelled["customers"] or first(
        "no of customers affected", "customers affected", "affected customers", "customers", "customer count"
    )
    reason = labelled["reason"] or first("cause", "reason", "outage cause")
    last_updated = labelled["last_updated"] or first("last updated", "updated")
    suburb = first("suburb", "suburbs", "locality", "town")
    streets = first("street", "streets", "location", "affected area")
    status = first("status") or "Outage reported"

    return {
        "incident": incident,
        "reason": reason,
        "status": status,
        "suburb": suburb,
        "streets": streets,
        "customers": parse_int(customers_text),
        "time_off": parse_essential_date(time_off),
        "estimated_restore": parse_essential_date(estimated_restore),
        "last_updated": parse_essential_date(last_updated),
    }


def infer_essential_outage_type(style_url: Any) -> str | None:
    """Classify Essential Energy placemarks exactly as the supplied ZIP does."""
    style = clean(style_url).lower()
    if "unplanned" in style:
        return "unplanned"
    if "planned" in style:
        return "planned"
    return None


def parse_essential_power(kml_data: bytes, lgas: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only unplanned Essential Energy outages located in Queensland."""
    root = ET.fromstring(kml_data)
    namespace_uri = root.tag.split("}", 1)[0].lstrip("{") if root.tag.startswith("{") else "http://www.opengis.net/kml/2.2"
    namespace = {"kml": namespace_uri}
    incidents: list[dict[str, Any]] = []
    seen: set[str] = set()

    for placemark in root.findall(".//kml:Placemark", namespace):
        name_node = placemark.find("kml:name", namespace)
        description_node = placemark.find("kml:description", namespace)
        style_node = placemark.find("kml:styleUrl", namespace)
        name = clean(name_node.text if name_node is not None else "")
        description = description_node.text if description_node is not None and description_node.text else ""
        style_url = clean(style_node.text if style_node is not None else "")

        # Essential's KML encodes planned/unplanned state in styleUrl. Keep only
        # explicit unplanned records; planned and unknown styles are excluded.
        if infer_essential_outage_type(style_url) != "unplanned":
            continue

        geometry = kml_placemark_geometry(placemark, namespace)
        coords, footprint = power_geometry(geometry)
        lga = locate_lga(coords, lgas)

        # Essential Energy also covers NSW. A successful Queensland LGA match is
        # required before the outage is allowed into the application.
        if not lga:
            continue

        fields = essential_fields(name, description)
        locality = clean(fields["suburb"] or fields["incident"] or name) or lga
        event_id = stable_id(
            "Essential Energy",
            locality,
            round(coords[0], 5) if coords else "",
            round(coords[1], 5) if coords else "",
            fields["time_off"] or "",
        )
        if event_id in seen:
            continue
        seen.add(event_id)

        reason = clean(fields["reason"])
        streets = clean(fields["streets"])
        description_text = ". ".join(part for part in (
            reason,
            f"Affected area: {streets}" if streets else "",
        ) if part)
        incidents.append({
            "id": f"power-essential-{event_id}",
            "sector": "power",
            "subtype": "unplanned",
            "title": f"{title_case(locality)} unplanned power outage",
            "description": description_text,
            "status": clean(fields["status"]) or "Outage reported",
            "lga": lga,
            "locality": title_case(locality),
            "coordinates": coords,
            "geometry": footprint,
            "customers": int(fields["customers"] or 0),
            "planned": False,
            "updated": iso(fields["last_updated"]) or NOW_ISO,
            "estimated_restore": iso(fields["estimated_restore"]) or None,
            "source_name": "Essential Energy",
            "source_url": OFFICIAL_SOURCE_URLS["essential"],
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
            "source_url": OFFICIAL_SOURCE_URLS["schools"],
            "school_sector": sector,
        })
    return incidents


def strip_html(value: str) -> str:
    return clean(html.unescape(re.sub(r"<[^>]+>", " ", value or "")))


def parse_rail(xml_data: bytes) -> list[dict[str, Any]]:
    """Parse Translink's train-status RSS feed.

    The current feed reports each line with a simple description value of
    ``Major``, ``Minor`` or ``Normal``. Major and Minor are active service
    disruptions; Normal is excluded. Older descriptive feed wording remains
    supported as a fallback.
    """
    root = ET.fromstring(xml_data)
    channel = root.find("channel")
    channel_pub_date = ""
    if channel is not None:
        pub_date_node = channel.find("pubDate")
        channel_pub_date = clean(pub_date_node.text if pub_date_node is not None else "")
    feed_updated = iso(channel_pub_date) or NOW_ISO

    incidents: list[dict[str, Any]] = []
    active_keywords = (
        "closed",
        "closure",
        "suspended",
        "not running",
        "no trains",
        "replacement bus",
        "cancelled",
        "canceled",
        "delay",
        "disruption",
        "track work",
    )
    closure_keywords = ("closed", "closure", "suspended", "not running", "no trains")

    for item in root.findall(".//item"):
        def value(tag: str) -> str:
            element = item.find(tag)
            return clean(element.text if element is not None else "")

        line_name = value("title") or "Rail service"
        raw_description = strip_html(value("description"))
        status_key = norm(raw_description)
        combined = norm(f"{line_name} {raw_description}")

        # Current Translink status feed: retain Major and Minor, ignore Normal.
        if status_key == "normal":
            continue
        if status_key in {"major", "minor"}:
            is_active = True
            is_closure = False
            description = f"Translink feed status: {raw_description}."
        else:
            # Backwards-compatible handling for descriptive RSS messages.
            is_active = any(keyword in combined for keyword in active_keywords)
            if not is_active:
                continue
            is_closure = any(keyword in combined for keyword in closure_keywords)
            description = raw_description or "Translink reports a train service disruption."

        link = value("link") or SOURCES["rail"]["url"]
        item_updated = iso(value("pubDate")) or feed_updated
        service_key = norm(line_name)
        incidents.append({
            # The RSS item URL can change whenever Translink republishes the
            # feed. Identify a rail incident by its service/line instead, so an
            # unchanged line is not counted as a newly disrupted line.
            "id": f"rail-{stable_id(service_key)}",
            "service_key": service_key,
            "line_name": line_name,
            "sector": "rail",
            "subtype": "closure" if is_closure else "disruption",
            "title": f"{line_name} line service disruption",
            "description": description,
            "status": "Service disruption",
            # Keep the source-reported operational state separately from the
            # display label. Feed publication timestamps can change without the
            # service condition changing; trend detection compares this field.
            "impact_state": status_key if status_key in {"major", "minor"} else "active",
            "lga": None,
            "locality": "Queensland rail network",
            "coordinates": None,
            "geometry": None,
            "customers": 0,
            "planned": "track work" in combined or "planned" in combined,
            "updated": item_updated,
            "source_name": "Translink",
            "source_url": link,
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


def incident_official_lgas(incident: dict[str, Any], official_names: set[str]) -> set[str]:
    """Return only official Queensland LGAs represented by an incident label."""
    names: set[str] = set()
    for raw_name in re.split(r"\s*/\s*|\s*;\s*", clean(incident.get("lga"))):
        name = LGA_ALIASES.get(raw_name, raw_name)
        if name in official_names:
            names.add(name)
    return names


def calculate_kpi_snapshot(incidents: list[dict[str, Any]], lgas: dict[str, Any], at: str) -> dict[str, Any]:
    """Create the compact statewide values used by the KPI trend visuals."""
    roads = [item for item in incidents if item.get("sector") == "roads"]
    power = [item for item in incidents if item.get("sector") == "power"]
    rail = [item for item in incidents if item.get("sector") == "rail"]
    schools = [item for item in incidents if item.get("sector") == "schools"]
    official_names = {
        clean(feature.get("properties", {}).get("display_name"))
        for feature in lgas.get("features", [])
        if clean(feature.get("properties", {}).get("display_name"))
    }
    affected_lgas: set[str] = set()
    for incident in incidents:
        affected_lgas.update(incident_official_lgas(incident, official_names))
    return {
        "at": at,
        "roads": sum(1 for item in roads if item.get("subtype") == "closure"),
        "power_customers": sum(int(item.get("customers") or 0) for item in power),
        "power_outages": len(power),
        "rail": len(rail),
        "schools": len(schools),
        "lgas": len(affected_lgas),
    }


def incident_index(incidents: list[dict[str, Any]], sector: str) -> dict[str, dict[str, Any]]:
    """Index stable incident identities for change detection."""
    return {
        clean(item.get("id")): item
        for item in incidents
        if item.get("sector") == sector and clean(item.get("id"))
    }


def rail_service_key(incident: dict[str, Any]) -> str:
    """Return a stable service identity for current and legacy rail records.

    Translink's RSS links and publication timestamps may change even when the
    operational state of a line does not. The line/service name is therefore
    the change-detection identity.
    """
    explicit = norm(incident.get("service_key") or incident.get("line_name"))
    if explicit:
        return explicit

    title = norm(incident.get("title"))
    # Legacy titles were formatted as "<line> line service disruption".
    title = re.sub(r"\bline service disruption\b$", "", title).strip()
    title = re.sub(r"\bservice disruption\b$", "", title).strip()
    title = re.sub(r"\bline\b$", "", title).strip()
    if title:
        return title

    return clean(incident.get("id"))


def rail_incident_index(incidents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index rail incidents by stable service name, never by mutable RSS URL."""
    indexed: dict[str, dict[str, Any]] = {}
    for item in incidents:
        if item.get("sector") != "rail":
            continue
        key = rail_service_key(item)
        if not key:
            continue
        existing = indexed.get(key)
        if existing is None or rail_impact_level(item) > rail_impact_level(existing):
            indexed[key] = item
    return indexed


def rail_impact_level(incident: dict[str, Any]) -> int:
    """Return an operational level without treating feed refreshes as changes.

    Major is operationally worse than Minor. Older embedded records may not
    contain ``impact_state``; infer it from their source text where possible,
    otherwise return 0 so deployment of this version does not invent a change.
    """
    state = norm(incident.get("impact_state"))
    if state == "major":
        return 2
    if state == "minor":
        return 1
    combined = norm(f"{incident.get('status')} {incident.get('description')}")
    if re.search(r"\bmajor\b", combined):
        return 2
    if re.search(r"\bminor\b", combined):
        return 1
    return 0


def affected_official_lgas(incidents: list[dict[str, Any]], lgas: dict[str, Any]) -> set[str]:
    official_names = {
        clean(feature.get("properties", {}).get("display_name"))
        for feature in lgas.get("features", [])
        if clean(feature.get("properties", {}).get("display_name"))
    }
    affected: set[str] = set()
    for incident in incidents:
        affected.update(incident_official_lgas(incident, official_names))
    return affected


def calculate_operational_changes(
    previous_incidents: list[dict[str, Any]],
    current_incidents: list[dict[str, Any]],
    lgas: dict[str, Any],
    since: str,
    at: str,
) -> dict[str, Any]:
    """Calculate only new, worsened, improved, or resolved operational impact.

    Routine source publication timestamps and text refreshes do not contribute.
    The signed KPI trend is produced by subtracting ``*_down`` from ``*_up``.
    """
    previous_roads = incident_index(previous_incidents, "roads")
    current_roads = incident_index(current_incidents, "roads")
    previous_closures = {key for key, item in previous_roads.items() if item.get("subtype") == "closure"}
    current_closures = {key for key, item in current_roads.items() if item.get("subtype") == "closure"}

    previous_power = incident_index(previous_incidents, "power")
    current_power = incident_index(current_incidents, "power")
    power_up = 0
    power_down = 0
    power_events_up = 0
    power_events_down = 0
    for key in previous_power.keys() | current_power.keys():
        before = max(0, int(previous_power.get(key, {}).get("customers") or 0))
        after = max(0, int(current_power.get(key, {}).get("customers") or 0))
        if after > before:
            power_up += after - before
            power_events_up += 1
        elif before > after:
            power_down += before - after
            power_events_down += 1

    previous_rail = rail_incident_index(previous_incidents)
    current_rail = rail_incident_index(current_incidents)
    rail_up = 0
    rail_down = 0
    for key in previous_rail.keys() | current_rail.keys():
        before_item = previous_rail.get(key)
        after_item = current_rail.get(key)
        if before_item is None and after_item is not None:
            rail_up += 1
            continue
        if before_item is not None and after_item is None:
            rail_down += 1
            continue
        if before_item is None or after_item is None:
            continue
        before_level = rail_impact_level(before_item)
        after_level = rail_impact_level(after_item)
        # Unknown legacy state is treated as unchanged on deployment. Once both
        # snapshots contain source states, Minor <-> Major changes are detected.
        if before_level and after_level > before_level:
            rail_up += 1
        elif after_level and before_level > after_level:
            rail_down += 1

    previous_schools = set(incident_index(previous_incidents, "schools"))
    current_schools = set(incident_index(current_incidents, "schools"))
    previous_lgas = affected_official_lgas(previous_incidents, lgas)
    current_lgas = affected_official_lgas(current_incidents, lgas)

    return {
        "since": since,
        "at": at,
        "roads_up": len(current_closures - previous_closures),
        "roads_down": len(previous_closures - current_closures),
        "power_customers_up": power_up,
        "power_customers_down": power_down,
        "power_events_up": power_events_up,
        "power_events_down": power_events_down,
        "rail_up": rail_up,
        "rail_down": rail_down,
        "schools_up": len(current_schools - previous_schools),
        "schools_down": len(previous_schools - current_schools),
        "lgas_up": len(current_lgas - previous_lgas),
        "lgas_down": len(previous_lgas - current_lgas),
    }


def update_change_history(
    previous: dict[str, Any],
    previous_incidents: list[dict[str, Any]],
    current_incidents: list[dict[str, Any]],
    lgas: dict[str, Any],
) -> list[dict[str, Any]]:
    """Append operational-change observations and retain seven days."""
    history = [
        dict(item) for item in previous.get("change_history", [])
        if isinstance(item, dict) and item.get("at")
    ]

    # v2 matched rail incidents by RSS item URL. Because Translink may change
    # those URLs on routine republication, v2 could record every active line as
    # newly disrupted. Remove only those legacy rail deltas during migration;
    # all other sector history remains intact.
    previous_history_version = int(previous.get("change_history_version") or 0)
    if previous_history_version < CHANGE_HISTORY_VERSION:
        for item in history:
            item["rail_up"] = 0
            item["rail_down"] = 0

    since = clean(previous.get("generated_at")) or NOW_ISO
    current = calculate_operational_changes(previous_incidents, current_incidents, lgas, since, NOW_ISO)
    if history and clean(history[-1].get("at")) == NOW_ISO:
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


def update_kpi_history(previous: dict[str, Any], incidents: list[dict[str, Any]], lgas: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve, append and prune KPI history across scheduled refreshes."""
    history = [item for item in previous.get("history", []) if isinstance(item, dict) and item.get("at")]

    # Seed history from the last embedded snapshot when upgrading an existing
    # dashboard, providing a comparison after the first run of this version.
    if not history and previous.get("generated_at"):
        history.append(calculate_kpi_snapshot(previous.get("incidents", []), lgas, clean(previous.get("generated_at"))))

    current = calculate_kpi_snapshot(incidents, lgas, NOW_ISO)
    last_at = parse_date(history[-1].get("at")) if history else None
    if last_at and NOW - last_at < HISTORY_MIN_SPACING:
        history[-1] = current
    else:
        history.append(current)

    cutoff = NOW - HISTORY_RETENTION
    retained = []
    for item in history:
        item_at = parse_date(item.get("at"))
        if item_at and item_at >= cutoff:
            retained.append(item)
    return retained[-HISTORY_MAX_POINTS:]


def main() -> int:
    print(f"Refresh script version: {SCRIPT_VERSION}")
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
        ("essential", lambda: parse_essential_power(get_essential_kml(), lgas)),
        ("schools", lambda: parse_schools(get_bytes(SOURCES["schools"]["url"]).decode("utf-8-sig"), lgas)),
        ("rail", lambda: parse_rail(get_bytes(SOURCES["rail"]["url"]))),
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
            fallback = previous_by_source.get(key, [])
            incidents.extend(fallback)
            sources[key] = source_result(key, "fallback" if fallback else "error", len(fallback), str(exc))
            print(f"{SOURCES[key]['name']}: ERROR - {exc}")

    history = update_kpi_history(previous, incidents, lgas)
    change_history = update_change_history(previous, previous_incidents, incidents, lgas)
    data = {
        "generated_at": NOW_ISO,
        "notice": "Snapshot refreshed automatically by GitHub. Scheduled runs occur every 15 minutes; individual source publication times may differ.",
        "incidents": incidents,
        "lgas": lgas,
        "sources": sources,
        "history": history,
        "change_history": change_history,
        "change_history_version": CHANGE_HISTORY_VERSION,
    }
    write_embedded(data)
    print(f"Updated {INDEX.name}: {len(incidents)} total incidents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
