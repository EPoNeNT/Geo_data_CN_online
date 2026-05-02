#!/usr/bin/env python3
"""Runtime helpers for GitHub Actions-friendly scripts."""

import logging
import os
import sys
import time


class AuthenticationError(RuntimeError):
    """Raised when a request clearly indicates the session is not authenticated."""


def require_env(name: str) -> str:
    """Read a required environment variable or fail fast."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required env var: {name}")
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
