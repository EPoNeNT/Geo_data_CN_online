"""Pure HTML parsers for public Certitude checker source pages."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit


_ALLOWED_HOSTS = {"certitudes.org", "www.certitudes.org"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_DESCRIPTION_NAMES = {"shortdescription", "longdescription"}


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent", "closed")

    def __init__(self, tag: str, attrs: dict[str, str | None], parent: _Node | None = None):
        self.tag = tag.lower()
        self.attrs = attrs
        self.children: list[_Node | str] = []
        self.parent = parent
        self.closed = False

    def text(self) -> str:
        return "".join(child.text() if isinstance(child, _Node) else child for child in self.children)

    def descendants(self):
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.descendants()


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        node = _Node(tag, attrs_dict, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag.lower() in _VOID_TAGS:
            node.closed = True
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self.stack[-1])
        node.closed = True
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack[index].closed = True
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _parse_tree(html: str) -> _Node:
    parser = _TreeBuilder()
    parser.feed(html)
    parser.close()
    return parser.root


def _class_nodes(node: _Node, class_name: str):
    return [item for item in node.descendants()
            if class_name in (item.attrs.get("class") or "").split()]


def _nonnegative_int(value: str) -> int | None:
    value = value.strip()
    return int(value) if re.fullmatch(r"\d+", value) else None


def _dashboard_age(node: _Node) -> int | None:
    # Attribute t is already an elapsed whole-second age, never a timestamp.
    return _nonnegative_int(node.attrs.get("t", ""))


def parse_dashboard_html(html: str, source_url: str, expected_country_id: str | None = None) -> dict:
    """Parse a complete Certitude dashboard page without inferring missing data."""
    if not isinstance(html, str) or not isinstance(source_url, str) or not _host_allowed(source_url):
        raise ValueError("dashboard HTML and allowed source URL are required")
    root = _parse_tree(html)
    html_nodes = [node for node in root.descendants() if node.tag == "html"]
    if not html_nodes or any(not node.closed for node in html_nodes):
        raise ValueError("dashboard document is incomplete")

    forms = [node for node in root.descendants() if node.tag == "form"]
    country_selects = [select for form in forms for select in form.descendants()
                       if select.tag == "select" and select.attrs.get("name") == "filter_country"]
    tables = [node for node in root.descendants() if node.tag == "table"]
    if len(country_selects) != 1 or not tables:
        raise ValueError("dashboard controls or table are unavailable")
    form = next(form for form in forms if country_selects[0] in list(form.descendants()))
    if not form.closed:
        raise ValueError("dashboard filter form is truncated")
    expected_headers = {"solution", "owner", "puzzle", "location", "date published", "woohoo!", "d'oh!", "latest activity"}
    table = next((candidate for candidate in tables
                  if expected_headers.issubset({" ".join(node.text().split()).casefold()
                                               for node in candidate.descendants() if node.tag == "th"})), None)
    if table is None:
        raise ValueError("dashboard activity table is unavailable")
    if not table.closed:
        raise ValueError("dashboard table is truncated")

    filter_names = {"filter_owner", "filter_country", "filter_region", "filter_quadrangle", "filter_user"}
    filters = {}
    for node in form.descendants():
        name = node.attrs.get("name")
        if name not in filter_names:
            continue
        if node.tag == "select":
            options = [item for item in node.descendants() if item.tag == "option"]
            selected = next((item for item in options if "selected" in item.attrs), options[0] if options else None)
            filters[name] = ((selected.attrs.get("value") if selected.attrs.get("value") is not None else selected.text()).strip()
                             if selected else "")
        elif node.tag == "input":
            filters[name] = node.attrs.get("value") or ""
    if not filter_names.issubset(filters):
        raise ValueError("dashboard filter controls are incomplete")

    countries = {}
    country_select = country_selects[0]
    for option in (item for item in country_select.descendants() if item.tag == "option"):
        label = " ".join(option.text().split()).casefold()
        key = next((key for phrase, key in (("china", "china"), ("taiwan", "taiwan"),
                      ("hong kong", "hong-kong"), ("macao", "macao"), ("macau", "macao"))
                    if label.endswith(phrase)), None)
        if key:
            value = option.attrs.get("value")
            if value is None:
                raise ValueError("dashboard country option has no value")
            countries[key] = value
    if set(countries) != {"china", "taiwan", "hong-kong", "macao"}:
        raise ValueError("dashboard country options are incomplete")
    selected_country = filters["filter_country"] or next(
        (item.attrs.get("value", "") for item in country_select.descendants()
         if item.tag == "option"), "")
    if expected_country_id is not None and selected_country != str(expected_country_id):
        raise ValueError("dashboard selected country conflicts with expected country")
    if expected_country_id is not None and (
            filters["filter_region"] != "All" or filters["filter_quadrangle"] != "All"
            or filters["filter_owner"] != "" or filters["filter_user"] != ""):
        raise ValueError("dashboard contains unexpected restrictive subfilters")

    headers = [" ".join(node.text().split()).casefold() for node in table.descendants() if node.tag == "th"]
    if not expected_headers.issubset(set(headers)):
        raise ValueError("dashboard table headers are incomplete")
    rows = []
    for tr in (item for item in table.descendants() if item.tag == "tr"):
        cells = [item for item in tr.descendants() if item.tag == "td"]
        if not cells:
            continue
        if not tr.closed or any(not cell.closed for cell in cells):
            raise ValueError("dashboard data row is truncated")
        def cell(class_name):
            return next((item for item in cells if class_name in (item.attrs.get("class") or "").split()), None)
        wp_cell, cache_cell, activity_cell = cell("c-wp"), cell("c-nam"), cell("c-act")
        success_cell, failure_cell = cell("c-woo"), cell("c-doh")
        if not all((wp_cell, cache_cell, activity_cell, success_cell, failure_cell)):
            raise ValueError("dashboard data row is malformed")
        checker_anchor = next((item for item in wp_cell.descendants() if item.tag == "a" and item.attrs.get("href")), None)
        if checker_anchor is None:
            raise ValueError("dashboard checker link is missing")
        checker_url = urljoin(source_url, checker_anchor.attrs["href"].strip())
        if not _host_allowed(checker_url):
            raise ValueError("dashboard checker link host is not allowed")
        checker_id = _waypoint(checker_url)
        if checker_id is None:
            raise ValueError("dashboard checker ID is unavailable")
        parent_link = next((urljoin(source_url, item.attrs["href"].strip()) for item in cache_cell.descendants()
                            if item.tag == "a" and item.attrs.get("href") and
                            _id_from_gc_url(urljoin(source_url, item.attrs["href"].strip()))), None)
        cache_code = _id_from_gc_url(parent_link) if parent_link else None
        rows.append({"checkerId": checker_id, "sourceUrl": checker_url, "cacheCode": cache_code,
                     "ageSeconds": _dashboard_age(activity_cell),
                     "successCount": _nonnegative_int(success_cell.text()),
                     "failureCount": _nonnegative_int(failure_cell.text())})

    action = urljoin(source_url, form.attrs.get("action", ""))
    action_query = parse_qs(urlsplit(action).query)
    sort_url = None
    for anchor in (node for node in root.descendants() if node.tag == "a" and node.attrs.get("href")):
        candidate = urljoin(source_url, anchor.attrs["href"].strip())
        query = parse_qs(urlsplit(candidate).query)
        if query.get("sort") == ["act"] and query.get("dir") == ["desc"]:
            sort_url = candidate
            break
    if sort_url is None and action_query.get("sort") == ["act"] and action_query.get("dir") == ["desc"]:
        sort_url = action

    next_url = None
    pagination = []
    for anchor in (node for node in root.descendants() if node.tag == "a" and node.attrs.get("href")):
        candidate = urljoin(source_url, anchor.attrs["href"].strip())
        query = parse_qs(urlsplit(candidate).query, keep_blank_values=True)
        parsed = urlsplit(candidate)
        if (parsed.hostname in {"certitudes.org", "www.certitudes.org"} and parsed.path == "/"
                and query.get("sort") == action_query.get("sort")
                and query.get("dir") == action_query.get("dir")
                and query.get("idx", [""])[0]):
            pagination.append({"url": candidate, "text": " ".join(anchor.text().split()),
                               "rel": anchor.attrs.get("rel", ""), "class": anchor.attrs.get("class", "")})
    for container in (node for node in root.descendants() if node.tag == "div" and
                       {"next", "logo"}.issubset(set((node.attrs.get("class") or "").split()))):
        links = [node for node in container.descendants() if node.tag == "a" and node.attrs.get("href")]
        if not links:
            continue
        candidates = []
        for link in links:
            resolved = urljoin(source_url, link.attrs["href"].strip())
            query = parse_qs(urlsplit(resolved).query, keep_blank_values=True)
            if (urlsplit(resolved).hostname in {"certitudes.org", "www.certitudes.org"} and
                    urlsplit(resolved).path == "/" and query.get("sort") == action_query.get("sort") and
                    query.get("dir") == action_query.get("dir") and query.get("idx", [""])[0]):
                candidates.append(resolved)
        if len(candidates) != 1:
            raise ValueError("dashboard pagination link is malformed or ambiguous")
        if next_url is not None and next_url != candidates[0]:
            raise ValueError("dashboard pagination links conflict")
        next_url = candidates[0]
    return {"filterAction": action, "activitySortUrl": sort_url,
            "sort": action_query.get("sort", [None])[0], "direction": action_query.get("dir", [None])[0], "countries": countries,
            "selectedCountry": selected_country, "filters": filters, "rows": rows,
            "pagination": pagination, "nextUrl": next_url}


def _host_allowed(url: str) -> bool:
    try:
        return (urlsplit(url).scheme.lower() in {"http", "https"}
                and (urlsplit(url).hostname or "").lower() in _ALLOWED_HOSTS)
    except ValueError:
        return False


def _waypoint(url: str) -> str | None:
    try:
        values = parse_qs(urlsplit(url).query, keep_blank_values=True).get("wp")
    except ValueError:
        return None
    return values[0] if values and values[0] else None


def _container_name(node: _Node) -> str | None:
    candidates = [node.attrs.get("id", ""), *str(node.attrs.get("class", "")).split()]
    for candidate in candidates:
        normalized = candidate.lower()
        if normalized in _DESCRIPTION_NAMES or any(normalized.endswith("_" + name) for name in _DESCRIPTION_NAMES):
            return candidate
    return None


def _container_kind(name: str) -> str:
    normalized = name.lower()
    return next((kind for kind in _DESCRIPTION_NAMES
                 if normalized == kind or normalized.endswith("_" + kind)), normalized)


def _is_login_wall(root: _Node, containers: list[_Node]) -> bool:
    body = " ".join(node.text() for node in containers).lower()
    if re.search(r"premium\s+(?:members?\s+)?only|must\s+(?:be\s+)?(?:logged|signed)\s+in|sign\s+in\s+to\s+(?:view|see)", body):
        return True
    for node in root.descendants():
        if node.tag == "input" and (node.attrs.get("type") or "").lower() == "password":
            return True
        if node.tag == "form" and re.search(r"login|signin|sign-in", node.attrs.get("action", ""), re.I):
            return True
    return False


def extract_description_links(html: str, page_url: str, expected_gc: str | None = None) -> list[dict]:
    """Extract Certitude links from complete legacy Short/LongDescription blocks."""
    if not isinstance(html, str) or not isinstance(page_url, str):
        raise ValueError("HTML and page URL are required")
    root = _parse_tree(html)
    containers = [node for node in root.descendants() if _container_name(node)]
    found_names = {_container_kind(_container_name(node)) for node in containers}
    if not _DESCRIPTION_NAMES.issubset(found_names):
        raise ValueError("recognized complete cache description containers are unavailable")
    if any(not node.closed for node in containers):
        raise ValueError("cache description container is truncated")
    if _is_login_wall(root, containers):
        raise ValueError("cache description is behind a login or premium wall")

    if expected_gc:
        observed_codes = []
        page_waypoint = _waypoint(page_url)
        if page_waypoint and page_waypoint.upper().startswith("GC"):
            observed_codes.append(page_waypoint.upper())
        for node in root.descendants():
            if node.tag != "h1":
                continue
            observed_codes.extend(code for anchor in node.descendants()
                                  if (code := _id_from_gc_url(urljoin(page_url, anchor.attrs.get("href", "")))))
        if any(code != expected_gc.upper() for code in observed_codes):
            raise ValueError("description cache identity conflicts with expected cache")

    results = []
    container_occurrences = {}
    for container in containers:
        evidence = _container_name(container)
        container_occurrences[evidence] = container_occurrences.get(evidence, 0) + 1
        container_ordinal = container_occurrences[evidence]
        anchors = [node for node in container.descendants() if node.tag == "a"]
        for anchor_ordinal, anchor in enumerate(anchors, 1):
            href = anchor.attrs.get("href")
            if not href:
                continue
            resolved = urljoin(page_url, href.strip())
            if not _host_allowed(resolved):
                continue
            waypoint = _waypoint(resolved)
            results.append({
                "sourceUrl": resolved,
                "sourceWaypoint": waypoint,
                "checkerId": None,
                "mappingStatus": "pending",
                "mappingEvidence": "description_link",
                "evidence": evidence,
                "descriptionEvidence": evidence,
                "sourceOccurrence": f"{evidence}#{container_ordinal}.anchor#{anchor_ordinal}",
            })
    return results


def _id_from_gc_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host == "coord.info":
        code = parsed.path.strip("/").split("/")[0].split("_", 1)[0]
    elif host in {"geocaching.com", "www.geocaching.com"}:
        match = re.search(r"/geocache/([^/]+)", parsed.path, re.I)
        code = match.group(1).split("_", 1)[0] if match else ""
    else:
        return None
    return code.upper() if re.fullmatch(r"GC[A-Z0-9]+", code, re.I) else None


def _epoch(value: str | int | float) -> datetime | None:
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return None
        return datetime.fromtimestamp(number, timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value.astimezone(timezone.utc)
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _observed_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("observed_at must be an ISO datetime") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("observed_at must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _js_array_end(script: str, opening: int) -> int | None:
    depth = 0
    quote = None
    escaped = False
    for index in range(opening, len(script)):
        char = script[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
    return None


def _parse_solver_array(script: str, has_solver_tbody: bool) -> list[tuple[str, float]] | None:
    if not has_solver_tbody:
        return None
    match = re.search(r"\bvar\s+solvers\s*=\s*\[", script)
    if not match:
        return None
    opening = script.find("[", match.start())
    closing = _js_array_end(script, opening)
    if closing is None or not re.match(r"\s*;\s*showSolvers\(\s*['\"](?:[^'\"]*)['\"]\s*,\s*['\"][^'\"]*['\"]\s*,\s*solvers\s*\)", script[closing + 1:]):
        return None
    body = script[opening + 1:closing]
    decoder = json.JSONDecoder()
    rows = []
    position = 0
    while position < len(body):
        while position < len(body) and (body[position].isspace() or body[position] == ","):
            position += 1
        if position >= len(body):
            break
        try:
            row, end = decoder.raw_decode(body, position)
        except json.JSONDecodeError:
            return None
        if (not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str)
                or isinstance(row[1], bool) or not isinstance(row[1], (int, float))):
            return None
        timestamp = float(row[1])
        if not math.isfinite(timestamp) or timestamp < 0 or _epoch(timestamp) is None:
            return None
        rows.append((row[0], timestamp))
        position = end
    return rows


def parse_checker_html(html: str, source_url: str, observed_at: datetime) -> dict:
    """Parse public metadata and complete public solver rows from a checker page."""
    if not isinstance(html, str) or not isinstance(source_url, str):
        raise ValueError("HTML and source URL are required")
    if not _host_allowed(source_url):
        raise ValueError("checker source URL host is not allowed")
    root = _parse_tree(html)
    query_waypoint = _waypoint(source_url)
    hidden_waypoints = [node.attrs.get("value", "") for node in root.descendants()
                        if node.tag == "input" and node.attrs.get("name") == "waypoint" and node.attrs.get("value")]
    heading_ids = []
    for node in root.descendants():
        if node.tag != "h1":
            continue
        for anchor in node.descendants():
            href = anchor.attrs.get("href", "")
            if re.search(r"(?:\?|&)wp=", href, re.I):
                waypoint = _waypoint(urljoin(source_url, href))
                if waypoint:
                    heading_ids.append(waypoint)
    checker_candidates = hidden_waypoints or ([query_waypoint] if query_waypoint else heading_ids)
    if not checker_candidates:
        raise ValueError("checker waypoint is not observable")
    checker_id = checker_candidates[0]
    if any(candidate != checker_id for candidate in checker_candidates):
        raise ValueError("conflicting checker waypoint values")
    if query_waypoint and query_waypoint != checker_id:
        raise ValueError("checker waypoint conflicts with source URL")
    if any(candidate != checker_id for candidate in heading_ids):
        raise ValueError("heading checker ID conflicts with waypoint")

    parent_ids = []
    for node in root.descendants():
        if node.tag != "h1":
            continue
        for anchor in node.descendants():
            parent_id = _id_from_gc_url(urljoin(source_url, anchor.attrs.get("href", "")))
            if parent_id:
                parent_ids.append(parent_id)
    parent_ids = list(dict.fromkeys(parent_ids))
    if len(parent_ids) > 1:
        raise ValueError("conflicting explicit parent cache links")
    cache_code = parent_ids[0] if parent_ids else None
    verified = cache_code is not None
    if verified and checker_id == cache_code:
        role = "final"
    else:
        role = "unknown"

    script_text = "\n".join(node.text() for node in root.descendants() if node.tag == "script")
    has_solver_tbody = any(node.tag == "tbody" and node.attrs.get("id") == "solvers"
                           for node in root.descendants())
    parsed_solvers = _parse_solver_array(script_text, has_solver_tbody)
    verified_solver_function = any(
        node.tag == "script" and (node.attrs.get("src", "").split("?")[0]
                                   == "https://static.certitudes.org/html/certitude.js")
        for node in root.descendants()
    )
    solver_coverage = "unknown"
    if parsed_solvers is not None and verified_solver_function:
        solver_coverage = "complete"

    counters = {}
    for node in root.descendants():
        if node.tag != "b":
            continue
        classes = set((node.attrs.get("class") or "").split())
        kind = "success" if "success" in classes else "failure" if "error" in classes else None
        if kind:
            text = node.text().strip()
            counters[kind] = int(text) if text.isdigit() else None
    counts_observed = ("success" in counters and "failure" in counters
                       and counters["success"] is not None and counters["failure"] is not None)
    success_count = counters.get("success") if counts_observed else None
    failure_count = counters.get("failure") if counts_observed else None

    timezone_match = re.search(r"\bsetTimezone\(\s*(['\"])([^'\"]+)\1\s*\)", script_text)
    timeline = re.search(
        r"\bshowTimeline\(\s*(['\"])[^'\"]*\1\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)",
        script_text,
    )
    published_at = _epoch(timeline.group(2)) if timeline else None
    latest_solver_at = _epoch(timeline.group(3)) if timeline else None
    timeline_observed = published_at is not None and latest_solver_at is not None

    field_coverage = "complete" if counts_observed and solver_coverage == "complete" and timeline_observed else "partial"
    observed_at = _observed_utc(observed_at)
    observed_iso = _iso_utc(observed_at)
    published_iso = _iso_utc(published_at)
    latest_iso = _iso_utc(latest_solver_at)
    solver_rows = []
    raw_solver_count = 0
    if solver_coverage == "complete":
        raw_solver_count = len(parsed_solvers)
        solver_rows = []
        seen = set()
        for rank, (username, timestamp) in enumerate(parsed_solvers, 1):
            normalized = username.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            solver_rows.append({
                "username": username,
                "normalizedUsername": normalized,
                "sourceUserId": None,
                "sourceRank": rank,
                "certifiedAt": _iso_utc(_epoch(timestamp)),
            })
    public_solver_count = len(solver_rows) if solver_coverage == "complete" else None
    success_rate = (success_count / (success_count + failure_count)
                    if counts_observed and success_count + failure_count > 0 else None)
    last_success = observed_iso if counts_observed or solver_coverage == "complete" else None
    return {
        "checkerId": checker_id,
        "sourceUrl": source_url,
        "cacheCode": cache_code,
        "mappingStatus": "verified" if verified else "pending",
        "mappingEvidence": "explicit_h1_cache_link" if verified else None,
        "role": role,
        "stageLabel": None,
        "answerType": "unknown",
        "matchingMode": "unknown",
        "sourceTimezone": timezone_match.group(2) if timezone_match else None,
        "successCount": success_count,
        "failureCount": failure_count,
        "counterObservedAt": observed_iso if counts_observed else None,
        "successRate": success_rate,
        "publishedAt": published_iso,
        "latestActivityAt": latest_iso,
        "solverListObservedAt": observed_iso if solver_coverage == "complete" else None,
        "solverListCoverage": solver_coverage,
        "solverRowsRetrieved": raw_solver_count,
        "solverReportedTotal": None,
        "publicSolverCount": public_solver_count,
        "publicSolvers": solver_rows,
        "observedAt": observed_iso,
        "crawl": {
            "lastAttemptAt": observed_iso,
            "lastSuccessAt": last_success,
            "status": "ok",
            "errorCode": None,
            "fieldCoverage": field_coverage,
            "stale": None,
        },
    }
