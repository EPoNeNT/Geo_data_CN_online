"""Encrypted, optional persistence of the ordinary account's renewed Cookie."""

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time

import requests

from runtime_utils import (
    is_login_url,
    looks_like_login_page,
    looks_like_public_geocaching_page,
    minimize_cookie_value,
    parse_cookie_fields,
)


logger = logging.getLogger(__name__)
DEFAULT_STATE_FILE = ".runtime/geocaching-auth/nonpremium.enc"
MAX_STATE_BYTES = 16384


class CookieStateStore:
    """Authenticate encrypted state and bind it to the manually supplied Cookie."""

    def __init__(self, path, key, initial_cookie):
        from cryptography.fernet import Fernet

        self.path = Path(path)
        try:
            self._cipher = Fernet(key.encode("ascii") if isinstance(key, str) else key)
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("Invalid GEOCACHING_COOKIE_STATE_KEY; a Fernet key is required") from None
        self._generation = hashlib.sha256(minimize_cookie_value(initial_cookie).encode()).hexdigest()

    @staticmethod
    def _valid_cookie(value):
        return (
            isinstance(value, str) and 0 < len(value) <= 4096
            and not any(character.isspace() or character in ';,\"\\' for character in value)
        )

    def load(self):
        from cryptography.fernet import InvalidToken

        if not self.path.is_file():
            return None
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError("oversized state")
            payload = json.loads(self._cipher.decrypt(self.path.read_bytes()))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("invalid state schema")
            if payload.get("generation") != self._generation:
                raise ValueError("configured Cookie changed")
            expires = payload.get("expires")
            if type(expires) is not int or expires <= time.time() or not self._valid_cookie(payload.get("value")):
                raise ValueError("expired or invalid state")
            logger.info("Restored encrypted nonpremium authentication state")
            return {"value": payload["value"], "expires": expires}
        except (OSError, ValueError, TypeError, InvalidToken):
            logger.warning("Ignoring unusable nonpremium authentication state; using configured Cookie")
            self.discard()
            return None

    def save(self, value, expires):
        if not self._valid_cookie(value) or type(expires) is not int:
            raise ValueError("Invalid authentication state")
        payload = {"version": 1, "generation": self._generation, "value": value, "expires": expires}
        encrypted = self._cipher.encrypt(json.dumps(payload, separators=(",", ":")).encode())
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".auth-", dir=self.path.parent)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encrypted)
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def discard(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove unusable nonpremium authentication state")


def _authentication_rejected(response):
    """Treat authentication evidence separately from access challenges."""
    if response.status_code == 403:
        return False
    if response.status_code == 401 or is_login_url(response.url):
        return True
    if response.status_code != 200:
        return False
    text = response.text
    if text.lstrip().startswith("{"):
        try:
            payload = response.json()
        except ValueError:
            return False
        if isinstance(payload, dict):
            props = payload.get("pageProps")
            if isinstance(props, dict) and is_login_url(str(props.get("__N_REDIRECT", ""))):
                return True
            # Log text can mention sign-in or contain pageInfo snippets.
            if isinstance(payload.get("data"), list):
                info = payload.get("pageInfo")
                return (
                    isinstance(info, dict) and type(info.get("totalRows")) is int
                    and type(info.get("size")) is int and info["size"] <= 5
                    and len(payload["data"]) <= info["size"] and info["totalRows"] > info["size"]
                )
    return looks_like_login_page(response.url, text) or looks_like_public_geocaching_page(text)


class GeocachingStateSession(requests.Session):
    """Restore renewed authentication and checkpoint only server-issued tickets."""

    def __init__(self, cookie, store):
        super().__init__()
        self._store = store
        self._seed = parse_cookie_fields(minimize_cookie_value(cookie)).get("gspkauth", "")
        self._retried_seed = False
        restored = store.load()
        self.auth_state_source = "cache" if restored else "configured_cookie"
        self._restored = bool(restored)
        if restored:
            self.cookies.set("gspkauth", restored["value"], domain=".geocaching.com", path="/", secure=True, expires=restored["expires"])
        elif self._seed:
            self.cookies.set("gspkauth", self._seed, domain=".geocaching.com", path="/", secure=True)

    def request(self, method, url, **kwargs):
        response = super().request(method, url, **kwargs)
        rejected = _authentication_rejected(response)
        if rejected:
            self._store.discard()
        if rejected and self._restored and not self._retried_seed and self._seed and method.upper() in {"GET", "HEAD"}:
            self._retried_seed = True
            self._restored = False
            self.auth_state_source = "configured_cookie_retry"
            logger.warning("Cached nonpremium authentication rejected; retrying configured Cookie once")
            response.close()
            self.cookies.clear()
            self.cookies.set("gspkauth", self._seed, domain=".geocaching.com", path="/", secure=True)
            response = super().request(method, url, **kwargs)
            rejected = _authentication_rejected(response)
            if rejected:
                self._store.discard()
        if response.status_code == 200 and not rejected:
            renewed = {
                item.value
                for hop in [*response.history, response] for item in hop.cookies
                if item.name == "gspkauth" and item.value and item.expires and item.expires > time.time()
                and item.domain.lstrip(".") in {"geocaching.com", "www.geocaching.com"}
            }
            for item in self.cookies:
                if item.name == "gspkauth" and item.value in renewed and item.expires and item.expires > time.time():
                    try:
                        self._store.save(item.value, item.expires)
                        logger.info("Saved encrypted renewed nonpremium authentication state")
                    except (OSError, ValueError):
                        logger.warning("Could not save renewed nonpremium authentication state")
                    break
        return response


def build_state_session(cookie):
    """Only the explicitly selected nonpremium profile uses encrypted state."""
    key = os.getenv("GEOCACHING_COOKIE_STATE_KEY", "").strip()
    if not key:
        return None
    path = os.getenv("GEOCACHING_COOKIE_STATE_FILE", "").strip() or DEFAULT_STATE_FILE
    return GeocachingStateSession(cookie, CookieStateStore(path, key, cookie))
