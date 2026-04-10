#!/usr/bin/env python3
"""Runtime helpers for GitHub Actions-friendly scripts."""

import logging
import os
import sys


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
