"""Alerts: what is due, the digest, the channels, and never sending the same thing twice."""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from dealfinder.alerts import (
    DISCORD_LIMIT,
    Alert,
    build_digest,
    channel_status,
    find_alerts,
    run_alerts,
)
from dealfinder.analysis import DISCLAIMER
from dealfinder.cli import app
from dealfinder.http import Fetcher
from dealfinder.pipeline.classify import classify_all
from dealfinder.settings import Environment
from dealfinder.store.db import session_scope
from dealfinder.store.tables import AlertRow, PropertyRow
from dealfinder.ui import data
from test_classify_pipeline import store
from test_funnel import fourplex, funnel

DISCORD = "https://discord.com/api/webhooks/1/token"
SLACK = "https://hooks.slack.com/services/T/B/x"
TODAY = date(2026, 11, 13)


@pytest.fixture
def offline(tmp_path):
    return Fetcher(tmp_path / "offline", offline=True)


@pytest.fixture
def online(tmp_path):
    """A fetcher that may send (to mocked addresses only) and never really sleeps."""
    return Fetcher(tmp_path / "online", sleep=lambda seconds: None)


@pytest.fixture
def deals(engine, config, offline):
    """Six fourplexes in one county; the cheapest ones land in the buy box with good grades."""
    store(engine, *(fourplex(n, 150_000.0 + 50_000.0 * n) for n in range(1, 7)))
    classify_all(engine, config)
    funnel(engine, config, offline)
    return engine


def environment(**values) -> Environment:
    return Environment(_env_file=None, **values)


def due(engine, config) -> list[Alert]:
    with session_scope(engine) as session:
        return find_alerts(session, config)


def stored(engine) -> list[AlertRow]:
    with session_scope(engine) as session:
        rows = session.scalars(select(AlertRow).order_by(AlertRow.id)).all()
        session.expunge_all()
        return list(rows)


def send(engine, config, fetcher, env=None, **options):
    env = env or environment(discord_webhook_url=DISCORD)
    return run_alerts(engine, config, env, fetcher, send=True, today=lambda: TODAY, **options)


class TestWhatIsDue:
    def test_good_deals_inside_the_buy_box(self, deals, config):
        found = due(deals, config)
        assert found and all(alert.kind == "new_deal" for alert in found)
        with session_scope(deals) as session:
            from dealfinder.pipeline.report import deal_rows

            expected = {
                row.property_id
                for row in deal_rows(session, config)
                if row.in_buy_box and row.grade in ("A", "B")
            }
        assert {alert.property_id for alert in found} == expected
        first = found[0]
        assert first.dedupe_key == f"new_deal:{first.property_id}:0"
        assert first.payload["address"].endswith("Columbus, OH, 43215")
        assert first.payload["rents"] == "estimated" and first.payload["grade"] in ("A", "B")

    def test_the_grades_are_a_setting(self, deals, config):
        config.settings.alerts.grades = ["A"]
        assert all(alert.payload["grade"] == "A" for alert in due(deals, config))
        config.settings.alerts.grades = []
        assert due(deals, config) == []

    def test_unconfirmed_and_outside_deals_never_alert(self, engine, config, offline):
        from test_classify_pipeline import rentcast_listing

        store(engine, rentcast_listing(1, price=150_000.0), fourplex(2, 900_000.0))
        classify_all(engine, config)
        funnel(engine, config, offline)
        assert due(engine, config) == []

    def test_a_deal_that_qualifies_after_a_price_cut(self, engine, config, offline):
        store(engine, *(fourplex(n, 400_000.0 + 10_000.0 * n) for n in range(1, 7)))
        classify_all(engine, config)
        funnel(engine, config, offline)
        assert due(engine, config) == []  # nothing is good enough at these prices
        store(engine, fourplex(1, 200_000.0))  # the first one is cut hard
        funnel(engine, config, offline)
        found = due(engine, config)
        assert [alert.kind for alert in found] == ["qualifies_after_cut"]
        payload = found[0].payload
        assert (payload["was"], payload["price"]) == (410_000.0, 200_000.0)
        assert payload["cut_pct"] == pytest.approx(1 - 200 / 410)

    def test_starred_price_cuts(self, deals, config):
        data.set_star(deals, 6, True)
        store(deals, fourplex(6, 450_000.0 * 0.98))  # 2%: below the 3% bar
        assert [a for a in due(deals, config) if a.kind == "starred_price_cut"] == []
        store(deals, fourplex(6, 420_000.0))  # a further 4.8% in one step
        cuts = [a for a in due(deals, config) if a.kind == "starred_price_cut"]
        assert len(cuts) == 1
        assert cuts[0].payload["was"] == 441_000.0 and cuts[0].payload["price"] == 420_000.0
        assert cuts[0].payload["address"] == "106 Sample St, Columbus, OH, 43215"

    def test_cuts_before_the_star_do_not_count(self, deals, config):
        store(deals, fourplex(6, 400_000.0))  # cut 11% while not starred
        data.set_star(deals, 6, True)
        assert [a for a in due(deals, config) if a.kind == "starred_price_cut"] == []
        # Unstarred properties never alert on cuts.
        store(deals, fourplex(5, 300_000.0))
        assert [a for a in due(deals, config) if a.kind == "starred_price_cut"] == []


class TestDigest:
    def alerts(self, count: int = 2) -> list[Alert]:
        return [
            Alert(
                kind="new_deal",
                dedupe_key=f"new_deal:{n}:0",
                property_id=n,
                payload={
                    "address": f"{100 + n} Sample St, Columbus, OH, 43215",
                    "price": 200_000.0 + n,
                    "grade": "A",
                    "score": 85.0,
                    "cash_flow_door_month": 300.0 - n,
                    "coc": 0.2,
                    "dscr": 1.9,
                    "units": 4,
                    "rents": "stated",
                    "url": f"https://example.com/{n}",
                },
            )
            for n in range(1, count + 1)
        ]

    def test_layout(self, config):
        cut = Alert(
            kind="starred_price_cut",
            dedupe_key="starred_price_cut:9",
            property_id=9,
            payload={"address": "9 Star Ct", "price": 190_000.0, "was": 200_000.0, "cut_pct": 0.05},
        )
        later = Alert(
            kind="qualifies_after_cut",
            dedupe_key="qualifies_after_cut:8:0",
            property_id=8,
            payload={
                "address": "8 Cut Rd", "price": 180_000.0, "grade": "B", "score": 70.0,
                "cash_flow_door_month": 150.0, "units": 4, "was": 200_000.0, "cut_pct": 0.10,
            },
        )  # fmt: skip
        digest = build_digest([cut, *self.alerts(), later], config, TODAY)
        assert digest.subject == "Dealfinder: 4 things to look at" and digest.count == 4
        lines = digest.body.splitlines()
        assert lines[0] == "Dealfinder: 4 things to look at (Nov 13, 2026)"
        assert lines[2] == "New in your buy box"
        assert lines[3] == (
            "1. A 85 · $299/door/mo · CoC 20.0% · DSCR 1.90 · $200,001 · 4 units · rents stated"
        )
        assert lines[4] == "   101 Sample St, Columbus, OH, 43215"
        assert lines[5] == "   https://example.com/1"
        assert "Now qualifies after a price cut" in lines
        assert "   was $200,000, now $180,000 (-10.0%)" in lines
        assert "Price cuts on starred properties" in lines
        assert "1. 9 Star Ct: $200,000 -> $190,000 (-5.0%)" in lines
        assert lines[-1] == DISCLAIMER

    def test_one_thing_is_singular(self, config):
        assert build_digest(self.alerts(1), config, TODAY).subject == (
            "Dealfinder: 1 thing to look at"
        )

    def test_long_sections_are_cut_with_a_count(self, config):
        config.settings.alerts.max_deals_per_section = 3
        digest = build_digest(self.alerts(8), config, TODAY)
        assert "3. A 85" in digest.body and "4. A 85" not in digest.body
        assert "... and 5 more in the dashboard." in digest.body

    def test_the_chat_version_fits_one_message(self, config):
        config.settings.alerts.max_deals_per_section = 100
        digest = build_digest(self.alerts(60), config, TODAY)
        short = digest.short()
        assert len(digest.body) > DISCORD_LIMIT >= len(short)
        assert short.endswith("... the rest is in the dashboard.\n" + DISCLAIMER)
        assert build_digest(self.alerts(1), config, TODAY).short() == (
            build_digest(self.alerts(1), config, TODAY).body
        )


class TestChannels:
    def test_status(self, config):
        assert channel_status(config, environment()) == {
            "discord": "DISCORD_WEBHOOK_URL not set",
            "slack": "SLACK_WEBHOOK_URL not set",
            "email": "SMTP_HOST and ALERT_EMAIL_TO not set",
        }
        ready = environment(
            discord_webhook_url=DISCORD,
            smtp_host="smtp.example.com",
            alert_email_to="me@example.com",
        )
        config.settings.alerts.channels.slack = False
        assert channel_status(config, ready) == {
            "discord": "ready",
            "slack": "switched off in settings",
            "email": "ready",
        }

    def test_discord_and_slack_get_one_message_each(self, deals, config, online, http_mock):
        discord = http_mock.post(DISCORD).mock(return_value=httpx.Response(204))
        slack = http_mock.post(SLACK).mock(return_value=httpx.Response(200, text="ok"))
        env = environment(discord_webhook_url=DISCORD, slack_webhook_url=SLACK)
        report = send(deals, config, online, env)
        assert report.sent and report.channels == {
            "discord": "sent",
            "slack": "sent",
            "email": "SMTP_HOST and ALERT_EMAIL_TO not set",
        }
        assert discord.call_count == 1 and slack.call_count == 1
        content = json.loads(discord.calls[0].request.content)["content"]
        assert content.startswith("Dealfinder: ") and DISCLAIMER in content
        assert json.loads(slack.calls[0].request.content)["text"] == report.digest
        rows = stored(deals)
        assert rows and all(row.sent_at is not None for row in rows)
        assert {row.channel for row in rows} == {"discord,slack"}

    def test_email(self, deals, config, offline):
        sent = []

        class FakeSmtp:
            def __init__(self, host, port, timeout):
                self.where = (host, port)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self, context):
                sent.append("tls")

            def login(self, user, password):
                sent.append(("login", user, password))

            def send_message(self, message):
                sent.append(message)

        env = environment(
            smtp_host="smtp.example.com",
            smtp_username="me@example.com",
            smtp_password="app-password",
            alert_email_to="me@example.com",
        )
        report = send(deals, config, offline, env, smtp_factory=FakeSmtp)
        assert report.channels["email"] == "sent" and report.sent
        assert sent[0] == "tls" and sent[1] == ("login", "me@example.com", "app-password")
        message = sent[2]
        assert message["To"] == "me@example.com" and message["From"] == "me@example.com"
        assert message["Subject"].startswith("Dealfinder: ")
        assert DISCLAIMER in message.get_content()

    def test_a_mail_server_error_is_a_failed_channel(self, deals, config, offline):
        import smtplib

        def refuse(*args, **kwargs):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

        env = environment(smtp_host="smtp.example.com", alert_email_to="me@example.com")
        report = send(deals, config, offline, env, smtp_factory=refuse)
        assert report.channels["email"].startswith("failed: the mail server refused")
        assert not report.sent and "stay pending" in report.note

    def test_a_webhook_must_be_https(self, deals, config, online):
        env = environment(discord_webhook_url="http://discord.example/hook")
        report = send(deals, config, online, env)
        assert report.channels["discord"] == "failed: the webhook address must start with https://"

    def test_the_webhook_address_never_shows_in_an_error(self, deals, config, online, http_mock):
        http_mock.post(DISCORD).mock(return_value=httpx.Response(404))
        report = send(deals, config, online)
        assert report.channels["discord"] == "failed: HTTP 404"
        assert "webhooks" not in report.text()


class TestNeverTwice:
    def test_a_second_run_sends_nothing(self, deals, config, online, http_mock):
        route = http_mock.post(DISCORD).mock(return_value=httpx.Response(204))
        first = send(deals, config, online)
        second = send(deals, config, online)
        assert first.sent and first.alerts >= 1
        assert (
            second.alerts == 0 and not second.sent and second.text() == "Nothing new to alert on."
        )
        assert route.call_count == 1

    def test_a_failed_send_is_retried_and_then_not_repeated(self, deals, config, online, http_mock):
        route = http_mock.post(DISCORD).mock(return_value=httpx.Response(500))
        failed = send(deals, config, online)
        assert not failed.sent and failed.channels["discord"].startswith("failed:")
        assert all(row.sent_at is None for row in stored(deals))
        route.mock(return_value=httpx.Response(204))
        retried = send(deals, config, online)
        assert retried.sent and retried.alerts == failed.alerts
        assert len(stored(deals)) == failed.alerts  # the same rows, now marked sent
        assert send(deals, config, online).alerts == 0

    def test_no_channel_keeps_alerts_pending(self, deals, config, online):
        report = send(deals, config, online, environment())
        assert not report.sent and "No alert channel is set up" in report.note
        assert all(row.sent_at is None for row in stored(deals))
        assert len(due(deals, config)) == report.alerts

    def test_preview_stores_and_sends_nothing(self, deals, config, online):
        report = run_alerts(
            deals, config, environment(discord_webhook_url=DISCORD), online, send=False,
            today=lambda: TODAY,
        )  # fmt: skip
        assert report.alerts >= 1 and report.note == "Preview only. Add --send to send it."
        assert stored(deals) == [] and report.digest.startswith("Dealfinder: ")

    def test_a_deal_that_leaves_and_returns_is_news_again(
        self, deals, config, online, offline, http_mock
    ):
        http_mock.post(DISCORD).mock(return_value=httpx.Response(204))
        first = send(deals, config, online)
        told = {row.property_id for row in stored(deals)}
        assert 1 in told
        store(deals, fourplex(1, 900_000.0))  # the price jumps: no longer a deal
        funnel(deals, config, offline)
        assert send(deals, config, online).alerts == 0
        assert next(r for r in stored(deals) if r.property_id == 1).payload["lapsed"] is True
        store(deals, fourplex(1, 190_000.0))  # and comes back cheaper than when first told
        funnel(deals, config, offline)
        again = send(deals, config, online)
        assert again.alerts == 1 and again.by_kind == {"qualifies_after_cut": 1}
        keys = [row.dedupe_key for row in stored(deals) if row.property_id == 1]
        assert keys == ["new_deal:1:0", "qualifies_after_cut:1:1"]
        assert first.alerts >= 1

    def test_starred_cut_goes_out_once(self, deals, config, online, http_mock):
        route = http_mock.post(DISCORD).mock(return_value=httpx.Response(204))
        send(deals, config, online)
        data.set_star(deals, 6, True)
        store(deals, fourplex(6, 400_000.0))
        report = send(deals, config, online)
        assert report.by_kind == {"starred_price_cut": 1}
        assert send(deals, config, online).alerts == 0 and route.call_count == 2

    def test_switched_off(self, deals, config, online):
        config.settings.alerts.enabled = False
        report = send(deals, config, online)
        assert report.text() == "Alerts are switched off in settings (alerts.enabled)."


class TestCommand:
    def test_preview_and_send_without_a_channel(self, deals):
        runner = CliRunner()
        preview = runner.invoke(app, ["alerts"], env={"COLUMNS": "200"})
        assert preview.exit_code == 0, preview.output
        assert "New in your buy box" in preview.output
        assert "Preview only. Add --send to send it." in preview.output
        assert "discord: DISCORD_WEBHOOK_URL not set" in preview.output

        sent = runner.invoke(app, ["alerts", "--send"], env={"COLUMNS": "200"})
        assert sent.exit_code == 0, sent.output
        assert "No alert channel is set up" in sent.output

    def test_nothing_to_say(self, engine):
        result = CliRunner().invoke(app, ["alerts", "--send"])
        assert result.exit_code == 0 and "Nothing new to alert on." in result.output


def test_starring_records_the_moment(deals):
    data.set_star(deals, 1, True)
    with session_scope(deals) as session:
        place = session.get_one(PropertyRow, 1)
        assert place.starred and place.starred_at is not None
        assert place.starred_at - place.created_at >= timedelta(0)
    data.set_star(deals, 1, False)
    with session_scope(deals) as session:
        assert session.get_one(PropertyRow, 1).starred_at is None
