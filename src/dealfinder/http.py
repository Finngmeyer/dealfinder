"""One HTTP path for every data source: retries, an on-disk cache, polite pacing.

Rules this module enforces:
  * Every successful response is cached on disk. A fresh-enough copy is served without a request.
  * Secrets never touch the disk: keys passed as ``secret_params`` or in ``headers`` are sent
    with the request but left out of the cache key and the cache metadata.
  * A failed refresh falls back to the last cached copy, marked stale, so an outage or a
    missing connection degrades the data instead of breaking the run.
  * No anti-bot tricks: one plain client, a fixed User-Agent, a pause between requests to the
    same host, and backing off when a server says to slow down.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

USER_AGENT = "dealfinder/0.1 (local real-estate research tool)"
_MAX_RETRY_AFTER_SECONDS = 30.0


class SourceUnavailable(Exception):
    """A data source cannot be used right now. ``reason`` is written for the person reading
    the output, for example "HUD_API_TOKEN is not set"."""

    def __init__(self, source: str, reason: str, *, missing_key: bool = False) -> None:
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason
        self.missing_key = missing_key  # the source is simply not set up, as opposed to failing


class _Retryable(Exception):
    """A failure worth another attempt: a dropped connection, a 429 or a 5xx."""


@dataclass(frozen=True)
class Fetched:
    body: bytes
    fetched_at: datetime
    from_cache: bool
    stale: bool  # an expired cached copy served because the refresh failed or we are offline
    headers: dict[str, str] = field(default_factory=dict)  # only the ones asked for

    def text(self) -> str:
        return self.body.decode("utf-8-sig", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body)


def _now() -> datetime:
    return datetime.now(UTC)


class Fetcher:
    """Cached, retrying GET requests. One instance is shared by all sources in a run."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        offline: bool = False,
        refresh: bool = False,
        client: httpx.Client | None = None,
        attempts: int = 4,
        min_interval_seconds: float = 0.25,
        timeout_seconds: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self.cache_dir = cache_dir
        self.offline = offline
        self.refresh = refresh  # ignore fresh cached copies and download everything again
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=15.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
        )
        self._attempts = attempts
        self._min_interval = min_interval_seconds
        self._sleep = sleep
        self._now = now
        self._last_request: dict[str, float] = {}

    # ---- cache -----------------------------------------------------------------------------

    @staticmethod
    def _key(url: str, params: Mapping[str, str] | None) -> str:
        canonical = json.dumps([url, sorted((params or {}).items())], separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]

    def _paths(self, source: str, key: str) -> tuple[Path, Path]:
        folder = self.cache_dir / source
        return folder / f"{key}.body", folder / f"{key}.json"

    def _read(self, source: str, key: str) -> Fetched | None:
        body_path, meta_path = self._paths(source, key)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return Fetched(
                body=body_path.read_bytes(),
                fetched_at=datetime.fromisoformat(meta["fetched_at"]),
                from_cache=True,
                stale=False,
                headers=dict(meta.get("headers", {})),
            )
        except (OSError, ValueError, KeyError):
            return None  # missing or damaged entry: treat as not cached

    def _write(
        self,
        source: str,
        key: str,
        url: str,
        params: Mapping[str, str] | None,
        body: bytes,
        headers: Mapping[str, str],
    ) -> datetime:
        body_path, meta_path = self._paths(source, key)
        body_path.parent.mkdir(parents=True, exist_ok=True)
        fetched_at = self._now()
        meta = {
            "url": url,
            "params": dict(params or {}),
            "fetched_at": fetched_at.isoformat(),
            "headers": dict(headers),
        }
        for path, data in ((body_path, body), (meta_path, json.dumps(meta).encode())):
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(data)
            temporary.replace(path)
        return fetched_at

    # ---- network ---------------------------------------------------------------------------

    def _pace(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self._min_interval - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = time.monotonic()

    def _request_once(
        self, source: str, url: str, params: Mapping[str, str], headers: Mapping[str, str] | None
    ) -> httpx.Response:
        self._pace(urlsplit(url).netloc)
        try:
            response = self._client.get(url, params=dict(params), headers=dict(headers or {}))
        except httpx.TransportError as error:
            raise _Retryable(f"network error ({type(error).__name__})") from error
        status = response.status_code
        if status == 200:
            return response
        if status == 429 or status >= 500:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                self._sleep(min(float(retry_after), _MAX_RETRY_AFTER_SECONDS))
            raise _Retryable(f"HTTP {status}")
        if status in (401, 403):
            raise SourceUnavailable(source, f"HTTP {status}: the key or token was not accepted")
        if 300 <= status < 400:
            target = urlsplit(response.headers.get("Location", "")).path or "another page"
            raise SourceUnavailable(source, f"the request was redirected to {target}")
        raise SourceUnavailable(source, f"HTTP {status}")

    def _download(
        self, source: str, url: str, params: Mapping[str, str], headers: Mapping[str, str] | None
    ) -> httpx.Response:
        retrying = Retrying(
            stop=stop_after_attempt(self._attempts),
            wait=wait_exponential_jitter(initial=1, max=20),
            retry=retry_if_exception_type(_Retryable),
            sleep=self._sleep,
            reraise=True,
        )
        try:
            return retrying(self._request_once, source, url, params, headers)
        except _Retryable as error:
            raise SourceUnavailable(
                source, f"no usable response after {self._attempts} attempts: {error}"
            ) from error

    # ---- public ----------------------------------------------------------------------------

    def get(
        self,
        source: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        secret_params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        max_age: timedelta | None = None,
        validate: Callable[[bytes], object] | None = None,
        refresh: bool = False,
        before_send: Callable[[], object] | None = None,
        keep_headers: Sequence[str] = (),
    ) -> Fetched:
        """GET with the cache in front.

        ``params`` identify the request and are part of the cache key. ``secret_params`` and
        ``headers`` are sent but never stored. ``max_age`` None means a cached copy never
        expires. ``validate`` may raise SourceUnavailable to reject a body (an error page, the
        wrong shape) before it is cached. ``refresh`` skips a fresh cached copy.

        ``before_send`` runs only when a real request is about to go out, never for a cache
        hit. A budget guard uses it to refuse a metered request; whatever it raises propagates.
        ``keep_headers`` names response headers to keep with the cached copy.
        """
        key = self._key(url, params)
        cached = self._read(source, key)
        if cached is not None and not (refresh or self.refresh):
            fresh = max_age is None or self._now() - cached.fetched_at <= max_age
            if fresh:
                return cached
        if self.offline:
            if cached is not None:
                return replace(cached, stale=True)
            raise SourceUnavailable(source, "offline and nothing is cached yet")
        if before_send is not None:
            before_send()
        try:
            response = self._download(
                source, url, {**(params or {}), **(secret_params or {})}, headers
            )
            body = response.content
            if validate is not None:
                validate(body)
        except SourceUnavailable:
            if cached is not None:
                return replace(cached, stale=True)
            raise
        kept = {name: response.headers[name] for name in keep_headers if name in response.headers}
        fetched_at = self._write(source, key, url, params, body, kept)
        return Fetched(
            body=body, fetched_at=fetched_at, from_cache=False, stale=False, headers=kept
        )

    def close(self) -> None:
        self._client.close()
