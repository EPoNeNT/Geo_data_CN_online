#!/usr/bin/env python3
"""Fill analysis-only reg_place values for users whose finds are private.

Project-GC is accessed through one persistent Chrome profile so its login and
Anubis verification can be reused. The browser blocks non-essential assets and
all ProfileStats modules except Milestones. Results are checkpointed before
optional database updates, making interrupted runs resumable.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from runtime_utils import connect_postgres, minimize_cookie_value, require_env


ROOT = Path(__file__).resolve().parent
DEFAULT_PROFILE_DIR = ROOT / ".runtime" / "project-gc-profile"
DEFAULT_CHECKPOINT = ROOT / "analysis" / "cohort-dashboard" / "private-reg-place-backfill.jsonl"
DEFAULT_CACHE_METADATA = ROOT / "analysis" / "cohort-dashboard" / "private-cache-metadata.json"
PROJECT_GC_HOME = "https://project-gc.com/"
PROFILE_URL = "https://project-gc.com/Statistics/ProfileStats?profile-name={user_name}#Milestones"
MILESTONE_API_MARKERS = ("Milestones%3AMilestonesModule", "Milestones:MilestonesModule")

LOGGER = logging.getLogger("private_reg_place_backfill")


def configure_text_stream(stream: Any) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError, ValueError):
        pass


@dataclass(frozen=True)
class Milestone:
    milestone_number: int
    find_date: date
    gc_code: str
    name: str


@dataclass(frozen=True)
class CacheMetadata:
    gc_code: str
    country: str
    placed_date: date
    source: str


class _TextExtractor(HTMLParser):
    BREAK_TAGS = {"br", "div", "p", "li", "table", "section"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in self.BREAK_TAGS or lowered == "tr":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"td", "th"}:
            self.parts.append(" ")
        elif lowered in self.BREAK_TAGS or lowered == "tr":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = []
        for line in "".join(self.parts).splitlines():
            normalized = re.sub(r"\s+", " ", line).strip()
            if normalized:
                lines.append(normalized)
        return "\n".join(lines)


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    return parser.text()


def _strings_from_json(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings_from_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings_from_json(item)


def api_payload_to_text(payload: str) -> str:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        candidates = [value for value in _strings_from_json(parsed) if "GC" in value]
        if candidates:
            payload = max(candidates, key=len)
    return html_to_text(payload) if "<" in payload and ">" in payload else payload


def extract_milestones_from_text(text: str) -> dict[int, Milestone]:
    milestones: dict[int, Milestone] = {}
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    pattern = re.compile(
        r"^\s*(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(.*?)\b(GC[A-Z0-9]{2,})\b\s*(.*)$",
        re.IGNORECASE,
    )
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        number = int(match.group(1))
        if number in milestones:
            continue
        milestones[number] = Milestone(
            milestone_number=number,
            find_date=date.fromisoformat(match.group(2)),
            gc_code=match.group(4).upper(),
            name=match.group(5).strip(),
        )

    number_pattern = re.compile(r"^\d+$")
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    gc_pattern = re.compile(r"^GC[A-Z0-9]{2,}$", re.IGNORECASE)
    for index, line in enumerate(lines):
        if not number_pattern.fullmatch(line) or index + 2 >= len(lines):
            continue
        if not date_pattern.fullmatch(lines[index + 1]):
            continue
        gc_index = next(
            (
                candidate
                for candidate in range(index + 2, min(index + 5, len(lines)))
                if gc_pattern.fullmatch(lines[candidate])
            ),
            None,
        )
        if gc_index is None or gc_index + 1 >= len(lines):
            continue
        number = int(line)
        if number in milestones:
            continue
        milestones[number] = Milestone(
            milestone_number=number,
            find_date=date.fromisoformat(lines[index + 1]),
            gc_code=lines[gc_index].upper(),
            name=lines[gc_index + 1],
        )
    return milestones


def choose_reg_place_milestone(
    milestones: dict[int, Milestone],
    placed_dates: dict[str, date],
) -> Milestone:
    first = milestones.get(1)
    if first is None:
        raise ValueError("First milestone is unavailable")
    first_placed = placed_dates.get(first.gc_code)
    if first_placed is None:
        raise ValueError(f"Cache placement date is unavailable for {first.gc_code}")
    if first.find_date >= first_placed:
        return first

    tenth = milestones.get(10)
    if tenth is None:
        raise ValueError("Invalid first milestone and 10th milestone is unavailable")
    tenth_placed = placed_dates.get(tenth.gc_code)
    if tenth_placed is None:
        raise ValueError(f"Cache placement date is unavailable for 10th milestone {tenth.gc_code}")
    if tenth.find_date < tenth_placed:
        raise ValueError("10th milestone also precedes its cache placement date")
    return tenth


def parse_cache_placed_date(page_html: str) -> date | None:
    compact = re.sub(r"\s+", " ", unescape(page_html))
    year_first = re.search(
        r"Hidden\s*:?\s*(?:</?[^>]+>\s*)*(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})",
        compact,
        re.IGNORECASE,
    )
    if year_first:
        return date(int(year_first.group(1)), int(year_first.group(2)), int(year_first.group(3)))
    us_date = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", compact)
    if us_date:
        return date(int(us_date.group(3)), int(us_date.group(1)), int(us_date.group(2)))
    iso_date = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", compact)
    if iso_date:
        return date(int(iso_date.group(1)), int(iso_date.group(2)), int(iso_date.group(3)))
    return None


def normalize_country_from_cache_title(title: str) -> str | None:
    decoded = re.sub(r"\s+", " ", unescape(title)).strip()
    prefix_match = re.search(r"^(.*?)\s+created by\b", decoded, re.IGNORECASE)
    if not prefix_match:
        return None
    prefix = prefix_match.group(1)
    typed_location = re.search(
        r"\([^)]*cache[^)]*\)\s+in\s+(.+)$",
        prefix,
        re.IGNORECASE,
    )
    if typed_location:
        location = typed_location.group(1).strip()
    else:
        parts = re.split(r"\s+in\s+", prefix, flags=re.IGNORECASE)
        if len(parts) < 2:
            return None
        location = parts[-1].strip()
    return location.rsplit(",", 1)[-1].strip() or None


def parse_cookie_header(raw_cookie: str) -> list[tuple[str, str]]:
    cookies = []
    for part in raw_cookie.strip().strip('"').strip("'").split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name and value:
            cookies.append((name, value))
    return cookies


def should_seed_cookie_from_env(name: str) -> bool:
    lowered = name.lower()
    return "anubis" not in lowered and lowered not in {"cf_clearance", "__cf_bm"}


def is_tables_api_url(url: str) -> bool:
    lowered = url.lower().split("?", 1)[0].rstrip("/")
    return "/api/web/page/profile-stats/token:" in lowered and lowered.endswith("/tables")


def milestone_api_url_from_tables_url(tables_url: str) -> str:
    if not is_tables_api_url(tables_url):
        raise ValueError(f"Not a Project-GC tables API URL: {tables_url}")
    return tables_url.rstrip("/")[:-len("tables")] + "modules/Milestones%3AMilestonesModule"


def is_milestone_api_url(url: str) -> bool:
    return any(marker in url for marker in MILESTONE_API_MARKERS)


def _date_value(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        latest[record["user_name"]] = record
    return latest


def append_checkpoint(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        handle.flush()


class CacheMetadataResolver:
    def __init__(self, conn, cache_path: Path) -> None:
        self.conn = conn
        self.cache_path = cache_path
        self.persisted: dict[str, dict[str, str]] = load_json(cache_path, {})
        self.session = requests.Session()
        cookie = minimize_cookie_value(
            os.getenv("REG_COOKIE")
            or os.getenv("REG-COOKIE")
            or os.getenv("GEOCOOKIE_NONPREMIUM")
            or os.getenv("GEOCOOKIE_PREMIUM")
            or ""
        )
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Cookie": cookie,
            }
        )

    def get(self, gc_code: str) -> CacheMetadata:
        code = gc_code.upper()
        persisted = self.persisted.get(code)
        if persisted:
            return CacheMetadata(
                gc_code=code,
                country=persisted["country"],
                placed_date=date.fromisoformat(persisted["placed_date"]),
                source=persisted.get("source", "metadata_cache"),
            )

        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT country, placed_date FROM caches WHERE code = %s",
                (code,),
            )
            row = cursor.fetchone()
        if row and row[0] and row[1]:
            metadata = CacheMetadata(code, row[0], _date_value(row[1]), "database")
        else:
            response = self.session.get(f"https://coord.info/{code}", timeout=30)
            response.raise_for_status()
            title_match = re.search(r"<title>(.*?)</title>", response.text, re.IGNORECASE | re.DOTALL)
            country = normalize_country_from_cache_title(title_match.group(1) if title_match else "")
            placed_date = parse_cache_placed_date(response.text)
            if not country or not placed_date:
                raise ValueError(f"Could not parse cache metadata for {code}")
            metadata = CacheMetadata(code, country, placed_date, "geocaching_page")

        self.persisted[code] = {
            "country": metadata.country,
            "placed_date": metadata.placed_date.isoformat(),
            "source": metadata.source,
        }
        save_json(self.cache_path, self.persisted)
        return metadata


class ProjectGcBrowser:
    def __init__(self, profile_dir: Path, headless: bool, timeout_seconds: int) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required. Run: python -m pip install -r requirements.txt"
            ) from exc

        profile_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_ms = timeout_seconds * 1000
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=headless,
            locale="zh-CN",
            args=[] if headless else ["--window-position=80,80"],
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self._seed_cookies_from_env()
        self.page.set_default_timeout(self.timeout_ms)
        self.route_handler = self._route

    @staticmethod
    def _route(route) -> None:
        request = route.request
        if request.resource_type in {"image", "media", "font", "stylesheet"}:
            route.abort()
            return
        url = request.url
        if "/api/web/page/profile-stats/" in url and "/modules/" in url:
            if is_milestone_api_url(url):
                route.continue_()
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"data":null,"meta":null,"status":{"message":"ok"}}',
            )
            return
        route.continue_()

    def _seed_cookies_from_env(self) -> None:
        raw_cookie = os.getenv("PROJECT_GC_COOKIE") or ""
        if not raw_cookie:
            return
        existing_names = {
            cookie["name"] for cookie in self.context.cookies([PROJECT_GC_HOME])
        }
        cookies = [
            {
                "name": name,
                "value": value,
                "domain": ".project-gc.com",
                "path": "/",
                "secure": True,
            }
            for name, value in parse_cookie_header(raw_cookie)
            if name not in existing_names and should_seed_cookie_from_env(name)
        ]
        if cookies:
            self.context.add_cookies(cookies)

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:
            LOGGER.debug("Browser context was already closed", exc_info=True)
        try:
            self.playwright.stop()
        except Exception:
            LOGGER.debug("Playwright was already stopped", exc_info=True)

    def login_only(self) -> None:
        self.page.unroute("**/*")
        self.page.goto(PROJECT_GC_HOME, wait_until="domcontentloaded", timeout=self.timeout_ms)
        print("请在打开的 Chrome 中完成 Project-GC 登录和机器人验证。完成后回到终端按 Enter。")
        input()

    def bootstrap_api_access(self, user_name: str) -> None:
        self.page.unroute("**/*")
        tables_urls: list[str] = []

        def capture_tables(response) -> None:
            if is_tables_api_url(response.url):
                tables_urls.append(response.url)

        self.page.on("response", capture_tables)
        try:
            profile_url = PROFILE_URL.format(user_name=quote(user_name, safe=""))
            self.page.goto(profile_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            deadline = time.monotonic() + min(30, self.timeout_ms / 1000)
            while not tables_urls and time.monotonic() < deadline:
                self.page.wait_for_timeout(250)
            if not tables_urls:
                raise RuntimeError("Could not discover the Project-GC tables API URL")

            try:
                self.page.goto(tables_urls[-1], wait_until="commit", timeout=self.timeout_ms)
            except Exception:
                pass
            print(
                "请在 Chrome 中等待并完成 Project-GC API 的机器人验证。"
                "页面显示 JSON 或表结构数据后，回到终端按 Enter。"
            )
            input()
            body = self.page.locator("body").inner_text(timeout=10_000)
            if classify_project_gc_page(body) == "anubis_challenge":
                raise RuntimeError("API Anubis verification is still active")
        finally:
            self.page.remove_listener("response", capture_tables)

    def fetch_milestones(self, user_name: str) -> tuple[dict[int, Milestone], str]:
        url = PROFILE_URL.format(user_name=quote(user_name, safe=""))
        state: dict[str, Any] = {"tables_url": None}

        def capture_bootstrap_response(response) -> None:
            if not is_tables_api_url(response.url):
                return
            try:
                payload = response.json()
            except Exception:
                return
            if (payload.get("status") or {}).get("message") != "ok":
                return
            state["tables_url"] = response.url

        self.page.on("response", capture_bootstrap_response)
        try:
            navigation_started = time.monotonic()
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            deadline = time.monotonic() + self.timeout_ms / 1000
            last_status = "unknown"
            while time.monotonic() < deadline:
                if state["tables_url"]:
                    self.page.evaluate("window.stop()")
                    api_url = milestone_api_url_from_tables_url(state["tables_url"])
                    result = self.page.evaluate(
                        """async (url) => {
                            const response = await fetch(url, {
                                method: "GET",
                                credentials: "include",
                                headers: {Accept: "application/json"},
                            });
                            return {
                                status: response.status,
                                body: await response.text(),
                            };
                        }""",
                        api_url,
                    )
                    payload = result["body"]
                    page_status = classify_project_gc_page(payload)
                    if page_status in {
                        "anubis_challenge",
                        "project_gc_page_error",
                        "project_gc_rate_limited",
                    }:
                        raise RuntimeError(page_status)
                    if result["status"] != 200:
                        raise RuntimeError(f"milestone_http_{result['status']}")
                    try:
                        api_payload = json.loads(payload)
                    except json.JSONDecodeError:
                        api_payload = {}
                    api_message = (api_payload.get("status") or {}).get("message")
                    if api_message and api_message != "ok":
                        raise RuntimeError(f"milestone_api_{api_message}")
                    milestones = extract_milestones_from_text(api_payload_to_text(payload))
                    if milestones.get(1):
                        return milestones, api_url
                    raise RuntimeError("milestone_parse_failed")

                body = self.page.locator("body").inner_text(timeout=5_000)
                last_status = classify_project_gc_page(body)
                elapsed = time.monotonic() - navigation_started
                if page_status_is_terminal(last_status, elapsed):
                    raise RuntimeError(last_status)
                self.page.wait_for_timeout(500)
            raise RuntimeError(last_status if last_status != "unknown" else "milestone_timeout")
        finally:
            self.page.remove_listener("response", capture_bootstrap_response)


def classify_project_gc_page(body: str) -> str:
    lowered = body.lower()
    if "rate limit exceeded for ip" in lowered:
        return "project_gc_rate_limited"
    if (
        "there was an error" in lowered
        and "please try refreshing the page" in lowered
        and "contact support" in lowered
    ):
        return "project_gc_page_error"
    if "authentication required" in lowered or "please authenticate" in lowered:
        return "authentication_required"
    if re.search(r"(?:this\s+)?user.{0,80}(?:has\s+)?opt(?:ed)?[ -]out", lowered):
        return "opt_out"
    if "opted out from project-gc" in lowered:
        return "opt_out"
    if "has opted out of third-party data sharing" in lowered:
        return "opt_out"
    if "elected to hide" in lowered and "detailed statistics" in lowered:
        return "hidden"
    if "profile statistics are hidden" in lowered:
        return "hidden"
    if "anubis" in lowered or "not a bot" in lowered or "不是机器人" in body:
        return "anubis_challenge"
    return "unknown"


def retry_delay_for_error(error: str, attempt: int) -> int:
    if error == "project_gc_page_error" or error == "anubis_challenge":
        return min(60, 30 * max(1, attempt))
    if error == "milestone_http_429" or "rate_limit" in error:
        return 60
    return min(10, 2 ** max(1, attempt))


def page_status_is_terminal(status: str, elapsed_seconds: float) -> bool:
    if status == "project_gc_page_error":
        return elapsed_seconds >= 10
    return status in {
        "authentication_required",
        "hidden",
        "opt_out",
        "anubis_challenge",
        "project_gc_rate_limited",
    }


def is_retryable_project_gc_error(error: str) -> bool:
    if error in {"authentication_required", "hidden", "opt_out"}:
        return False
    deterministic_prefixes = (
        "Could not parse cache metadata",
        "Cache placement date is unavailable",
        "Invalid first milestone",
        "10th milestone also precedes",
        "First milestone is unavailable",
    )
    return not error.startswith(deterministic_prefixes)


def is_valid_resolved_checkpoint(result: dict[str, Any]) -> bool:
    if result.get("status") != "resolved":
        return False
    reg_place = result.get("reg_place")
    if not isinstance(reg_place, str) or not reg_place.strip():
        return False
    return re.search(r"\([^)]*cache[^)]*\)\s+in\s+", reg_place, re.IGNORECASE) is None


def should_wait_for_project_gc_rate_limit(error: str) -> bool:
    return error == "project_gc_rate_limited"


def fetch_private_users(conn, limit: int | None) -> list[dict[str, Any]]:
    sql = (
        'SELECT user_name, guid, registration_date FROM "user" '
        "WHERE reg_place = %s ORDER BY user_name"
    )
    params: list[Any] = ["private"]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return [
            {"user_name": row[0], "guid": row[1], "registration_date": row[2]}
            for row in cursor.fetchall()
        ]


def update_reg_places(conn, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    updated = 0
    with conn.cursor() as cursor:
        for row in rows:
            cursor.execute(
                'UPDATE "user" SET reg_place = %s WHERE user_name = %s AND reg_place = %s',
                (row["reg_place"], row["user_name"], "private"),
            )
            updated += cursor.rowcount
    conn.commit()
    return updated


def process_user(
    user: dict[str, Any],
    browser: ProjectGcBrowser,
    resolver: CacheMetadataResolver,
) -> dict[str, Any]:
    milestones, api_url = browser.fetch_milestones(user["user_name"])
    first = milestones.get(1)
    if first is None:
        raise ValueError("First milestone is unavailable")
    first_metadata = resolver.get(first.gc_code)
    placed_dates = {first.gc_code: first_metadata.placed_date}
    metadata_by_code = {first.gc_code: first_metadata}

    if first.find_date < first_metadata.placed_date:
        tenth = milestones.get(10)
        if tenth is None:
            raise ValueError("Invalid first milestone and 10th milestone is unavailable")
        tenth_metadata = resolver.get(tenth.gc_code)
        placed_dates[tenth.gc_code] = tenth_metadata.placed_date
        metadata_by_code[tenth.gc_code] = tenth_metadata

    selected = choose_reg_place_milestone(milestones, placed_dates)
    selected_metadata = metadata_by_code[selected.gc_code]
    return {
        "user_name": user["user_name"],
        "status": "resolved",
        "reg_place": selected_metadata.country,
        "source_milestone": selected.milestone_number,
        "find_date": selected.find_date.isoformat(),
        "gc_code": selected.gc_code,
        "cache_name": selected.name,
        "cache_placed_date": selected_metadata.placed_date.isoformat(),
        "cache_metadata_source": selected_metadata.source,
        "first_find_date": first.find_date.isoformat(),
        "first_find_gc_code": first.gc_code,
        "first_find_cache_placed_date": first_metadata.placed_date.isoformat(),
        "first_find_valid": first.find_date >= first_metadata.placed_date,
        "project_gc_api_url": api_url,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login-only", action="store_true", help="Open the persistent Chrome profile for manual login.")
    parser.add_argument(
        "--bootstrap-api",
        action="store_true",
        help="Open the discovered tables API for one-time manual Anubis verification.",
    )
    parser.add_argument("--bootstrap-user", default="(Korbi)", help="Profile used to discover the tables API URL.")
    parser.add_argument("--apply", action="store_true", help="Write resolved countries to the user table. Default is dry-run.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N currently-private users.")
    parser.add_argument("--batch-size", type=int, default=50, help="Database commit size when --apply is used.")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between users in seconds.")
    parser.add_argument(
        "--rate-limit-wait",
        type=int,
        default=3600,
        help="Seconds to wait before retrying the same user after an IP rate limit.",
    )
    parser.add_argument("--timeout", type=int, default=120, help="Browser response timeout in seconds.")
    parser.add_argument("--retries", type=int, default=2, help="Attempts per Project-GC profile.")
    parser.add_argument("--headless", action="store_true", help="Use headless Chrome; Anubis may reject this mode.")
    parser.add_argument("--retry-unresolved", action="store_true", help="Retry users previously checkpointed as unresolved.")
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--cache-metadata", type=Path, default=DEFAULT_CACHE_METADATA)
    return parser.parse_args(argv)


def main() -> int:
    configure_text_stream(sys.stdout)
    configure_text_stream(sys.stderr)
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(ROOT / ".env")

    browser = ProjectGcBrowser(args.profile_dir, args.headless, args.timeout)
    if args.login_only:
        try:
            browser.login_only()
            return 0
        finally:
            browser.close()
    if args.bootstrap_api:
        try:
            browser.bootstrap_api_access(args.bootstrap_user)
            return 0
        finally:
            browser.close()

    conn = connect_postgres(require_env("DATABASE_URL"), connect_timeout=10)
    resolver = CacheMetadataResolver(conn, args.cache_metadata)
    latest = load_checkpoint(args.checkpoint)
    pending_updates: list[dict[str, Any]] = []
    resolved_count = 0
    unresolved_count = 0

    try:
        users = fetch_private_users(conn, args.limit)
        LOGGER.info("Currently private users selected: %s", len(users))
        for index, user in enumerate(users, start=1):
            existing = latest.get(user["user_name"])
            if existing and is_valid_resolved_checkpoint(existing):
                result = existing
            elif existing and existing.get("status") != "resolved" and not args.retry_unresolved:
                unresolved_count += 1
                continue
            else:
                result = None
                attempt = 1
                max_attempts = max(1, args.retries)
                while attempt <= max_attempts:
                    try:
                        result = process_user(user, browser, resolver)
                        break
                    except Exception as exc:
                        error = str(exc) or type(exc).__name__
                        if error == "authentication_required":
                            raise RuntimeError(
                                "Project-GC authentication is required. Run --login-only first."
                            ) from exc
                        if should_wait_for_project_gc_rate_limit(error):
                            if args.apply and pending_updates:
                                updated = update_reg_places(conn, pending_updates)
                                LOGGER.info(
                                    "Committed %s verified updates before rate-limit wait",
                                    updated,
                                )
                                pending_updates = []
                            wait_seconds = max(0, args.rate_limit_wait)
                            LOGGER.warning(
                                "Project-GC IP rate limit reached for %s; waiting %ss before retry",
                                user["user_name"],
                                wait_seconds,
                            )
                            time.sleep(wait_seconds)
                            continue
                        if (
                            attempt < max_attempts
                            and is_retryable_project_gc_error(error)
                        ):
                            retry_delay = retry_delay_for_error(error, attempt)
                            LOGGER.warning(
                                "%s failed with %s; retrying in %ss (attempt %s/%s)",
                                user["user_name"],
                                error,
                                retry_delay,
                                attempt,
                                max_attempts,
                            )
                            time.sleep(retry_delay)
                            attempt += 1
                            continue
                        else:
                            result = {
                                "user_name": user["user_name"],
                                "status": "unresolved",
                                "reason": error,
                                "updated_at": datetime.now().isoformat(timespec="seconds"),
                            }
                            break
                append_checkpoint(args.checkpoint, result)
                latest[user["user_name"]] = result

            if result["status"] == "resolved":
                resolved_count += 1
                if args.apply:
                    pending_updates.append(result)
                    if len(pending_updates) >= max(1, args.batch_size):
                        updated = update_reg_places(conn, pending_updates)
                        LOGGER.info("Committed %s reg_place updates", updated)
                        pending_updates = []
            else:
                unresolved_count += 1

            print(
                f"[{index}/{len(users)}] {user['user_name']}: "
                f"{result.get('reg_place') or result.get('reason')}"
            )
            if index < len(users) and args.delay > 0:
                time.sleep(args.delay)

        if args.apply and pending_updates:
            updated = update_reg_places(conn, pending_updates)
            LOGGER.info("Committed final %s reg_place updates", updated)
        LOGGER.info(
            "Finished: resolved=%s unresolved=%s apply=%s",
            resolved_count,
            unresolved_count,
            args.apply,
        )
        return 0
    finally:
        resolver.session.close()
        conn.close()
        browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
