"""
Legacy schedule API, now a thin facade over the configurable state machine in
``state_machine.py``. Existing callers keep working; new code should use
``get_schedule_state()`` and read the fields it needs from one resolved state.
"""

import os
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, NamedTuple, Optional

import state_machine
from state_machine import (
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    ConfigStore,
    ResolvedState,
)

__all__ = [
    "MAX_POLL_INTERVAL",
    "MIN_POLL_INTERVAL",
    "STORE",
    "CommuteLighting",
    "_parse_hour_env",
    "get_commute_lighting",
    "get_policy_header",
    "get_presentation",
    "get_schedule_report",
    "get_schedule_state",
    "get_status_note",
    "get_target_poll_interval",
    "is_overnight_hours",
    "is_peak_commute_hours",
]


class CommuteLighting(NamedTuple):
    brightness: int
    warmth: int


def _parse_hour_env(name: str, default: float) -> float:
    """Parses a decimal-hour env var (e.g. '9.5' or '10'), falling back to
    default on any error."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# The process-wide config store. Tests swap it with patch.object(schedule,
# "STORE", ConfigStore(...)); every accessor below reads it at call time.
STORE = ConfigStore()


def get_schedule_state(dt: Optional[datetime] = None) -> ResolvedState:
    """Resolves the full schedule state (phase + parameters) at ``dt``."""
    return STORE.resolve(dt)


def _unforced(dt: Optional[datetime]) -> ResolvedState:
    """Resolves ignoring FORCE_FAST_POLL (which never affected these)."""
    cfg = STORE.get()
    if cfg.force_fast_poll:
        cfg = replace(cfg, force_fast_poll=False)
    return state_machine.resolve(cfg, dt)


def is_peak_commute_hours(dt: Optional[datetime] = None) -> bool:
    """
    True while the active phase is interactive (by default the weekday peak
    commute windows 07:30-09:30 and 16:30-19:00).
    """
    return _unforced(dt).presentation == "interactive"


def is_overnight_hours(dt: Optional[datetime] = None) -> bool:
    """True while the active phase is dormant (by default 22:00-06:00)."""
    return _unforced(dt).presentation == "dormant"


def get_commute_lighting(dt: Optional[datetime] = None) -> CommuteLighting:
    """Returns (brightness, warmth) configured for the active phase."""
    light = _unforced(dt).lighting
    return CommuteLighting(light.brightness, light.warmth)


def get_presentation(dt: Optional[datetime] = None) -> str:
    """
    Returns the client-facing presentation state of the active phase:
    "interactive", "idle" or "dormant". FORCE_FAST_POLL=1 forces "interactive".
    A client may still override to "interactive" while it is awake in a
    power-button interaction session (see the ?present= param).
    """
    return get_schedule_state(dt).presentation


def get_status_note(presentation: str, dt: Optional[datetime] = None) -> str:
    """Bottom-strip label for a non-interactive presentation (idle/dormant)."""
    if dt is None:
        dt = datetime.now()
    return state_machine.phase_note(STORE.get(), presentation, dt)


def get_target_poll_interval(dt: Optional[datetime] = None) -> int:
    """Returns the poll interval (seconds) configured for the active phase."""
    return get_schedule_state(dt).poll_interval


def get_policy_header(state: ResolvedState) -> str:
    """The X-Tracker-Policy value for ``state`` under the active config."""
    return state_machine.format_policy_header(state, STORE.get())


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt is not None else None


def get_schedule_report(
    dt: Optional[datetime] = None, horizon: timedelta = timedelta(hours=24)
) -> dict[str, Any]:
    """
    Debug view for GET /schedule: config source and any load error, the
    current resolved state, upcoming transitions and the effective config.
    """
    if dt is None:
        dt = datetime.now()
    cfg = STORE.get()
    state = state_machine.resolve(cfg, dt)
    return {
        "source": STORE.source,
        "path": STORE.path,
        "error": STORE.error or None,
        "now": _iso(dt),
        "current": {
            "phase": state.phase,
            "presentation": state.presentation,
            "status_note": state.status_note,
            "poll_interval": state.poll_interval,
            "lighting": {
                "brightness": state.lighting.brightness,
                "warmth": state.lighting.warmth,
            },
            "realtime": state.realtime,
            "suspend": state.suspend,
            "view": state.view,
            "until": _iso(state.until),
        },
        "transitions": [
            {"at": _iso(t), "phase": name}
            for t, name in state_machine.upcoming_transitions(cfg, dt, horizon)
        ],
        "config": cfg.to_dict(),
        "overrides": {
            "force_phase": cfg.force_phase or None,
            "force_fast_poll": cfg.force_fast_poll,
        },
    }
