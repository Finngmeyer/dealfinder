from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from dealfinder.http import USER_AGENT, Fetcher, SourceUnavailable

URL = "https://data.example.org/table"


def all_cached_bytes(cache_dir: Path) -> bytes:
    return b"".join(path.read_bytes() for path in cache_dir.rglob("*") if path.is_file())


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class TestCache:
    def test_second_call_is_served_from_disk(self, fetcher, http_mock):
        route = http_mock.get(URL).mock(return_value=httpx.Response(200, text="first"))
        first = fetcher.get("demo", URL, params={"a": "1"})
        second = fetcher.get("demo", URL, params={"a": "1"})
        assert route.call_count == 1
        assert (first.from_cache, second.from_cache) == (False, True)
        assert second.text() == "first"
        assert not second.stale

    def test_different_params_are_different_entries(self, fetcher, http_mock):
        route = http_mock.get(URL).mock(return_value=httpx.Response(200, text="x"))
        fetcher.get("demo", URL, params={"a": "1"})
        fetcher.get("demo", URL, params={"a": "2"})
        assert route.call_count == 2

    def test_expired_entry_is_refreshed(self, tmp_path, http_mock):
        clock = Clock()
        fetcher = Fetcher(tmp_path / "cache", sleep=lambda s: None, now=clock)
        route = http_mock.get(URL).mock(
            side_effect=[httpx.Response(200, text="old"), httpx.Response(200, text="new")]
        )
        fetcher.get("demo", URL, max_age=timedelta(days=1))
        clock.advance(hours=23)
        assert fetcher.get("demo", URL, max_age=timedelta(days=1)).text() == "old"
        clock.advance(hours=2)
        assert fetcher.get("demo", URL, max_age=timedelta(days=1)).text() == "new"
        assert route.call_count == 2

    def test_no_max_age_means_the_copy_never_expires(self, tmp_path, http_mock):
        clock = Clock()
        fetcher = Fetcher(tmp_path / "cache", sleep=lambda s: None, now=clock)
        route = http_mock.get(URL).mock(return_value=httpx.Response(200, text="x"))
        fetcher.get("demo", URL)
        clock.advance(days=5000)
        fetcher.get("demo", URL)
        assert route.call_count == 1

    def test_refresh_ignores_a_fresh_copy(self, fetcher, http_mock):
        route = http_mock.get(URL).mock(
            side_effect=[httpx.Response(200, text="old"), httpx.Response(200, text="new")]
        )
        fetcher.get("demo", URL)
        assert fetcher.get("demo", URL, refresh=True).text() == "new"
        assert route.call_count == 2

    def test_failed_refresh_falls_back_to_the_stale_copy(self, tmp_path, http_mock):
        clock = Clock()
        fetcher = Fetcher(tmp_path / "cache", sleep=lambda s: None, now=clock)
        http_mock.get(URL).mock(side_effect=[httpx.Response(200, text="old"), httpx.Response(404)])
        fetcher.get("demo", URL, max_age=timedelta(days=1))
        clock.advance(days=2)
        again = fetcher.get("demo", URL, max_age=timedelta(days=1))
        assert again.text() == "old"
        assert again.stale and again.from_cache

    def test_damaged_entry_counts_as_missing(self, fetcher, http_mock):
        route = http_mock.get(URL).mock(return_value=httpx.Response(200, text="x"))
        fetcher.get("demo", URL)
        for meta in fetcher.cache_dir.rglob("*.json"):
            meta.write_text("{not json")
        fetcher.get("demo", URL)
        assert route.call_count == 2


class TestOffline:
    def test_nothing_cached(self, tmp_path):
        offline = Fetcher(tmp_path / "cache", offline=True)
        with pytest.raises(SourceUnavailable, match="offline"):
            offline.get("demo", URL)

    def test_cached_copy_is_served_even_when_expired_and_marked_stale(self, tmp_path, http_mock):
        clock = Clock()
        http_mock.get(URL).mock(return_value=httpx.Response(200, text="kept"))
        Fetcher(tmp_path / "cache", now=clock).get("demo", URL)
        clock.advance(days=30)
        offline = Fetcher(tmp_path / "cache", offline=True, now=clock)
        got = offline.get("demo", URL, max_age=timedelta(days=1))
        assert got.text() == "kept"
        assert got.stale

    def test_fresh_cached_copy_is_not_stale(self, tmp_path, http_mock):
        http_mock.get(URL).mock(return_value=httpx.Response(200, text="kept"))
        Fetcher(tmp_path / "cache").get("demo", URL)
        got = Fetcher(tmp_path / "cache", offline=True).get("demo", URL, max_age=timedelta(days=1))
        assert not got.stale


class TestFailures:
    def test_server_errors_are_retried(self, fetcher, http_mock):
        route = http_mock.get(URL).mock(
            side_effect=[httpx.Response(503), httpx.Response(500), httpx.Response(200, text="ok")]
        )
        assert fetcher.get("demo", URL).text() == "ok"
        assert route.call_count == 3

    def test_dropped_connections_are_retried(self, fetcher, http_mock):
        route = http_mock.get(URL).mock(
            side_effect=[httpx.ConnectError("down"), httpx.Response(200, text="ok")]
        )
        assert fetcher.get("demo", URL).text() == "ok"
        assert route.call_count == 2

    def test_gives_up_after_the_configured_attempts(self, tmp_path, http_mock):
        fetcher = Fetcher(tmp_path / "cache", attempts=3, sleep=lambda s: None)
        route = http_mock.get(URL).mock(return_value=httpx.Response(500))
        with pytest.raises(SourceUnavailable, match="after 3 attempts: HTTP 500"):
            fetcher.get("demo", URL)
        assert route.call_count == 3

    def test_retry_after_is_honoured_up_to_a_cap(self, tmp_path, http_mock):
        slept: list[float] = []
        fetcher = Fetcher(tmp_path / "cache", sleep=slept.append)
        http_mock.get(URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "7"}),
                httpx.Response(429, headers={"Retry-After": "9999"}),
                httpx.Response(200, text="ok"),
            ]
        )
        fetcher.get("demo", URL)
        assert 7.0 in slept
        assert 30.0 in slept
        assert max(slept) <= 30.0

    @pytest.mark.parametrize("status", [401, 403])
    def test_rejected_credentials_are_not_retried(self, fetcher, http_mock, status):
        route = http_mock.get(URL).mock(return_value=httpx.Response(status))
        with pytest.raises(SourceUnavailable, match="key or token was not accepted"):
            fetcher.get("demo", URL)
        assert route.call_count == 1

    def test_other_client_errors_are_not_retried(self, fetcher, http_mock):
        route = http_mock.get(URL).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceUnavailable, match="HTTP 404"):
            fetcher.get("demo", URL)
        assert route.call_count == 1

    def test_a_redirect_is_reported_not_followed(self, fetcher, http_mock):
        http_mock.get(URL).mock(
            return_value=httpx.Response(
                302, headers={"Location": "https://data.example.org/missing_key.html"}
            )
        )
        with pytest.raises(SourceUnavailable, match=r"redirected to /missing_key\.html"):
            fetcher.get("demo", URL)

    def test_a_rejected_body_is_not_cached(self, fetcher, http_mock):
        def reject(body: bytes) -> None:
            raise SourceUnavailable("demo", "not the table we asked for")

        route = http_mock.get(URL).mock(return_value=httpx.Response(200, text="<html>error"))
        for _ in range(2):
            with pytest.raises(SourceUnavailable, match="not the table"):
                fetcher.get("demo", URL, validate=reject)
        assert route.call_count == 2
        assert not list(fetcher.cache_dir.rglob("*.body"))


class TestSecretsAndManners:
    def test_secrets_are_sent_but_never_written_to_disk(self, fetcher, http_mock):
        route = http_mock.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        fetcher.get(
            "demo",
            URL,
            params={"state": "39"},
            secret_params={"key": "census-secret-123"},
            headers={"Authorization": "Bearer hud-secret-456"},
        )
        sent = route.calls.last.request
        assert sent.url.params["key"] == "census-secret-123"
        assert sent.headers["Authorization"] == "Bearer hud-secret-456"
        stored = all_cached_bytes(fetcher.cache_dir)
        assert b"census-secret-123" not in stored
        assert b"hud-secret-456" not in stored
        assert b'"state"' in stored  # the non-secret parameter is recorded

    def test_secret_is_not_part_of_the_cache_key(self, fetcher, http_mock):
        route = http_mock.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        fetcher.get("demo", URL, secret_params={"key": "one"})
        fetcher.get("demo", URL, secret_params={"key": "two"})
        assert route.call_count == 1

    def test_secret_does_not_leak_into_error_messages(self, fetcher, http_mock):
        http_mock.get(URL).mock(return_value=httpx.Response(403))
        with pytest.raises(SourceUnavailable) as caught:
            fetcher.get("demo", URL, secret_params={"key": "census-secret-123"})
        assert "census-secret-123" not in str(caught.value)

    def test_identifies_itself_and_does_not_follow_redirects(self, fetcher, http_mock):
        route = http_mock.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        fetcher.get("demo", URL)
        assert route.calls.last.request.headers["User-Agent"] == USER_AGENT

    def test_requests_to_one_host_are_spaced_out(self, tmp_path, http_mock):
        slept: list[float] = []
        fetcher = Fetcher(tmp_path / "cache", min_interval_seconds=5.0, sleep=slept.append)
        http_mock.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        http_mock.get("https://other.example.com/x").mock(
            return_value=httpx.Response(200, text="ok")
        )
        fetcher.get("demo", URL, params={"n": "1"})
        fetcher.get("demo", "https://other.example.com/x")
        assert slept == []  # first request to each host goes straight out
        fetcher.get("demo", URL, params={"n": "2"})
        assert len(slept) == 1 and 4.0 < slept[0] <= 5.0
