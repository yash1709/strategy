"""Alert formatting and delivery.

Alerts are first written to the ``alerts`` outbox table (inside the same
transaction as the crossover itself), then delivered. A channel that fails is
retried on the next run, so an alert is never lost to a network error.
"""
from __future__ import annotations

import logging
import smtplib
import sys
from abc import ABC, abstractmethod
from datetime import date
from email.message import EmailMessage

import requests

from .config import NotifyConfig
from .storage import Repository
from .strategy import Event

log = logging.getLogger(__name__)


def fmt_date(d: date | str | None) -> str:
    if d is None or d == "":
        return "-"
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d.strftime("%d-%b-%Y")


def fmt_inr(x: float | None) -> str:
    return "-" if x is None else f"₹{x:,.2f}"


def fmt_crore(x: float | None) -> str:
    return "-" if x is None else f"₹{x / 1e7:,.0f} Cr"


def fmt_int(x: float | None) -> str:
    return "-" if x is None else f"{int(x):,}"


# -- message templates --------------------------------------------------------
def crossover_message(ev: Event) -> tuple[str, str]:
    d = ev.data
    subject = f"SMA 50 crossed: {ev.symbol}"
    body = "\n".join([
        f"Stock: {ev.symbol}" + (f" ({ev.company})" if ev.company else ""),
        f"RSI Entry Date: {fmt_date(d['rsi_date'])}",
        f"RSI: {d['rsi_value']:.1f}",
        f"Crossover Date: {fmt_date(ev.trade_date)}",
        f"CMP: {fmt_inr(d['cmp'])}",
        f"SMA 50: {fmt_inr(d['sma50'])}",
        f"Trading Days Taken: {d['trading_days_taken']}",
        "Status: SMA 50 Crossed",
        f"{'AUM' if d.get('kind') == 'ETF' else 'Market Cap'}: {fmt_crore(d.get('market_cap'))}",
        f"Volume: {fmt_int(d.get('volume'))}",
    ])
    return subject, body


def summary_message(days: list[date], events: list[Event], active_count: int) -> tuple[str, str]:
    entered = [e for e in events if e.kind == "ENTERED"]
    crossed = [e for e in events if e.kind == "CROSSED"]
    closed = [e for e in events if e.kind == "CLOSED"]
    span = fmt_date(days[0]) if len(days) == 1 else f"{fmt_date(days[0])} to {fmt_date(days[-1])}"
    lines = [f"NSE RSI/SMA50 monitor - {span}",
             f"New RSI<30 entries: {len(entered)} | SMA50 crossovers: {len(crossed)} | "
             f"Closed (data/delisted): {len(closed)} | Active now: {active_count}"]
    if entered:
        lines.append("Entered: " + ", ".join(f"{e.symbol} ({e.data['rsi_value']:.1f})" for e in entered[:60])
                     + (" ..." if len(entered) > 60 else ""))
    if crossed:
        lines.append("Crossed: " + ", ".join(f"{e.symbol} ({e.data['trading_days_taken']}d)" for e in crossed))
    if closed:
        lines.append("Closed: " + ", ".join(f"{e.symbol} [{e.data['status']}]" for e in closed))
    return f"NSE monitor summary {fmt_date(days[-1])}", "\n".join(lines)


# -- channels ---------------------------------------------------------------
class Notifier(ABC):
    channel: str

    @abstractmethod
    def send(self, subject: str, body: str) -> None:
        """Deliver or raise."""


class ConsoleNotifier(Notifier):
    channel = "console"

    def send(self, subject: str, body: str) -> None:
        bar = "=" * 48
        text = f"\n{bar}\n{subject}\n{bar}\n{body}\n"
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            print(text.encode(sys.stdout.encoding or "ascii", "replace").decode(sys.stdout.encoding or "ascii"))


class TelegramNotifier(Notifier):
    channel = "telegram"

    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id

    def send(self, subject: str, body: str) -> None:
        resp = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                             json={"chat_id": self.chat_id, "text": f"{subject}\n\n{body}"}, timeout=20)
        resp.raise_for_status()


class WebhookNotifier(Notifier):
    """Generic JSON webhook. Sends both ``text`` (Slack/Teams/Mattermost) and
    ``content`` (Discord) keys so common services accept it as-is."""
    channel = "webhook"

    def __init__(self, url: str):
        self.url = url

    def send(self, subject: str, body: str) -> None:
        text = f"*{subject}*\n{body}"
        resp = requests.post(self.url, json={"text": text, "content": text[:1900]}, timeout=20)
        resp.raise_for_status()


class EmailNotifier(Notifier):
    channel = "email"

    def __init__(self, cfg: NotifyConfig):
        self.cfg = cfg

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.cfg.email_from, ", ".join(self.cfg.email_to)
        msg.set_content(body)
        with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as smtp:
            if self.cfg.smtp_use_tls:
                smtp.starttls()
            if self.cfg.smtp_user:
                smtp.login(self.cfg.smtp_user, self.cfg.smtp_password)
            smtp.send_message(msg)


def build_notifiers(cfg: NotifyConfig) -> list[Notifier]:
    out: list[Notifier] = []
    if cfg.console:
        out.append(ConsoleNotifier())
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        out.append(TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id))
    if cfg.webhook_url:
        out.append(WebhookNotifier(cfg.webhook_url))
    if cfg.smtp_host and cfg.email_to:
        out.append(EmailNotifier(cfg))
    return out


def dispatch_pending(repo: Repository, notifiers: list[Notifier], max_attempts: int) -> tuple[int, int]:
    """Send every undelivered alert whose channel is configured. Returns (sent, failed)."""
    by_channel = {n.channel: n for n in notifiers}
    sent = failed = 0
    for alert in repo.pending_alerts(max_attempts):
        notifier = by_channel.get(alert["channel"])
        if notifier is None:
            continue  # channel was disabled since the alert was queued
        try:
            notifier.send(alert["subject"], alert["body"])
            repo.mark_alert(alert["id"], True)
            sent += 1
        except Exception as exc:
            log.error("Alert %s via %s failed: %s", alert["id"], alert["channel"], exc)
            repo.mark_alert(alert["id"], False, str(exc)[:500])
            failed += 1
    return sent, failed
