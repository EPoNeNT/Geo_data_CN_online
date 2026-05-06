#!/usr/bin/env python3
"""Runtime helpers for GitHub Actions-friendly scripts."""

import logging
import os
import sys
import time
from collections import OrderedDict
from http.cookies import SimpleCookie
from typing import Iterable


class AuthenticationError(RuntimeError):
    """Raised when a request clearly indicates the session is not authenticated."""


def require_env(name: str) -> str:
    """Read a required environment variable or fail fast."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def normalize_cookie_value(value: str | None) -> str:
    """Normalize cookie values from .env files and GitHub Secrets."""
    cookie = (value or "").strip()
    if len(cookie) >= 2 and cookie[0] == cookie[-1] and cookie[0] in {"'", '"'}:
        cookie = cookie[1:-1].strip()
    return cookie


def parse_cookie_fields(cookie: str) -> OrderedDict[str, str]:
    """Parse a Cookie header into ordered name/value fields."""
    parsed: OrderedDict[str, str] = OrderedDict()
    normalized = normalize_cookie_value(cookie)
    if not normalized:
        return parsed

    simple = SimpleCookie()
    try:
        simple.load(normalized)
    except Exception:
        simple = SimpleCookie()

    if simple:
        for key, morsel in simple.items():
            parsed[key] = morsel.value
        return parsed

    for part in normalized.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def minimize_cookie_value(
    value: str | None,
    required_fields: Iterable[str] = ("gspkauth",),
) -> str:
    """Keep only the cookie fields required by Geocaching authenticated requests."""
    fields = parse_cookie_fields(value or "")
    if not fields:
        return ""

    required = [name for name in required_fields if name in fields]
    if not required:
        return normalize_cookie_value(value)
    return "; ".join(f"{name}={fields[name]}" for name in required)


def optional_cookie(
    *names: str,
    required_fields: Iterable[str] = ("gspkauth",),
) -> str:
    """Return the first configured cookie, minimized to required fields."""
    for name in names:
        value = minimize_cookie_value(os.getenv(name), required_fields=required_fields)
        if value:
            return value
    return ""


def require_cookie(
    *names: str,
    required_fields: Iterable[str] = ("gspkauth",),
) -> str:
    """Read a required cookie from one of several env vars, then minimize it."""
    value = optional_cookie(*names, required_fields=required_fields)
    if not value:
        joined = ", ".join(names)
        raise RuntimeError(f"Missing required cookie env var: {joined}")
    return value


def env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean environment flag."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def setup_logging(log_filename: str) -> logging.Logger:
    """Configure stdout-first logging and optional file logging."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)

    handlers = [logging.StreamHandler(sys.stdout)]
    if env_flag("LOG_TO_FILE", default=False):
        handlers.insert(0, logging.FileHandler(log_filename, encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )
    return logging.getLogger(__name__)


def connect_postgres(database_url: str, logger: logging.Logger | None = None, **kwargs):
    """Connect to Postgres with retries for transient CI/Neon network failures."""
    import psycopg2

    attempts = int(os.getenv("DATABASE_CONNECT_RETRIES", "5"))
    initial_delay = float(os.getenv("DATABASE_CONNECT_RETRY_DELAY_SECONDS", "5"))
    max_delay = float(os.getenv("DATABASE_CONNECT_RETRY_MAX_DELAY_SECONDS", "60"))

    if "connect_timeout" not in kwargs:
        kwargs["connect_timeout"] = int(os.getenv("DATABASE_CONNECT_TIMEOUT", "10"))
    elif os.getenv("DATABASE_CONNECT_TIMEOUT"):
        kwargs["connect_timeout"] = int(os.getenv("DATABASE_CONNECT_TIMEOUT", "10"))

    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            return psycopg2.connect(database_url, **kwargs)
        except psycopg2.OperationalError as exc:
            if attempt >= attempts:
                if logger:
                    logger.error("Database connection failed after %s attempts: %s", attempts, exc)
                raise

            delay = min(max_delay, initial_delay * (2 ** (attempt - 1)))
            if logger:
                logger.warning(
                    "Database connection attempt %s/%s failed: %s; retrying in %.1fs",
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
            time.sleep(delay)


def is_login_url(url: str) -> bool:
    """Heuristic check for login-related redirects."""
    if not url:
        return False

    lowered = url.lower()
    markers = (
        "/account/signin",
        "/login",
        "signin?",
        "returnurl=",
        "next-auth",
    )
    return any(marker in lowered for marker in markers)


def looks_like_login_page(url: str, text: str) -> bool:
    """Heuristic check for HTML responses that are actually login pages."""
    if is_login_url(url):
        return True

    lowered = (text or "").lower()
    markers = (
        "<title>log in",
        "<title>sign in",
        "create your free account",
        "sign in to continue",
        "next-auth",
        "account/signin",
    )
    return any(marker in lowered for marker in markers)
