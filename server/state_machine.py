"""
Configurable schedule state machine (layer 1 of the schedule/interaction model).

The day is divided into named *phases* (by default ``peak``, ``offpeak`` and
``overnight``). Ordered time *windows* select the active phase; outside every
window ``default_phase`` applies. Each phase carries the behaviour that used to
be spread across ``schedule.py`` if-chains: poll interval, presentation, status
strip text, frontlight, realtime feeds and whether the client may suspend.

Configuration precedence (lowest to highest):

1. Built-in defaults (identical to the historical hardcoded schedule).
2. ``schedule.json`` (path from ``$SCHEDULE_CONFIG``), deep-merged by phase.
3. Legacy env vars (``PEAK_AM_START`` ... ``OFFPEAK_INTERVAL``), kept for
   backward compatibility. Empty values are treated as unset.

Validation is strict and fails safe: an invalid file is rejected as a whole and
the last good configuration (or the built-in defaults) stays in effect.

The client-side interaction overlay (idle -> session -> hold) is parameterised
by the ``interaction`` section and shipped to clients via the signed
``X-Tracker-Policy`` header.
"""

import json
import math
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Optional

CONFIG_VERSION = 1
DEFAULT_CONFIG_PATH = "/app/config/schedule.json"
MAX_CONFIG_BYTES = 64 * 1024

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PRESENTATIONS = ("interactive", "idle", "dormant")
VIEWS = ("morning", "evening")

# Poll intervals advertised to clients: a 0 or negative value would make
# clients spin, a huge one would freeze the board.
MIN_POLL_INTERVAL = 30
MAX_POLL_INTERVAL = 7200
LIGHT_MAX = 24
MAX_PHASES = 16
MAX_WINDOWS = 64
MAX_NOTE_LEN = 120

# Interaction overlay bounds (seconds).
SESSION_TIMEOUT_RANGE = (10, 1800)
FAST_POLL_HOLD_RANGE = (0, 7200)
HOLD_RANGE = (0, 14400)

# How far ahead the next phase transition is searched for.
TRANSITION_HORIZON_DAYS = 8

POLICY_VERSION = 1

_PHASE_NAME_RE = re.compile(r"\A[a-z][a-z0-9_-]{0,23}\Z")
_TIME_RE = re.compile(r"\A([01]\d|2[0-4]):([0-5]\d)\Z")


class ConfigError(ValueError):
    """Raised when a schedule configuration is invalid."""


@dataclass(frozen=True)
class Lighting:
    brightness: int = 0
    warmth: int = 0


@dataclass(frozen=True)
class PhaseConfig:
    name: str
    poll_interval: int = 600
    presentation: str = "idle"
    status_note: str = ""
    lighting: Lighting = field(default_factory=Lighting)
    realtime: bool = True
    suspend: bool = True


@dataclass(frozen=True)
class Window:
    """
    A time window selecting ``phase``. Times are minutes since midnight; a
    window with ``start > end`` wraps past midnight and belongs to the day it
    starts on. ``days`` holds ``datetime.weekday()`` values.
    """

    phase: str
    start: int
    end: int
    days: frozenset[int] = frozenset(range(7))
    view: Optional[str] = None

    def matches(self, dt: datetime) -> bool:
        minute = dt.hour * 60 + dt.minute
        weekday = dt.weekday()
        if self.start < self.end:
            return weekday in self.days and self.start <= minute < self.end
        # Wrapping window: the evening part belongs to today, the early-morning
        # part to the day before.
        if weekday in self.days and minute >= self.start:
            return True
        return (weekday - 1) % 7 in self.days and minute < self.end


@dataclass(frozen=True)
class InteractionConfig:
    session_timeout: int = 90
    session_lighting: Lighting = field(default_factory=lambda: Lighting(8, 12))
    fast_poll_hold: int = 600
    hold: int = 2700


@dataclass(frozen=True)
class ScheduleConfig:
    phases: Mapping[str, PhaseConfig]
    windows: tuple[Window, ...]
    default_phase: str
    interaction: InteractionConfig = field(default_factory=InteractionConfig)
    force_phase: str = ""
    force_fast_poll: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view, in the same shape as schedule.json."""
        return {
            "version": CONFIG_VERSION,
            "default_phase": self.default_phase,
            "phases": {
                name: {
                    "poll_interval": p.poll_interval,
                    "presentation": p.presentation,
                    "status_note": p.status_note,
                    "lighting": {
                        "brightness": p.lighting.brightness,
                        "warmth": p.lighting.warmth,
                    },
                    "realtime": p.realtime,
                    "suspend": p.suspend,
                }
                for name, p in self.phases.items()
            },
            "windows": [_window_to_dict(w) for w in self.windows],
            "interaction": {
                "session_timeout": self.interaction.session_timeout,
                "session_lighting": {
                    "brightness": self.interaction.session_lighting.brightness,
                    "warmth": self.interaction.session_lighting.warmth,
                },
                "fast_poll_hold": self.interaction.fast_poll_hold,
                "hold": self.interaction.hold,
            },
        }


@dataclass(frozen=True)
class ResolvedState:
    """The effective schedule state at an instant."""

    phase: str
    poll_interval: int
    presentation: str
    status_note: str
    lighting: Lighting
    realtime: bool
    suspend: bool
    view: Optional[str]
    until: Optional[datetime]


def _fmt_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _window_to_dict(w: Window) -> dict[str, Any]:
    out: dict[str, Any] = {
        "phase": w.phase,
        "start": _fmt_minutes(w.start),
        "end": _fmt_minutes(w.end),
        "days": [DAY_NAMES[d] for d in sorted(w.days)],
    }
    if w.view:
        out["view"] = w.view
    return out


# ---------------------------------------------------------------------------
# Built-in defaults (the historical hardcoded schedule)
# ---------------------------------------------------------------------------

WEEKDAYS = frozenset(range(5))
ALL_DAYS = frozenset(range(7))


def _default_phases() -> dict[str, PhaseConfig]:
    return {
        "peak": PhaseConfig(
            name="peak",
            poll_interval=60,
            presentation="interactive",
            lighting=Lighting(8, 12),
            realtime=True,
            suspend=False,
        ),
        "offpeak": PhaseConfig(
            name="offpeak",
            poll_interval=600,
            presentation="idle",
            status_note="PRESS POWER BUTTON TO INTERACT",
            realtime=True,
            suspend=True,
        ),
        "overnight": PhaseConfig(
            name="overnight",
            poll_interval=3600,
            presentation="dormant",
            status_note="SLEEPING — back at {until} · press power to interact",
            realtime=False,
            suspend=True,
        ),
    }


def _default_windows() -> tuple[Window, ...]:
    return (
        Window("peak", 450, 570, WEEKDAYS, "morning"),  # 07:30-09:30
        Window("peak", 990, 1140, WEEKDAYS, "evening"),  # 16:30-19:00
        Window("overnight", 1320, 360, ALL_DAYS),  # 22:00-06:00
    )


def default_config() -> ScheduleConfig:
    return ScheduleConfig(
        phases=_default_phases(),
        windows=_default_windows(),
        default_phase="offpeak",
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _check_keys(obj: Mapping[str, Any], allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(set(obj) - set(allowed))
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _as_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected an object")
    return value


def _as_int(value: Any, lo: int, hi: int, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected an integer")
    if not lo <= value <= hi:
        raise ConfigError(f"{where}: {value} is outside {lo}..{hi}")
    return value


def _as_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false")
    return value


def _as_choice(value: Any, choices: tuple[str, ...], where: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ConfigError(f"{where}: expected one of {', '.join(choices)}")
    return value


def _as_note(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a string")
    if len(value) > MAX_NOTE_LEN or any(ord(c) < 32 for c in value):
        raise ConfigError(
            f"{where}: must be at most {MAX_NOTE_LEN} printable characters"
        )
    return value


def _parse_time(value: Any, where: str) -> int:
    if not isinstance(value, str):
        raise ConfigError(f'{where}: expected "HH:MM"')
    m = _TIME_RE.match(value)
    if not m:
        raise ConfigError(f'{where}: expected "HH:MM" (00:00-24:00)')
    minutes = int(m.group(1)) * 60 + int(m.group(2))
    if minutes > 1440:
        raise ConfigError(f"{where}: {value} is past 24:00")
    return minutes


def _parse_lighting(value: Any, base: Lighting, where: str) -> Lighting:
    obj = _as_dict(value, where)
    _check_keys(obj, ("brightness", "warmth"), where)
    return Lighting(
        brightness=_as_int(
            obj.get("brightness", base.brightness), 0, LIGHT_MAX, f"{where}.brightness"
        ),
        warmth=_as_int(obj.get("warmth", base.warmth), 0, LIGHT_MAX, f"{where}.warmth"),
    )


_PHASE_KEYS = (
    "poll_interval",
    "presentation",
    "status_note",
    "lighting",
    "realtime",
    "suspend",
)


def _parse_phase(name: str, value: Any, base: Optional[PhaseConfig]) -> PhaseConfig:
    where = f"phases.{name}"
    if not _PHASE_NAME_RE.match(name):
        raise ConfigError(f"{where}: phase names must match [a-z][a-z0-9_-]{{0,23}}")
    obj = _as_dict(value, where)
    _check_keys(obj, _PHASE_KEYS, where)
    p = base if base is not None else PhaseConfig(name=name)
    return PhaseConfig(
        name=name,
        poll_interval=_as_int(
            obj.get("poll_interval", p.poll_interval),
            MIN_POLL_INTERVAL,
            MAX_POLL_INTERVAL,
            f"{where}.poll_interval",
        ),
        presentation=_as_choice(
            obj.get("presentation", p.presentation),
            PRESENTATIONS,
            f"{where}.presentation",
        ),
        status_note=_as_note(
            obj.get("status_note", p.status_note), f"{where}.status_note"
        ),
        lighting=(
            _parse_lighting(obj["lighting"], p.lighting, f"{where}.lighting")
            if "lighting" in obj
            else p.lighting
        ),
        realtime=_as_bool(obj.get("realtime", p.realtime), f"{where}.realtime"),
        suspend=_as_bool(obj.get("suspend", p.suspend), f"{where}.suspend"),
    )


def _parse_days(value: Any, where: str) -> frozenset[int]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}: expected a non-empty list of day names")
    days = set()
    for i, d in enumerate(value):
        days.add(DAY_NAMES.index(_as_choice(d, DAY_NAMES, f"{where}[{i}]")))
    return frozenset(days)


def _parse_window(index: int, value: Any) -> Window:
    where = f"windows[{index}]"
    obj = _as_dict(value, where)
    _check_keys(obj, ("phase", "start", "end", "days", "view"), where)
    for key in ("phase", "start", "end"):
        if key not in obj:
            raise ConfigError(f"{where}: missing required key {key}")
    if not isinstance(obj["phase"], str):
        raise ConfigError(f"{where}.phase: expected a phase name")
    start = _parse_time(obj["start"], f"{where}.start")
    end = _parse_time(obj["end"], f"{where}.end")
    if start == end or start == 1440:
        raise ConfigError(f"{where}: start and end must differ (and start < 24:00)")
    if end == 0:
        end = 1440
    days = _parse_days(obj["days"], f"{where}.days") if "days" in obj else ALL_DAYS
    view = _as_choice(obj["view"], VIEWS, f"{where}.view") if "view" in obj else None
    return Window(obj["phase"], start, end, days, view)


def _parse_interaction(value: Any, base: InteractionConfig) -> InteractionConfig:
    where = "interaction"
    obj = _as_dict(value, where)
    _check_keys(
        obj, ("session_timeout", "session_lighting", "fast_poll_hold", "hold"), where
    )
    return InteractionConfig(
        session_timeout=_as_int(
            obj.get("session_timeout", base.session_timeout),
            *SESSION_TIMEOUT_RANGE,
            f"{where}.session_timeout",
        ),
        session_lighting=(
            _parse_lighting(
                obj["session_lighting"],
                base.session_lighting,
                f"{where}.session_lighting",
            )
            if "session_lighting" in obj
            else base.session_lighting
        ),
        fast_poll_hold=_as_int(
            obj.get("fast_poll_hold", base.fast_poll_hold),
            *FAST_POLL_HOLD_RANGE,
            f"{where}.fast_poll_hold",
        ),
        hold=_as_int(obj.get("hold", base.hold), *HOLD_RANGE, f"{where}.hold"),
    )


def parse_config(data: Any, base: Optional[ScheduleConfig] = None) -> ScheduleConfig:
    """
    Validates a decoded schedule.json document and merges it onto ``base``
    (default: the built-in config). Phases merge by name and key, ``windows``
    replaces the base windows wholesale and ``interaction`` merges by key.
    Raises ConfigError on any problem.
    """
    base = base or default_config()
    obj = _as_dict(data, "config")
    _check_keys(
        obj, ("version", "default_phase", "phases", "windows", "interaction"), "config"
    )
    version = obj.get("version", CONFIG_VERSION)
    if version != CONFIG_VERSION or isinstance(version, bool):
        raise ConfigError(f"config.version: only version {CONFIG_VERSION} is supported")

    phases: dict[str, PhaseConfig] = dict(base.phases)
    if "phases" in obj:
        for name, spec in _as_dict(obj["phases"], "phases").items():
            phases[name] = _parse_phase(name, spec, phases.get(name))
    if len(phases) > MAX_PHASES:
        raise ConfigError(f"phases: at most {MAX_PHASES} phases are allowed")

    windows = base.windows
    if "windows" in obj:
        raw = obj["windows"]
        if not isinstance(raw, list):
            raise ConfigError("windows: expected a list")
        if len(raw) > MAX_WINDOWS:
            raise ConfigError(f"windows: at most {MAX_WINDOWS} windows are allowed")
        windows = tuple(_parse_window(i, w) for i, w in enumerate(raw))

    default_phase = obj.get("default_phase", base.default_phase)
    if not isinstance(default_phase, str) or default_phase not in phases:
        raise ConfigError("default_phase: must name a defined phase")
    for i, w in enumerate(windows):
        if w.phase not in phases:
            raise ConfigError(f"windows[{i}].phase: undefined phase {w.phase!r}")

    interaction = (
        _parse_interaction(obj["interaction"], base.interaction)
        if "interaction" in obj
        else base.interaction
    )
    return replace(
        base,
        phases=phases,
        windows=windows,
        default_phase=default_phase,
        interaction=interaction,
    )


# ---------------------------------------------------------------------------
# Legacy env-var compatibility
# ---------------------------------------------------------------------------

_TRUTHY = ("1", "true", "yes", "on")


def _env(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _env_hour(env: Mapping[str, str], name: str) -> Optional[float]:
    raw = _env(env, name)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _hour_to_minute(hour: float) -> int:
    """
    Converts a decimal hour to the first whole minute at or after it, so that
    ``hour <= h + m/60`` (the historical comparison) is equivalent to
    ``minute >= result``.
    """
    return max(0, min(1440, math.ceil(round(hour * 60, 6))))


def _env_interval(env: Mapping[str, str], name: str) -> Optional[int]:
    value = _env_hour(env, name)
    if value is None:
        return None
    return max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, int(value)))


def _legacy_overnight_window(start_h: float, end_h: float) -> Optional[Window]:
    """
    Translates the historical overnight predicate exactly: ordered windows
    (start <= end) matched ``start <= hour < end``; otherwise the window
    wrapped and matched ``hour >= start or hour < end``. Returns None when the
    predicate can never be true.
    """
    s, e = _hour_to_minute(start_h), _hour_to_minute(end_h)
    if start_h <= end_h:
        return Window("overnight", s, e, ALL_DAYS) if s < e else None
    if s <= e:
        return Window("overnight", 0, 1440, ALL_DAYS)
    if s >= 1440 and e <= 0:
        return None
    return Window("overnight", s, e, ALL_DAYS)


_WINDOW_ENV = (
    "PEAK_AM_START",
    "PEAK_AM_END",
    "PEAK_PM_START",
    "PEAK_PM_END",
    "PEAK_WEEKENDS",
    "OVERNIGHT_START",
    "OVERNIGHT_END",
)


def apply_env_overrides(
    cfg: ScheduleConfig, env: Mapping[str, str], custom_windows: bool
) -> tuple[ScheduleConfig, list[str]]:
    """
    Applies the legacy env vars on top of ``cfg``. Window env vars only apply
    to the built-in windows; when the config file defines its own windows they
    are ignored with a warning. Returns (config, warnings).
    """
    warnings: list[str] = []
    window_env_set = [n for n in _WINDOW_ENV if _env(env, n)]
    windows = cfg.windows
    if window_env_set and custom_windows:
        warnings.append(
            "ignoring "
            + ", ".join(window_env_set)
            + " because schedule.json defines its own windows"
        )
    elif window_env_set:
        weekends = _env(env, "PEAK_WEEKENDS").lower() in _TRUTHY
        peak_days = ALL_DAYS if weekends else WEEKDAYS

        def hour(name: str, default: float) -> float:
            value = _env_hour(env, name)
            return default if value is None else value

        built: list[Window] = []
        for start_name, end_name, d_start, d_end, view in (
            ("PEAK_AM_START", "PEAK_AM_END", 7.5, 9.5, "morning"),
            ("PEAK_PM_START", "PEAK_PM_END", 16.5, 19.0, "evening"),
        ):
            start = _hour_to_minute(hour(start_name, d_start))
            end = _hour_to_minute(hour(end_name, d_end))
            # Historically a peak window with start >= end never matched.
            if start < end:
                built.append(Window("peak", start, end, peak_days, view))
            else:
                warnings.append(f"{start_name}/{end_name} window is empty; skipped")
        overnight = _legacy_overnight_window(
            hour("OVERNIGHT_START", 22.0), hour("OVERNIGHT_END", 6.0)
        )
        if overnight is not None:
            built.append(overnight)
        else:
            warnings.append("OVERNIGHT_START/OVERNIGHT_END window is empty; skipped")
        windows = tuple(built)

    phases = dict(cfg.phases)
    for env_name, phase_name in (
        ("OFFPEAK_INTERVAL", "offpeak"),
        ("OVERNIGHT_INTERVAL", "overnight"),
    ):
        interval = _env_interval(env, env_name)
        if interval is None:
            continue
        if phase_name in phases:
            phases[phase_name] = replace(phases[phase_name], poll_interval=interval)
        else:
            warnings.append(f"ignoring {env_name}: no {phase_name!r} phase defined")

    force_phase = _env(env, "FORCE_PHASE")
    if force_phase and force_phase not in phases:
        warnings.append(f"ignoring FORCE_PHASE={force_phase!r}: undefined phase")
        force_phase = ""

    return (
        replace(
            cfg,
            phases=phases,
            windows=windows,
            force_phase=force_phase,
            force_fast_poll=_env(env, "FORCE_FAST_POLL").lower() in _TRUTHY,
        ),
        warnings,
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _match(cfg: ScheduleConfig, dt: datetime) -> tuple[str, Optional[str]]:
    """Returns (phase name, window view) for ``dt``, ignoring overrides."""
    if cfg.force_phase:
        return cfg.force_phase, None
    for w in cfg.windows:
        if w.matches(dt):
            return w.phase, w.view
    return cfg.default_phase, None


def _candidate_times(cfg: ScheduleConfig, dt: datetime) -> list[datetime]:
    """Every window boundary (and midnight) in the search horizon after dt."""
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    offsets = {0}
    for w in cfg.windows:
        offsets.add(w.start)
        offsets.add(w.end)
    out = set()
    for day in range(TRANSITION_HORIZON_DAYS + 1):
        base = midnight + timedelta(days=day)
        for minutes in offsets:
            t = base + timedelta(minutes=minutes)
            if t > dt:
                out.add(t)
    return sorted(out)


def next_transition(cfg: ScheduleConfig, dt: datetime) -> Optional[datetime]:
    """
    Returns the first instant after ``dt`` at which the phase changes, or None
    if it does not change within the search horizon (or is pinned).
    """
    if cfg.force_phase:
        return None
    current, _ = _match(cfg, dt)
    for t in _candidate_times(cfg, dt):
        if _match(cfg, t)[0] != current:
            return t
    return None


def format_clock(dt: Optional[datetime]) -> str:
    """Formats a time as e.g. '6:00 AM'; 'later' when unknown."""
    if dt is None:
        return "later"
    hour12 = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{hour12}:{dt.minute:02d} {suffix}"


def render_note(template: str, until: Optional[datetime]) -> str:
    return template.replace("{until}", format_clock(until))


def resolve(cfg: ScheduleConfig, dt: Optional[datetime] = None) -> ResolvedState:
    """
    Resolves the effective schedule state at ``dt`` (default: now). Pure apart
    from reading the clock when ``dt`` is None.
    """
    if dt is None:
        dt = datetime.now()
    name, view = _match(cfg, dt)
    phase = cfg.phases[name]
    until = next_transition(cfg, dt)
    presentation = phase.presentation
    poll_interval = phase.poll_interval
    if cfg.force_fast_poll:
        # Testing aid: always interactive with the fast cadence.
        presentation = "interactive"
        poll_interval = 60
    note = (
        "" if presentation == "interactive" else render_note(phase.status_note, until)
    )
    return ResolvedState(
        phase=name,
        poll_interval=poll_interval,
        presentation=presentation,
        status_note=note,
        lighting=phase.lighting,
        realtime=phase.realtime,
        suspend=phase.suspend and presentation != "interactive",
        view=view,
        until=until,
    )


def phase_note(cfg: ScheduleConfig, presentation: str, dt: datetime) -> str:
    """
    Status-strip text for an arbitrary presentation: the current phase's note
    when it matches, otherwise the note of the next phase with that
    presentation, with ``{until}`` taken from that phase's next occurrence.
    """
    if presentation == "interactive":
        return ""
    name, _ = _match(cfg, dt)
    if cfg.phases[name].presentation == presentation:
        return render_note(cfg.phases[name].status_note, next_transition(cfg, dt))
    target = next(
        (p for p in cfg.phases.values() if p.presentation == presentation), None
    )
    if target is None:
        return ""
    for t in _candidate_times(cfg, dt):
        if _match(cfg, t)[0] == target.name:
            return render_note(target.status_note, next_transition(cfg, t))
    return render_note(target.status_note, None)


def upcoming_transitions(
    cfg: ScheduleConfig, dt: datetime, horizon: timedelta
) -> list[tuple[datetime, str]]:
    """Lists (time, new phase) transitions in (dt, dt + horizon]."""
    out: list[tuple[datetime, str]] = []
    current, _ = _match(cfg, dt)
    end = dt + horizon
    for t in _candidate_times(cfg, dt):
        if t > end:
            break
        name, _ = _match(cfg, t)
        if name != current:
            out.append((t, name))
            current = name
    return out


def format_policy_header(state: ResolvedState, cfg: ScheduleConfig) -> str:
    """
    Encodes the client policy as ``key=value`` pairs joined by ``;``. Every
    value is ASCII drawn from validated config, so the header is always safe.
    """
    ia = cfg.interaction
    parts = [
        f"v={POLICY_VERSION}",
        f"phase={state.phase}",
    ]
    if state.until is not None:
        parts.append(f"until={int(state.until.timestamp())}")
    parts.extend(
        [
            f"suspend={1 if state.suspend else 0}",
            f"session={ia.session_timeout}",
            f"fast={ia.fast_poll_hold}",
            f"hold={ia.hold}",
            f"sl={ia.session_lighting.brightness},{ia.session_lighting.warmth}",
        ]
    )
    return ";".join(parts)


# ---------------------------------------------------------------------------
# Loading & hot reload
# ---------------------------------------------------------------------------


def load_config(
    path: Optional[str], env: Mapping[str, str]
) -> tuple[ScheduleConfig, list[str]]:
    """
    Loads ``path`` (if it exists) and applies env overrides. Raises
    ConfigError if the file exists but is invalid. Returns (config, warnings).
    """
    cfg = default_config()
    custom_windows = False
    if path and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                raw = f.read(MAX_CONFIG_BYTES + 1)
        except OSError as e:
            raise ConfigError(f"cannot read {path}: {e}") from e
        if len(raw) > MAX_CONFIG_BYTES:
            raise ConfigError(f"{path} is larger than {MAX_CONFIG_BYTES} bytes")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ConfigError(f"{path} is not valid JSON: {e}") from e
        cfg = parse_config(data, cfg)
        custom_windows = isinstance(data, dict) and "windows" in data
    return apply_env_overrides(cfg, env, custom_windows)


class ConfigStore:
    """
    Thread-safe holder for the active schedule config. Re-reads the file when
    its mtime (or existence) changes; an invalid file is reported and the last
    good config stays in effect.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        log: Any = print,
    ) -> None:
        self.env: Mapping[str, str] = dict(os.environ if env is None else env)
        if path is None:
            path = _env(self.env, "SCHEDULE_CONFIG") or DEFAULT_CONFIG_PATH
        self.path = path
        self._log = log
        self._lock = threading.Lock()
        self._stamp: Any = object()  # forces the first load
        self._config, _ = apply_env_overrides(default_config(), self.env, False)
        self.source = "built-in defaults"
        self.error = ""
        self._override_phase: Optional[str] = None
        self._override_fast_poll: Optional[bool] = None
        self.get()

    def _file_stamp(self) -> Optional[tuple[int, int]]:
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _apply_runtime_overrides(self, cfg: ScheduleConfig) -> ScheduleConfig:
        phase = cfg.force_phase
        if self._override_phase is not None:
            phase = self._override_phase
        fast_poll = cfg.force_fast_poll
        if self._override_fast_poll is not None:
            fast_poll = self._override_fast_poll
        if phase != cfg.force_phase or fast_poll != cfg.force_fast_poll:
            return replace(cfg, force_phase=phase, force_fast_poll=fast_poll)
        return cfg

    def get(self) -> ScheduleConfig:
        stamp = self._file_stamp()
        with self._lock:
            if stamp == self._stamp:
                return self._apply_runtime_overrides(self._config)
            self._stamp = stamp
            try:
                cfg, warnings = load_config(self.path, self.env)
            except ConfigError as e:
                self.error = str(e)
                self._log(
                    f"[Schedule] rejected {self.path}: {e}; keeping {self.source}."
                )
                return self._apply_runtime_overrides(self._config)
            self._config = cfg
            self.error = ""
            self.source = self.path if stamp is not None else "built-in defaults"
            for w in warnings:
                self._log(f"[Schedule] warning: {w}")
            self._log(f"[Schedule] loaded schedule from {self.source}.")
            return self._apply_runtime_overrides(self._config)

    def set_override(
        self,
        force_phase: Optional[str] = None,
        force_fast_poll: Optional[bool] = None,
    ) -> ScheduleConfig:
        with self._lock:
            if force_phase is not None:
                clean_phase = force_phase.strip().lower()
                if clean_phase in ("auto", "none", "clear", ""):
                    clean_phase = ""
                elif clean_phase not in self._config.phases:
                    raise ConfigError(f"unknown phase {clean_phase!r}")
                self._override_phase = clean_phase
            if force_fast_poll is not None:
                self._override_fast_poll = bool(force_fast_poll)
            self._config = self._apply_runtime_overrides(self._config)
            self._log(
                f"[Schedule] override updated: phase={self._override_phase!r}, fast_poll={self._override_fast_poll}"
            )
            return self._config

    def clear_override(self) -> ScheduleConfig:
        with self._lock:
            self._override_phase = ""
            self._override_fast_poll = False
            self._config = replace(self._config, force_phase="", force_fast_poll=False)
            self._log("[Schedule] overrides cleared.")
            return self._config

    def save_config(
        self, data: dict[str, Any], target_path: Optional[str] = None
    ) -> tuple[ScheduleConfig, list[str]]:
        new_cfg = parse_config(data)
        dict_data = new_cfg.to_dict()
        raw = json.dumps(dict_data, indent=2).encode("utf-8") + b"\n"

        target = target_path or self.path
        written_path = target

        def _atomic_write(dest: str) -> None:
            parent = os.path.dirname(os.path.abspath(dest))
            os.makedirs(parent, exist_ok=True)
            tmp = dest + f".tmp.{os.getpid()}"
            with open(tmp, "wb") as f:
                f.write(raw)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, dest)

        try:
            _atomic_write(target)
        except OSError as err:
            try:
                from paths import resolve_cache_dir

                cache_dir = resolve_cache_dir()
            except ImportError:
                cache_dir = os.environ.get("CACHE_DIR", "/app/cache")
            fallback = os.path.join(cache_dir, "schedule.json")
            if os.path.abspath(fallback) == os.path.abspath(target):
                raise ConfigError(
                    f"failed to write schedule to {target}: {err}"
                ) from err
            try:
                _atomic_write(fallback)
                written_path = fallback
                self._log(
                    f"[Schedule] target {target} not writable ({err}); saved to {fallback}"
                )
            except OSError as fallback_err:
                raise ConfigError(
                    f"failed to write schedule to {target} ({err}) and fallback {fallback} ({fallback_err})"
                ) from fallback_err

        with self._lock:
            self.path = written_path
            self.source = written_path
            self.error = ""
            self._stamp = self._file_stamp()
            cfg, warnings = apply_env_overrides(
                new_cfg, self.env, custom_windows=bool(new_cfg.windows)
            )
            self._config = self._apply_runtime_overrides(cfg)
            for w in warnings:
                self._log(f"[Schedule] warning: {w}")
            self._log(
                f"[Schedule] saved and applied schedule configuration to {self.source}."
            )
            return self._config, warnings

    def reset_to_default(self) -> tuple[ScheduleConfig, list[str]]:
        self.clear_override()
        return self.save_config(default_config().to_dict())

    def resolve(self, dt: Optional[datetime] = None) -> ResolvedState:
        return resolve(self.get(), dt)
