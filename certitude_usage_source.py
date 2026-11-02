"""Conservative inspection of complete Geocaching description HTML."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from certitude_source import (
    _DESCRIPTION_NAMES,
    _container_kind,
    _container_name,
    _id_from_gc_url,
    _parse_tree,
    _waypoint,
    extract_description_links,
)


_GEOCACHING_HOSTS = {"geocaching.com", "www.geocaching.com"}
_SHORTENER_HOSTS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "cutt.ly", "url.cn", "shorturl.at",
}
_CERTITUDE_MARKER = re.compile(r"certitudes?\s*\.\s*org|certitude", re.I)


def _result(status: str, links: list[dict] | None = None, reason: str = "") -> dict:
    return {"status": status, "links": links or [], "reason": reason}


def inspect_description(html: str, page_url: str, expected_gc: str) -> dict:
    """Classify Certitude links in verified, complete legacy description blocks.

    A negative result is emitted only when both description containers are
    complete, identity can be verified, and the nonempty description has no
    direct links or ambiguous indicators.
    """
    if not isinstance(html, str) or not isinstance(page_url, str) or not isinstance(expected_gc, str):
        return _result("unknown", reason="invalid_input")
    try:
        parsed_url = urlsplit(page_url)
    except ValueError:
        return _result("unknown", reason="invalid_page_url")
    try:
        safe_authority = parsed_url.username is None and parsed_url.password is None and parsed_url.port in (None, 443)
    except ValueError:
        safe_authority = False
    if (parsed_url.scheme.lower() != "https" or (parsed_url.hostname or "").lower() not in _GEOCACHING_HOSTS
            or not safe_authority):
        return _result("unknown", reason="untrusted_page_url")
    expected = expected_gc.strip().upper()
    if not re.fullmatch(r"GC[A-Z0-9]+", expected):
        return _result("unknown", reason="invalid_expected_identity")

    try:
        root = _parse_tree(html)
    except Exception:
        return _result("unknown", reason="html_parse_failed")
    html_nodes = [node for node in root.descendants() if node.tag == "html"]
    if not html_nodes or any(not node.closed for node in html_nodes):
        return _result("unknown", reason="incomplete_document")
    containers = [node for node in root.descendants() if _container_name(node)]
    found_names = {_container_kind(_container_name(node)) for node in containers}
    if not _DESCRIPTION_NAMES.issubset(found_names):
        return _result("unknown", reason="description_containers_missing")
    if any(not node.closed for node in containers):
        return _result("unknown", reason="description_truncated")

    observed = []
    url_code = _id_from_gc_url(page_url)
    if url_code:
        observed.append(url_code)
    page_waypoint = _waypoint(page_url)
    if page_waypoint and page_waypoint.upper().startswith("GC"):
        observed.append(page_waypoint.upper())
    for heading in (node for node in root.descendants() if node.tag == "h1"):
        observed.extend(code for anchor in heading.descendants()
                        if (code := _id_from_gc_url(urljoin(page_url, anchor.attrs.get("href", "")))))
    if not observed or any(code != expected for code in observed):
        return _result("unknown", reason="identity_unverified")

    try:
        links = extract_description_links(html, page_url, expected)
    except ValueError as exc:
        message = str(exc).lower()
        reason = "login_or_premium" if "login" in message or "premium" in message else "description_unavailable"
        if "identity" in message or "conflicts" in message:
            reason = "identity_unverified"
        return _result("unknown", reason=reason)
    if links:
        return _result("linked", links, "direct_link_found")

    text = " ".join(node.text() for node in containers).strip()
    images = any(node.tag == "img" for container in containers for node in container.descendants())
    if not text and not images:
        return _result("unknown", reason="empty_description")
    if images and not text:
        return _result("unknown", reason="image_only_description")
    if images:
        return _result("unknown", reason="unverified_description_image")
    if _CERTITUDE_MARKER.search(text):
        return _result("unknown", reason="ambiguous_certitude_text")
    for container in containers:
        nodes = [container, *container.descendants()]
        if any(node.tag in {"iframe", "object", "embed", "script"}
               or any(key.lower().startswith("on") for key in node.attrs) for node in nodes):
            return _result("unknown", reason="unverified_interactive_content")
        if any(_CERTITUDE_MARKER.search(str(value or ""))
               for node in nodes for value in node.attrs.values()):
            return _result("unknown", reason="ambiguous_certitude_attribute")
        for anchor in (node for node in nodes if node.tag == "a"):
            href = anchor.attrs.get("href")
            if not href:
                continue
            if href.strip().lower().startswith("javascript:"):
                return _result("unknown", reason="unverified_interactive_content")
            try:
                host = (urlsplit(urljoin(page_url, href.strip())).hostname or "").lower()
            except ValueError:
                continue
            if host.removeprefix("www.") in _SHORTENER_HOSTS:
                return _result("unknown", reason="shortened_description_link")
    return _result("not_linked", reason="complete_description_has_no_link")
