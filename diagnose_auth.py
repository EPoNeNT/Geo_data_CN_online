#!/usr/bin/env python3
"""Read-only Geocaching authentication checks for local and Actions runners."""

import argparse
import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from runtime_utils import (
    create_geocaching_session,
    looks_like_login_page,
    looks_like_public_geocaching_page,
    minimize_cookie_value,
    parse_cookie_fields,
)


def load_env_file(path: str) -> None:
    """Load an explicit local file without overriding the process environment."""
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def cookie_format_metadata(cookie: str) -> dict:
    """Describe authentication field structure without decoding identity claims."""
    fields = parse_cookie_fields(cookie)
    return {
        name: {
            "length": len(fields[name]),
            "dot_segment_lengths": [len(part) for part in fields[name].split(".")],
            "percent_encoded": bool(re.search(r"%[0-9a-fA-F]{2}", fields[name])),
            "has_whitespace": any(char.isspace() for char in fields[name]),
        }
        for name in ("gspkauth", "jwt") if name in fields
    }


def response_metadata(response: requests.Response) -> dict:
    """Return only safe metadata, never URLs with queries or response contents."""
    hops = [*response.history, response]
    return {
        "status": response.status_code,
        "final_host": urlsplit(response.url).hostname,
        "final_path": urlsplit(response.url).path,
        "hops": [
            {
                "status": hop.status_code,
                "sent_auth_cookie": "gspkauth" in parse_cookie_fields(hop.request.headers.get("Cookie", "")),
                "set_cookie_names": re.findall(r"(?:^|,\s*)([\w.-]+)=", hop.headers.get("Set-Cookie", "")),
            }
            for hop in hops
        ],
        "login_page": looks_like_login_page(response.url, response.text if response.text.lstrip().startswith("<") else ""),
        "public_page": looks_like_public_geocaching_page(response.text),
        "challenge": response.status_code == 403 or bool(
            re.search(r"<title[^>]*>\s*(just a moment|access denied|attention required)", response.text, re.I)
        ),
        "authenticated_marker": bool(re.search(r'"isAuthenticated"\s*:\s*true', response.text)),
        "auth_cookie_updates": [
            {
                "domain": cookie.domain,
                "secure": cookie.secure,
                "expires_utc": datetime.fromtimestamp(cookie.expires, timezone.utc).isoformat() if cookie.expires else None,
            }
            for hop in hops for cookie in getattr(hop, "cookies", ()) if cookie.name == "gspkauth"
        ],
    }


def check_cookie(cookie: str, cache: str, legacy: bool = False, ignore_proxy: bool = False, full_cookie: bool = False) -> dict:
    session = requests.Session() if legacy or full_cookie else create_geocaching_session(cookie)
    if full_cookie:
        for name, value in parse_cookie_fields(cookie).items():
            session.cookies.set(name, value, domain=".geocaching.com", path="/", secure=True)
    session.trust_env = not ignore_proxy
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
    }
    if legacy:
        headers["Cookie"] = minimize_cookie_value(cookie)
    mode = "legacy_header" if legacy else ("full_cookie_jar" if full_cookie else "cookie_jar")
    result = {"mode": mode, "ok": False}
    try:
        response = session.get(
            "https://www.geocaching.com/seek/cache_details.aspx",
            params={"wp": cache}, headers=headers, timeout=20,
        )
        result["detail"] = response_metadata(response)
        result["auth_cookie_in_jar_after"] = any(c.name == "gspkauth" for c in session.cookies)
        original_auth = parse_cookie_fields(cookie).get("gspkauth")
        result["auth_cookie_value_updated"] = any(
            item.name == "gspkauth" and item.value != original_auth
            for hop in [*response.history, response] for item in getattr(hop, "cookies", ())
        )
        token = re.search(r"userToken\s*=\s*['\"]([^'\"]+)['\"]", response.text)
        result["has_token"] = bool(token)
        detail = result["detail"]
        if response.status_code != 200 or detail["login_page"] or detail["public_page"] or detail["challenge"] or not token:
            return result
        headers["Referer"] = f"https://www.geocaching.com/seek/geocache_logs.aspx?code={cache}"
        logs = session.get(
            "https://www.geocaching.com/seek/geocache.logbook",
            params={"tkn": token.group(1), "idx": 1, "num": 6, "sp": "false", "sf": "false", "showOwnerOnly": "false", "decrypt": "false"},
            headers=headers, timeout=20,
        )
        result["logbook"] = response_metadata(logs)
        if logs.status_code != 200 or result["logbook"]["login_page"] or result["logbook"]["challenge"]:
            return result
        body = logs.json()
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            result["error"] = "unexpected_logbook_schema"
            return result
        result["log_count"] = len(body["data"])
        info = body.get("pageInfo")
        if isinstance(info, dict):
            # Page counts are useful; arbitrary API message fields may contain secrets.
            result["page_info"] = {key: info[key] for key in ("size", "totalRows") if isinstance(info.get(key), int)}
        capped = result["log_count"] <= 5 and result.get("page_info", {}).get("totalRows", 0) > result["log_count"]
        result["ok"] = (
            body.get("success") is not False
            and str(body.get("status", "")).lower() not in {"error", "fail", "failed", "false"}
            and not capped
        )
        return result
    except (requests.RequestException, ValueError) as exc:
        # RequestException messages can embed the logbook token in the URL.
        result["error"] = type(exc).__name__
        return result
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="Optional local .env file; Actions uses Secrets directly")
    parser.add_argument("--cache", default="GCAEW7A", help="A public, non-Premium cache code")
    parser.add_argument("--compare-legacy", action="store_true", help="Also test the previous fixed-header behavior")
    parser.add_argument("--compare-full-cookie", action="store_true", help="Also check whether discarded cookie fields affect authentication")
    parser.add_argument("--ignore-proxy", action="store_true", help="Diagnostic only: bypass environment proxy settings")
    args = parser.parse_args()
    if not re.fullmatch(r"GC[0-9A-Z]+", args.cache.upper()):
        parser.error("--cache must be a GC code")
    if args.env_file:
        load_env_file(args.env_file)
    print(json.dumps({
        "environment": {"os": platform.system(), "python": platform.python_version(), "requests": requests.__version__,
                        "github_actions": os.getenv("GITHUB_ACTIONS") == "true",
                        "proxy_configured": any(os.getenv(name) for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")),
                        "ignore_proxy": args.ignore_proxy},
        "cache": args.cache.upper(),
    }), flush=True)
    all_ok = True
    for profile, names in (
        ("nonpremium", ("GEOCOOKIE_NONPREMIUM", "GEOCACHING_COOKIE")),
        ("premium", ("GEOCOOKIE_PREMIUM",)),
    ):
        source = next((name for name in names if os.getenv(name, "").strip()), None)
        cookie = os.getenv(source, "") if source else ""
        fields = parse_cookie_fields(cookie)
        print(json.dumps({"profile": profile, "source": source, "cookie_fields": sorted(fields),
                          "has_auth_cookie": bool(fields.get("gspkauth")),
                          "auth_format": cookie_format_metadata(cookie)}), flush=True)
        if not fields.get("gspkauth"):
            all_ok = False
            continue
        result = check_cookie(cookie, args.cache.upper(), ignore_proxy=args.ignore_proxy)
        print(json.dumps({"profile": profile, **result}), flush=True)
        all_ok = all_ok and result["ok"]
        if args.compare_legacy:
            print(json.dumps({"profile": profile, **check_cookie(cookie, args.cache.upper(), legacy=True, ignore_proxy=args.ignore_proxy)}), flush=True)
        if args.compare_full_cookie:
            print(json.dumps({"profile": profile, **check_cookie(cookie, args.cache.upper(), full_cookie=True, ignore_proxy=args.ignore_proxy)}), flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
