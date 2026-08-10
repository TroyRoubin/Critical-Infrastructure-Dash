#!/usr/bin/env python3
"""Add the MSQ Marine Warnings KPI to the Critical Infrastructure Dashboard.

This is an idempotent repository patcher. It updates both refresh.py and
index.html while deliberately leaving the existing QLDTraffic, Queensland-only,
multi-LGA and dashboard-title patches untouched.

Marine data is fetched from the public Maritime Safety Queensland dashboard.
Because that site is a JavaScript application, the parser is intentionally
fail-closed: a zero is accepted only when the returned document explicitly says
there are no current maritime/marine warnings. If the public page exposes only
its application shell, refresh.py raises an error so the dashboard displays the
source as unavailable (or keeps a previously verified fallback) instead of
showing a false zero.
"""
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
REFRESH = ROOT / "refresh.py"
INDEX = ROOT / "index.html"

REFRESH_MARKER = "# Marine Warnings KPI: Maritime Safety Queensland v1"
INDEX_MARKER = "/* Marine Warnings KPI v1 */"
MSQ_URL = "https://qldmaritime.msq.qld.gov.au/"


MARINE_PARSER_BLOCK = r'''
# Marine Warnings KPI: Maritime Safety Queensland v1
MSQ_NO_WARNING_PHRASES = (
    "no current maritime warnings",
    "no active maritime warnings",
    "there are no current maritime warnings",
    "there are no active maritime warnings",
    "no current marine warnings",
    "no active marine warnings",
    "there are no current marine warnings",
    "there are no active marine warnings",
)

MSQ_GENERIC_WARNING_LABELS = {
    "marine warning",
    "marine warnings",
    "maritime warning",
    "maritime warnings",
    "warnings",
    "current warnings",
    "maritime safety queensland",
    "opt in notifications",
}


class _MSQVisibleTextParser(HTMLParser):
    """Extract visible text lines without executing dashboard JavaScript."""

    _BREAK_TAGS = {
        "address", "article", "aside", "br", "dd", "div", "dl", "dt",
        "figcaption", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "li", "main", "nav", "p", "section", "table", "td",
        "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if not self._skip_depth and tag in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if not self._skip_depth and tag in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def lines(self) -> list[str]:
        return [clean(line) for line in "".join(self.parts).splitlines() if clean(line)]


def _msq_warning_candidate(value: Any) -> str | None:
    text = clean(html.unescape(str(value or "")))
    if len(text) < 12 or len(text) > 500:
        return None

    key = norm(text)
    if not key or key in MSQ_GENERIC_WARNING_LABELS:
        return None
    if key.startswith("opt in notification") or key in {"sign in", "register"}:
        return None

    # Strong phrases that identify an operational maritime warning rather than
    # a navigation label or general preparedness link on the dashboard shell.
    strong_phrases = (
        "port closed",
        "port closure",
        "pilotage area closed",
        "waterway closed",
        "closed to vessel traffic",
        "closed to all vessel traffic",
        "navigation warning",
        "hazard to navigation",
        "marine warning in place",
        "marine warnings in place",
        "maritime warning in place",
        "maritime warnings in place",
        "extreme weather warning",
        "red alert",
        "orange alert",
        "yellow alert",
        "white alert",
    )
    if any(phrase in key for phrase in strong_phrases):
        return text

    # Also accept a descriptive warning sentence when it contains both a
    # maritime subject and an active/impact qualifier.
    maritime_terms = (
        "port", "pilotage", "waterway", "navigation", "harbour", "harbor",
        "vessel", "marine", "maritime", "cyclone", "storm", "swell", "flood",
    )
    active_terms = (
        "active", "current", "closed", "closure", "hazard", "danger",
        "alert", "issued", "effective", "restricted", "restriction",
    )
    if "warning" in key and any(term in key for term in maritime_terms) and any(term in key for term in active_terms):
        return text

    return None


def _msq_unique_candidates(values: list[str]) -> list[str]:
    candidates = []
    for value in values:
        candidate = _msq_warning_candidate(value)
        if candidate:
            candidates.append(candidate)

    # Prefer concise card/title text when a longer captured block contains the
    # same warning. This avoids counting one dashboard card multiple times.
    unique: list[tuple[str, str]] = []
    for candidate in sorted(candidates, key=lambda item: (len(item), item.lower())):
        key = norm(candidate)
        if any(key == existing or (len(key) >= 24 and (key in existing or existing in key)) for existing, _ in unique):
            continue
        unique.append((key, candidate))

    if len(unique) > 40:
        raise RuntimeError(
            "MSQ dashboard warning extraction was ambiguous (more than 40 candidate blocks); "
            "refusing to publish a potentially misleading KPI."
        )
    return [candidate for _, candidate in unique]


def parse_msq_marine_warnings(html_data: bytes) -> list[dict[str, Any]]:
    """Parse active warnings from the public MSQ dashboard, failing closed.

    A verified explicit no-warning message is the only condition that produces
    a trustworthy zero. If the server returns only the JavaScript application
    shell, raise so source health reports the feed as unavailable rather than 0.
    """
    raw = html_data.decode("utf-8-sig", errors="replace")
    if not clean(raw):
        raise RuntimeError("MSQ dashboard returned an empty response")

    parser = _MSQVisibleTextParser()
    parser.feed(raw)
    visible_lines = parser.lines()
    visible_key = norm(" ".join(visible_lines))

    if any(phrase in visible_key for phrase in MSQ_NO_WARNING_PHRASES):
        return []

    # Some Guardian/QIT dashboard deployments place public card payloads in
    # script/JSON strings even when the main page is client-rendered. Inspect
    # string values as a secondary path, without executing any JavaScript.
    script_strings = []
    for match in re.finditer(r"[\"']([^\"'\r\n]{12,500})[\"']", raw):
        value = match.group(1)
        if "warning" in value.lower() or "alert" in value.lower() or "closed" in value.lower():
            script_strings.append(value.replace(r"\u0026", "&").replace(r"\/", "/"))

    warnings = _msq_unique_candidates(visible_lines + script_strings)
    if not warnings:
        shell_key = norm(raw)
        if "maritime safety queensland" in shell_key or "powered by qit plus" in shell_key:
            raise RuntimeError(
                "MSQ dashboard returned its application shell, but the active maritime-warning state "
                "was not present in the server response"
            )
        raise RuntimeError("Unable to verify the active maritime-warning state from the MSQ dashboard")

    incidents = []
    for warning in warnings:
        title = warning if len(warning) <= 170 else warning[:167].rstrip() + "…"
        description = "" if title == warning else warning
        incidents.append({
            "id": f"marine-{stable_id(warning)}",
            "sector": "marine",
            "subtype": "warning",
            "event_category": "maritime warning",
            "title": title,
            "description": description,
            "status": "Active maritime warning",
            "lga": None,
            "locality": "Queensland maritime network",
            "coordinates": None,
            "geometry": None,
            "customers": 0,
            "planned": False,
            "updated": NOW_ISO,
            "source_name": "Maritime Safety Queensland",
            "source_url": SOURCES["marine"]["url"],
        })
    return incidents


'''


INDEX_CSS_BLOCK = r'''    /* Marine Warnings KPI v1 */
    .focus-lga-kpi{
      flex:0 0 auto;display:flex;align-items:center;gap:9px;min-width:112px;
      padding:7px 9px;border:1px solid rgba(165,140,242,.42);border-radius:10px;
      background:rgba(165,140,242,.12);box-shadow:inset 3px 0 0 #a58cf2
    }
    .focus-lga-kpi-label{color:#cbbdfb;font-size:9px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;line-height:1.15}
    .focus-lga-kpi-value{color:#cbbdfb;font-size:22px;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}
    html[data-theme="light"] .focus-lga-kpi{background:rgba(112,83,205,.08);border-color:rgba(112,83,205,.3)}
    html[data-theme="light"] .focus-lga-kpi-label,html[data-theme="light"] .focus-lga-kpi-value{color:#6749b8}
'''

MARINE_CARD = '''        <article class="metric" style="--tone:var(--marine)" data-kpi-sector="marine" role="button" tabindex="0" aria-label="Toggle marine warnings">
          <div class="metric-label">Marine warnings</div>
          <div class="metric-value" id="marineValue">—</div>
          <div class="metric-detail" id="marineDetail">MSQ status pending</div>
        </article>
'''

LGA_BADGE = '''          <div class="focus-lga-kpi" title="Queensland LGAs affected within the current filters" aria-label="LGAs affected within current filters">
            <span class="focus-lga-kpi-label">LGAs<br>affected</span>
            <strong class="focus-lga-kpi-value" id="lgaValue">0</strong>
          </div>
'''


TOP_LGA_CARD_PATTERN = re.compile(
    r'''        <article class="metric" style="--tone:#a58cf2" data-kpi-sector="all"[^>]*>\s*'''
    r'''<div class="metric-label">LGAs affected</div>\s*'''
    r'''<div class="metric-value" id="lgaValue">0</div>\s*'''
    r'''<div class="metric-detail">within current filters</div>\s*'''
    r'''</article>\s*''',
    re.S,
)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Marine KPI patch expected exactly one {label}; found {count}.")
    return text.replace(old, new, 1)


def patch_refresh(text: str) -> tuple[str, bool]:
    if REFRESH_MARKER in text:
        validate_refresh(text)
        return text, False

    if 'from html.parser import HTMLParser' not in text:
        text = replace_once(
            text,
            "import html\n",
            "import html\nfrom html.parser import HTMLParser\n",
            "html import anchor",
        )

    if '"marine": {' not in text:
        geography_anchor = '    "geography": {\n'
        marine_source = (
            '    "marine": {\n'
            '        "name": "Maritime Safety Queensland",\n'
            f'        "url": "{MSQ_URL}",\n'
            '    },\n'
        )
        text = replace_once(
            text,
            geography_anchor,
            marine_source + geography_anchor,
            "SOURCES geography anchor",
        )

    text = replace_once(
        text,
        "\ndef read_embedded() -> dict[str, Any]:\n",
        "\n" + MARINE_PARSER_BLOCK + "def read_embedded() -> dict[str, Any]:\n",
        "read_embedded anchor",
    )

    rail_job = '        ("rail", lambda: parse_rail(get_bytes(SOURCES["rail"]["url"]))),\n'
    marine_job = '        ("marine", lambda: parse_msq_marine_warnings(get_bytes(SOURCES["marine"]["url"]))),\n'
    text = replace_once(text, rail_job, rail_job + marine_job, "rail job anchor")

    validate_refresh(text)
    return text, True


def validate_refresh(text: str) -> None:
    required = (
        REFRESH_MARKER,
        '"marine": {',
        '"name": "Maritime Safety Queensland"',
        f'"url": "{MSQ_URL}"',
        'def parse_msq_marine_warnings(html_data: bytes)',
        '"sector": "marine"',
        '("marine", lambda: parse_msq_marine_warnings(get_bytes(SOURCES["marine"]["url"]))),',
        'raise RuntimeError("Unable to verify the active maritime-warning state from the MSQ dashboard")',
    )
    for token in required:
        if token not in text:
            raise RuntimeError(f"Marine refresh patch validation failed: missing {token!r}.")
    compile(text, str(REFRESH), "exec")


def patch_index(text: str) -> tuple[str, bool]:
    if INDEX_MARKER in text:
        validate_index(text)
        return text, False

    # Add a distinct marine sector colour while keeping the five-card KPI grid.
    text = replace_once(
        text,
        "      --schools-soft:rgba(72,199,142,.13);\n",
        "      --schools-soft:rgba(72,199,142,.13);\n"
        "      --marine:#35c6bb;\n"
        "      --marine-soft:rgba(53,198,187,.14);\n",
        "schools colour variables",
    )

    css_anchor = "    .metric-detail{margin-top:4px;color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\n"
    text = replace_once(text, css_anchor, css_anchor + "\n" + INDEX_CSS_BLOCK, "metric-detail CSS anchor")

    text, count = TOP_LGA_CARD_PATTERN.subn(MARINE_CARD, text, count=1)
    if count != 1:
        raise RuntimeError(f"Marine KPI patch expected one top-row LGAs affected card; found {count}.")

    focus_anchor = '''            <div class="panel-subtitle">Filter active disruptions by location, sector, or search term</div>
          </div>
        </div>
        <div class="filter-body">
'''
    focus_replacement = '''            <div class="panel-subtitle">Filter active disruptions by location, sector, or search term</div>
          </div>
''' + LGA_BADGE + '''        </div>
        <div class="filter-body">
'''
    text = replace_once(text, focus_anchor, focus_replacement, "Operational focus header")

    sectors_anchor = "      schools: {label:'Schools', symbol:'S', colour:'#48c78e', soft:'rgba(72,199,142,.14)'}\n"
    sectors_replacement = (
        "      schools: {label:'Schools', symbol:'S', colour:'#48c78e', soft:'rgba(72,199,142,.14)'},\n"
        "      marine: {label:'Marine', symbol:'M', colour:'#35c6bb', soft:'rgba(53,198,187,.14)'}\n"
    )
    text = replace_once(text, sectors_anchor, sectors_replacement, "SECTORS schools entry")

    source_anchor = "      schools:'https://closures.qld.edu.au/'\n"
    source_replacement = (
        "      schools:'https://closures.qld.edu.au/',\n"
        f"      marine:'{MSQ_URL}'\n"
    )
    text = replace_once(text, source_anchor, source_replacement, "official source schools entry")

    summary_anchor = "      const nonStateSchools = schools.filter(i => String(i.school_sector || '').toLowerCase() !== 'state');\n"
    summary_insert = summary_anchor + (
        "      const marine = context.filter(i => i.sector === 'marine');\n"
        "      const marineSource = DASHBOARD_DATA.sources?.marine || {};\n"
    )
    text = replace_once(text, summary_anchor, summary_insert, "renderSummary school calculation")

    school_dom_anchor = "      document.getElementById('schoolDetail').textContent = `${fmt(stateSchools.length)} state · ${fmt(nonStateSchools.length)} non-state`;\n"
    marine_dom = school_dom_anchor + (
        "      const marineValue = document.getElementById('marineValue');\n"
        "      const marineDetail = document.getElementById('marineDetail');\n"
        "      const marineVerified = marineSource.status === 'current' || marineSource.status === 'fallback';\n"
        "      marineValue.textContent = marineVerified ? fmt(marine.length) : '—';\n"
        "      marineDetail.textContent = marineSource.status === 'fallback'\n"
        "        ? 'cached MSQ snapshot'\n"
        "        : marineSource.status === 'current'\n"
        "          ? `${fmt(marine.length)} active warning${marine.length === 1 ? '' : 's'}`\n"
        "          : 'MSQ unavailable';\n"
    )
    text = replace_once(text, school_dom_anchor, marine_dom, "renderSummary school DOM update")

    validate_index(text)
    return text, True


def validate_index(text: str) -> None:
    required = (
        INDEX_MARKER,
        '--marine:#35c6bb;',
        'data-kpi-sector="marine"',
        'id="marineValue"',
        'id="marineDetail"',
        'class="focus-lga-kpi"',
        'id="lgaValue"',
        "marine: {label:'Marine'",
        f"marine:'{MSQ_URL}'",
        "const marineSource = DASHBOARD_DATA.sources?.marine || {};",
        "'MSQ unavailable'",
    )
    for token in required:
        if token not in text:
            raise RuntimeError(f"Marine index patch validation failed: missing {token!r}.")

    if text.count('id="marineValue"') != 1:
        raise RuntimeError("Marine index patch validation failed: marineValue must occur exactly once.")
    if text.count('id="lgaValue"') != 1:
        raise RuntimeError("Marine index patch validation failed: lgaValue must occur exactly once.")
    if re.search(r'<article[^>]+data-kpi-sector="all"', text):
        raise RuntimeError("Marine index patch validation failed: old all-sectors LGA KPI card remains.")


def main() -> int:
    if not REFRESH.exists() or not INDEX.exists():
        missing = [path.name for path in (REFRESH, INDEX) if not path.exists()]
        raise SystemExit(f"Missing required dashboard file(s): {', '.join(missing)}")

    refresh_original = REFRESH.read_text(encoding="utf-8")
    index_original = INDEX.read_text(encoding="utf-8")

    try:
        refresh_patched, refresh_changed = patch_refresh(refresh_original)
        index_patched, index_changed = patch_index(index_original)
    except Exception as exc:
        raise SystemExit(f"Marine KPI patch aborted without writing files: {exc}") from exc

    # Write only after both files validate, so a failed index patch cannot leave
    # refresh.py half-updated (and vice versa).
    if refresh_changed:
        REFRESH.write_text(refresh_patched, encoding="utf-8")
    if index_changed:
        INDEX.write_text(index_patched, encoding="utf-8")

    if refresh_changed or index_changed:
        print("Applied Marine Warnings KPI: MSQ source, Marine sector, and Operational Focus LGA badge.")
    else:
        print("Marine Warnings KPI is already applied; no changes required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
