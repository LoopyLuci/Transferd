"""Mapping from the model's states to the words and tones the views show. One place decides what each state looks like."""

from __future__ import annotations

from typing import Tuple

from transferd_gui.model.fmt import NO_DATA, clock, human_age
from transferd_gui.model.snapshot import RelaySnapshot, SessionSnapshot


def relay_status(relay: RelaySnapshot) -> Tuple[str, str]:
    """The word and tone for the relay's state."""
    if not relay.configured and relay.state == "off":
        return "not configured", "idle"
    labels = {
        "off": ("not connected", "idle"),
        "connecting": ("connecting", "accent"),
        "registered": ("registered", "ok"),
        "failed": ("failed", "error"),
    }
    return labels.get(relay.state, (relay.state, "idle"))


def session_status(session: SessionSnapshot) -> Tuple[str, str]:
    labels = {
        "negotiating": ("negotiating", "accent"),
        "ready": ("ready", "ok"),
        "no route": ("no live route", "warn"),
        "closed": ("closed", "idle"),
    }
    return labels.get(session.state, (session.state, "idle"))


def route_status(up: bool, closed: bool) -> Tuple[str, str]:
    if closed:
        return "closed", "idle"
    return ("up", "ok") if up else ("down", "error")


def relay_next_text(relay: RelaySnapshot, now: float) -> str:
    """When the next registration renewal is due, or the no-data text."""
    if relay.next_registration_at is None:
        return NO_DATA
    remaining = relay.next_registration_at - now
    if remaining <= 0:
        return "due now"
    return f"in {human_age(remaining)}"


def time_text(when: float | None) -> str:
    return NO_DATA if when is None else clock(when)
