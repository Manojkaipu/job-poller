"""Tiny HTTP client on urllib: JSON/text GET and POST with timeouts and retries on transient errors."""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request

USER_AGENT = "Mozilla/5.0 (compatible; job-poller/0.1; personal job alerts)"
TIMEOUT = 30
RETRIES = 3
RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _request(url: str, *, body: dict | None = None, accept: str = "application/json",
             headers: dict | None = None) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": USER_AGENT, "Accept": accept, **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS or attempt == RETRIES - 1:
                raise FetchError(f"HTTP {e.code} from {url}", e.code) from None
            # A 429 without Retry-After usually means a per-minute quota: back off for real.
            wait = float(e.headers.get("Retry-After") or 0) or (10 * 2 ** attempt if e.code == 429 else 2 ** attempt)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == RETRIES - 1:
                raise FetchError(f"{type(e).__name__} from {url}: {e}") from None
            wait = 2 ** attempt
        time.sleep(min(wait, 60) + random.random())
    raise AssertionError("unreachable")


def get_json(url: str, **kw):
    raw = _request(url, **kw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise FetchError(f"not JSON from {url} (board moved or needs a different slug?)") from None


def post_json(url: str, body: dict, **kw):
    return json.loads(_request(url, body=body, **kw))


def get_text(url: str, **kw) -> str:
    return _request(url, **kw).decode("utf-8", "replace")
