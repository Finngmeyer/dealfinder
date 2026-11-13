"""Alerts: what is worth telling the owner about, as one digest per run.

Three things raise an alert:

* a deal inside the buy box with a grade worth alerting on (A or B by default) that has not
  been alerted before, or that dropped out and is back;
* the same, when it qualifies now because its price was cut;
* a starred property whose price fell by at least ``alerts.starred_price_cut_pct`` in one
  step, counted from the moment it was starred.

Every alert has a key stored in the ``alerts`` table. Once a digest carrying it has gone out
on at least one channel it is marked sent and never goes out again. If every channel fails,
it stays pending and rides in the next digest.

Channels: a Discord webhook, a Slack webhook, or email over SMTP. A channel without its
settings in ``.env`` is skipped and says so.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from datetime import date
from email.message import EmailMessage
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from dealfinder.analysis import DISCLAIMER
from dealfinder.http import Fetcher, SourceUnavailable
from dealfinder.pipeline.report import DealRow, deal_rows
from dealfinder.settings import Config, Environment
from dealfinder.store.db import session_scope
from dealfinder.store.tables import AlertRow, ListingRow, PriceEventRow, PropertyRow, utcnow
from dealfinder.text import short_date

DEAL_KINDS = ("new_deal", "qualifies_after_cut")
DISCORD_LIMIT = 2000  # characters in one Discord message
SECTIONS = {
    "new_deal": "New in your buy box",
    "qualifies_after_cut": "Now qualifies after a price cut",
    "starred_price_cut": "Price cuts on starred properties",
}


class Alert(BaseModel):
    kind: str
    dedupe_key: str
    property_id: int
    payload: dict[str, Any]


def _money(amount: float | None) -> str:
    if amount is None:
        return "n/a"
    return f"-${-amount:,.0f}" if amount < 0 else f"${amount:,.0f}"


def _deal_payload(row: DealRow, **extra: Any) -> dict[str, Any]:
    return {
        "address": row.full_address,
        "price": row.price,
        "grade": row.grade,
        "score": row.score,
        "cash_flow_door_month": row.cash_flow_door_month,
        "coc": row.coc,
        "dscr": row.dscr,
        "units": row.units,
        "rents": "stated" if row.rents_estimated == 0 and row.rents_stated else "estimated",
        "url": row.url,
        "scenario": row.scenario,
        "listing_id": row.listing_id,
        **extra,
    }


def _first_price(session: Session, listing_id: int) -> float | None:
    return session.scalar(
        select(PriceEventRow.price)
        .where(PriceEventRow.listing_id == listing_id, PriceEventRow.price.is_not(None))
        .order_by(PriceEventRow.id)
        .limit(1)
    )


def _sent_deal_alerts():  # type: ignore[no-untyped-def]
    return (
        select(AlertRow)
        .where(AlertRow.kind.in_(DEAL_KINDS), AlertRow.sent_at.is_not(None))
        .order_by(AlertRow.id)
    )


def find_alerts(session: Session, config: Config) -> list[Alert]:
    """Alerts that are due now and have not been sent. Nothing is stored."""
    rule = config.settings.alerts
    # Only alerts that actually went out count as "already told". One that was recorded but
    # could not be sent is still due, under the same key.
    history: dict[int, list[AlertRow]] = {}
    for row in session.scalars(_sent_deal_alerts()):
        history.setdefault(row.property_id, []).append(row)
    sent_keys = set(
        session.scalars(select(AlertRow.dedupe_key).where(AlertRow.sent_at.is_not(None)))
    )

    found: list[Alert] = []
    for row in deal_rows(session, config):
        if not (row.in_buy_box and row.type_match == "yes" and row.grade in rule.grades):
            continue
        earlier = history.get(row.property_id, [])
        last = earlier[-1] if earlier else None
        if last is not None and not last.payload.get("lapsed"):
            continue  # already told, and it has qualified ever since
        first = _first_price(session, row.listing_id)
        was = (last.payload.get("price") if last is not None else None) or first
        cut = bool(was and row.price and row.price < was)
        kind = "qualifies_after_cut" if cut else "new_deal"
        extra = {"was": was, "cut_pct": 1 - row.price / was} if cut and row.price and was else {}
        if last is not None and not cut:
            extra["note"] = "back in the buy box"
        found.append(
            Alert(
                kind=kind,
                dedupe_key=f"{kind}:{row.property_id}:{len(earlier)}",
                property_id=row.property_id,
                payload=_deal_payload(row, **extra),
            )
        )

    cuts = session.execute(
        select(PriceEventRow, ListingRow, PropertyRow)
        .join(ListingRow, ListingRow.id == PriceEventRow.listing_id)
        .join(PropertyRow, PropertyRow.id == ListingRow.property_id)
        .where(
            PropertyRow.starred.is_(True),
            PropertyRow.hidden.is_(False),
            PriceEventRow.event == "price_change",
            PriceEventRow.previous_price.is_not(None),
            PriceEventRow.price.is_not(None),
        )
        .order_by(PriceEventRow.id)
    )
    for event, listing, place in cuts:
        if place.starred_at is not None and event.observed_at < place.starred_at:
            continue  # the cut came before the owner starred it
        change = 1 - event.price / event.previous_price
        if change < rule.starred_price_cut_pct:
            continue
        found.append(
            Alert(
                kind="starred_price_cut",
                dedupe_key=f"starred_price_cut:{event.id}",
                property_id=place.id,
                payload={
                    "address": ", ".join(
                        p
                        for p in (place.address_line1, place.city, place.state, place.zip_code)
                        if p
                    ),
                    "price": event.price,
                    "was": event.previous_price,
                    "cut_pct": change,
                    "url": listing.url,
                    "listing_id": listing.id,
                },
            )
        )
    return [alert for alert in found if alert.dedupe_key not in sent_keys]


def mark_lapsed(session: Session, config: Config) -> int:
    """Note which properties alerted earlier no longer qualify, so that a later return to the
    buy box counts as news again. Returns how many were marked."""
    rule = config.settings.alerts
    qualifying = {
        row.property_id
        for row in deal_rows(session, config)
        if row.in_buy_box and row.type_match == "yes" and row.grade in rule.grades
    }
    latest: dict[int, AlertRow] = {}
    for row in session.scalars(_sent_deal_alerts()):
        latest[row.property_id] = row
    marked = 0
    for property_id, row in latest.items():
        if (
            property_id not in qualifying
            and row.sent_at is not None
            and not row.payload.get("lapsed")
        ):
            row.payload = {**row.payload, "lapsed": True}
            marked += 1
    return marked


# ---- the digest ----------------------------------------------------------------------------------


def _deal_lines(payload: dict[str, Any]) -> list[str]:
    facts = [
        f"{payload.get('grade') or '-'} {payload['score']:.0f}" if payload.get("score") is not None
        else str(payload.get("grade") or "-"),
        f"{_money(payload.get('cash_flow_door_month'))}/door/mo",
    ]  # fmt: skip
    if payload.get("coc") is not None:
        facts.append(f"CoC {payload['coc']:.1%}")
    if payload.get("dscr") is not None:
        facts.append(f"DSCR {payload['dscr']:.2f}")
    facts.append(_money(payload.get("price")))
    if payload.get("units"):
        facts.append(f"{payload['units']} units")
    facts.append(f"rents {payload.get('rents', 'estimated')}")
    lines = [" · ".join(facts), f"   {payload.get('address', '')}"]
    if payload.get("was") and payload.get("cut_pct"):
        lines.append(
            f"   was {_money(payload['was'])}, now {_money(payload.get('price'))} "
            f"(-{payload['cut_pct']:.1%})"
        )
    if payload.get("note"):
        lines.append(f"   {payload['note']}")
    if payload.get("url"):
        lines.append(f"   {payload['url']}")
    return lines


def _cut_lines(payload: dict[str, Any]) -> list[str]:
    lines = [
        f"{payload.get('address', '')}: {_money(payload.get('was'))} -> "
        f"{_money(payload.get('price'))} (-{payload.get('cut_pct', 0):.1%})"
    ]
    if payload.get("url"):
        lines.append(f"   {payload['url']}")
    return lines


class Digest(BaseModel):
    subject: str
    body: str
    count: int

    def short(self, limit: int = DISCORD_LIMIT) -> str:
        """The body cut to fit one chat message, ending on a whole line."""
        if len(self.body) <= limit:
            return self.body
        tail = "\n... the rest is in the dashboard.\n" + DISCLAIMER
        kept = self.body[: limit - len(tail)]
        return kept[: kept.rfind("\n")] + tail


def build_digest(alerts: list[Alert], config: Config, today: date) -> Digest:
    """One message for every alert, grouped by kind, best cash flow first."""
    cap = config.settings.alerts.max_deals_per_section
    count = len(alerts)
    noun = "thing" if count == 1 else "things"
    lines = [f"Dealfinder: {count} {noun} to look at ({short_date(today)})"]
    for kind, heading in SECTIONS.items():
        group = [alert for alert in alerts if alert.kind == kind]
        if not group:
            continue
        if kind != "starred_price_cut":
            group.sort(key=lambda alert: -(alert.payload.get("cash_flow_door_month") or 0.0))
        lines += ["", heading]
        for number, alert in enumerate(group[:cap], start=1):
            detail = _cut_lines if kind == "starred_price_cut" else _deal_lines
            first, *rest = detail(alert.payload)
            lines.append(f"{number}. {first}")
            lines.extend(rest)
        if len(group) > cap:
            lines.append(f"... and {len(group) - cap} more in the dashboard.")
    lines += ["", DISCLAIMER]
    return Digest(
        subject=f"Dealfinder: {count} {noun} to look at", body="\n".join(lines), count=count
    )


# ---- channels ------------------------------------------------------------------------------------


def channel_status(config: Config, environment: Environment) -> dict[str, str]:
    """Each channel and either "ready" or why it will be skipped."""
    switches = config.settings.alerts.channels
    needs = {
        "discord": {"DISCORD_WEBHOOK_URL": environment.discord_webhook_url},
        "slack": {"SLACK_WEBHOOK_URL": environment.slack_webhook_url},
        "email": {
            "SMTP_HOST": environment.smtp_host,
            "ALERT_EMAIL_TO": environment.alert_email_to,
        },
    }
    status: dict[str, str] = {}
    for name, required in needs.items():
        missing = [key for key, present in required.items() if not present]
        if not getattr(switches, name):
            status[name] = "switched off in settings"
        elif missing:
            status[name] = f"{' and '.join(missing)} not set"
        else:
            status[name] = "ready"
    return status


def _webhook(fetcher: Fetcher, source: str, secret: Any, payload: dict[str, str]) -> None:
    url = secret.get_secret_value()
    if not url.lower().startswith("https://"):
        raise SourceUnavailable(source, "the webhook address must start with https://")
    fetcher.post_json(source, url, payload, timeout_seconds=30)


def _email(environment: Environment, digest: Digest, smtp_factory: Callable[..., Any]) -> None:
    message = EmailMessage()
    message["Subject"] = digest.subject
    sender = environment.alert_email_from or environment.smtp_username or environment.alert_email_to
    message["From"] = sender
    message["To"] = environment.alert_email_to
    message.set_content(digest.body)
    try:
        with smtp_factory(environment.smtp_host, environment.smtp_port, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            if environment.smtp_username and environment.smtp_password:
                server.login(
                    environment.smtp_username, environment.smtp_password.get_secret_value()
                )
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as error:
        reason = type(error).__name__ if not str(error) else f"{type(error).__name__}: {error}"
        raise SourceUnavailable("email", f"the mail server refused: {reason}"[:200]) from error


def send_digest(
    digest: Digest,
    config: Config,
    environment: Environment,
    fetcher: Fetcher,
    smtp_factory: Callable[..., Any] = smtplib.SMTP,
) -> dict[str, str]:
    """Send on every ready channel. Returns channel -> "sent" or the reason it was not."""
    results: dict[str, str] = {}
    for name, state in channel_status(config, environment).items():
        if state != "ready":
            results[name] = state
            continue
        try:
            if name == "discord":
                _webhook(
                    fetcher, name, environment.discord_webhook_url, {"content": digest.short()}
                )
            elif name == "slack":
                _webhook(fetcher, name, environment.slack_webhook_url, {"text": digest.body})
            else:
                _email(environment, digest, smtp_factory)
        except SourceUnavailable as problem:
            results[name] = f"failed: {problem.reason}"
        else:
            results[name] = "sent"
    return results


# ---- the run -------------------------------------------------------------------------------------


class AlertReport(BaseModel):
    alerts: int = 0
    by_kind: dict[str, int] = Field(default_factory=dict)
    digest: str = ""
    sent: bool = False
    channels: dict[str, str] = Field(default_factory=dict)
    note: str = ""

    def text(self) -> str:
        if not self.alerts:
            return self.note or "Nothing new to alert on."
        lines = [self.digest, ""]
        lines.extend(f"{name}: {state}" for name, state in self.channels.items())
        if self.note:
            lines.append(self.note)
        return "\n".join(lines)


def run_alerts(
    engine: Engine,
    config: Config,
    environment: Environment,
    fetcher: Fetcher,
    *,
    send: bool,
    run_id: int | None = None,
    today: Callable[[], date] = date.today,
    smtp_factory: Callable[..., Any] = smtplib.SMTP,
) -> AlertReport:
    """Work out what is due and, with ``send``, send one digest and record it.

    Without ``send`` this is a preview: nothing is stored and nothing goes out.
    """
    report = AlertReport()
    if not config.settings.alerts.enabled:
        report.note = "Alerts are switched off in settings (alerts.enabled)."
        return report
    with session_scope(engine) as session:
        if send:
            mark_lapsed(session, config)
        due = find_alerts(session, config)
    report.alerts = len(due)
    for alert in due:
        report.by_kind[alert.kind] = report.by_kind.get(alert.kind, 0) + 1
    report.channels = channel_status(config, environment)
    if not due:
        return report
    digest = build_digest(due, config, today())
    report.digest = digest.body
    if not send:
        report.note = "Preview only. Add --send to send it."
        return report

    with session_scope(engine) as session:  # record first, so a crash cannot cause a repeat
        waiting = {
            row.dedupe_key: row
            for row in session.scalars(select(AlertRow).where(AlertRow.sent_at.is_(None)))
        }
        due_keys = {alert.dedupe_key for alert in due}
        for key, row in waiting.items():
            if key not in due_keys:
                session.delete(row)  # recorded earlier, never sent, and no longer true
        for alert in due:
            row = waiting.get(alert.dedupe_key)
            if row is None:
                session.add(
                    AlertRow(
                        run_id=run_id,
                        property_id=alert.property_id,
                        kind=alert.kind,
                        dedupe_key=alert.dedupe_key,
                        payload=alert.payload,
                    )
                )
            else:
                row.payload = alert.payload  # the figures may have moved since it was recorded
    report.channels = send_digest(digest, config, environment, fetcher, smtp_factory)
    delivered = [name for name, state in report.channels.items() if state == "sent"]
    if delivered:
        report.sent = True
        with session_scope(engine) as session:
            keys = [alert.dedupe_key for alert in due]
            for row in session.scalars(select(AlertRow).where(AlertRow.dedupe_key.in_(keys))):
                row.sent_at = utcnow()
                row.channel = ",".join(delivered)
    elif all(
        state != "ready" and not state.startswith("failed") for state in report.channels.values()
    ):
        report.note = (
            "No alert channel is set up, so nothing was sent. Add DISCORD_WEBHOOK_URL, "
            "SLACK_WEBHOOK_URL or the SMTP settings to .env. These alerts stay pending."
        )
    else:
        report.note = "Every channel failed, so these alerts stay pending for the next run."
    return report
