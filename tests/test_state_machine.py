"""
Tests for the configurable schedule state machine (server/state_machine.py).

The legacy if-chain schedule is reproduced below as a reference model so the
built-in defaults (and the legacy env-var overrides) can be proven equivalent
to the historical behaviour, minute by minute.
"""

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

import state_machine as sm
from hypothesis import given, settings
from hypothesis import strategies as st

# A Thursday; 2026-10-10 is a Saturday.
THU = datetime(2026, 10, 8)


def at(day_offset, hour, minute=0, base=THU):
    return base + timedelta(days=day_offset, hours=hour, minutes=minute)


def quiet_store(path="", **env):
    logs = []
    store = sm.ConfigStore(path=path, env=env, log=logs.append)
    return store, logs


# ---------------------------------------------------------------------------
# Reference model: the pre-state-machine schedule.py logic
# ---------------------------------------------------------------------------


class Legacy:
    def __init__(
        self,
        am=(7.5, 9.5),
        pm=(16.5, 19.0),
        weekends=False,
        overnight=(22.0, 6.0),
        offpeak_interval=600,
        overnight_interval=3600,
    ):
        self.am, self.pm, self.weekends = am, pm, weekends
        self.overnight = overnight
        self.offpeak_interval = offpeak_interval
        self.overnight_interval = overnight_interval

    def is_peak(self, dt):
        if dt.weekday() >= 5 and not self.weekends:
            return False
        hour = dt.hour + dt.minute / 60.0
        return (self.am[0] <= hour < self.am[1]) or (self.pm[0] <= hour < self.pm[1])

    def is_overnight(self, dt):
        start, end = self.overnight
        hour = dt.hour + dt.minute / 60.0
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    def presentation(self, dt):
        if self.is_peak(dt):
            return "interactive"
        if self.is_overnight(dt):
            return "dormant"
        return "idle"

    def interval(self, dt):
        if self.is_peak(dt):
            return 60
        if self.is_overnight(dt):
            return self.overnight_interval
        return self.offpeak_interval

    def lighting(self, dt):
        return (8, 12) if self.is_peak(dt) else (0, 0)

    def note(self, presentation):
        if presentation == "dormant":
            total = int(round(self.overnight[1] * 60)) % (24 * 60)
            hour, minute = divmod(total, 60)
            suffix = "AM" if hour < 12 else "PM"
            return (
                f"SLEEPING — back at {hour % 12 or 12}:{minute:02d} {suffix}"
                " · press power to interact"
            )
        if presentation == "idle":
            return "PRESS POWER BUTTON TO INTERACT"
        return ""


class TestDefaultEquivalence(unittest.TestCase):
    def test_every_minute_of_a_week_matches_legacy(self):
        cfg = sm.default_config()
        legacy = Legacy()
        dt = datetime(2026, 10, 5)  # Monday
        for _ in range(7 * 24 * 60):
            state = sm.resolve(cfg, dt)
            self.assertEqual(state.presentation, legacy.presentation(dt), dt)
            self.assertEqual(state.poll_interval, legacy.interval(dt), dt)
            self.assertEqual(
                (state.lighting.brightness, state.lighting.warmth),
                legacy.lighting(dt),
                dt,
            )
            self.assertEqual(state.realtime, not legacy.is_overnight(dt), dt)
            self.assertEqual(state.status_note, legacy.note(state.presentation), dt)
            dt += timedelta(minutes=1)

    def test_window_views_match_hour_rule(self):
        # The default window views reproduce resolve_view()'s 5-12 rule inside
        # the peak windows, so "auto" renders exactly as before.
        cfg = sm.default_config()
        self.assertEqual(sm.resolve(cfg, at(0, 8)).view, "morning")
        self.assertEqual(sm.resolve(cfg, at(0, 17)).view, "evening")
        self.assertIsNone(sm.resolve(cfg, at(0, 13)).view)


class TestEnvEquivalence(unittest.TestCase):
    hours = st.floats(min_value=-2, max_value=26, allow_nan=False).map(
        lambda h: round(h, 2)
    )

    @settings(max_examples=150, deadline=None)
    @given(
        am_s=hours,
        am_e=hours,
        pm_s=hours,
        pm_e=hours,
        on_s=hours,
        on_e=hours,
        weekends=st.booleans(),
        minute_of_week=st.integers(min_value=0, max_value=7 * 1440 - 1),
    )
    def test_legacy_env_vars_match_legacy_logic(
        self, am_s, am_e, pm_s, pm_e, on_s, on_e, weekends, minute_of_week
    ):
        env = {
            "PEAK_AM_START": str(am_s),
            "PEAK_AM_END": str(am_e),
            "PEAK_PM_START": str(pm_s),
            "PEAK_PM_END": str(pm_e),
            "OVERNIGHT_START": str(on_s),
            "OVERNIGHT_END": str(on_e),
            "PEAK_WEEKENDS": "1" if weekends else "",
        }
        cfg, _ = sm.apply_env_overrides(sm.default_config(), env, False)
        legacy = Legacy(
            am=(am_s, am_e), pm=(pm_s, pm_e), weekends=weekends, overnight=(on_s, on_e)
        )
        dt = datetime(2026, 10, 5) + timedelta(minutes=minute_of_week)
        # Legacy precedence: peak wins over overnight, same as window order.
        self.assertEqual(sm.resolve(cfg, dt).presentation, legacy.presentation(dt))


class TestWindowMatching(unittest.TestCase):
    def test_plain_window_is_half_open(self):
        w = sm.Window("x", 450, 570)
        self.assertFalse(w.matches(at(0, 7, 29)))
        self.assertTrue(w.matches(at(0, 7, 30)))
        self.assertTrue(w.matches(at(0, 9, 29)))
        self.assertFalse(w.matches(at(0, 9, 30)))

    def test_wrapping_window_belongs_to_start_day(self):
        fri = frozenset({4})
        w = sm.Window("late", 1380, 120, fri)  # Fri 23:00 - Sat 02:00
        self.assertTrue(w.matches(at(1, 23, 30)))  # Fri 23:30
        self.assertTrue(w.matches(at(2, 1, 0)))  # Sat 01:00
        self.assertFalse(w.matches(at(2, 23, 30)))  # Sat 23:30
        self.assertFalse(w.matches(at(1, 1, 0)))  # Fri 01:00 (Thu night)
        self.assertFalse(w.matches(at(2, 2, 0)))  # Sat 02:00 (end exclusive)

    def test_window_until_midnight(self):
        w = sm.Window("x", 1320, 1440)
        self.assertTrue(w.matches(at(0, 23, 59)))
        self.assertFalse(w.matches(at(1, 0, 0)))

    def test_first_match_wins(self):
        cfg = sm.parse_config(
            {
                "windows": [
                    {"phase": "peak", "start": "08:00", "end": "09:00"},
                    {"phase": "overnight", "start": "00:00", "end": "24:00"},
                ]
            }
        )
        self.assertEqual(sm.resolve(cfg, at(0, 8, 30)).phase, "peak")
        self.assertEqual(sm.resolve(cfg, at(0, 9, 0)).phase, "overnight")


class TestTransitions(unittest.TestCase):
    def setUp(self):
        self.cfg = sm.default_config()

    def test_next_transition_within_day(self):
        self.assertEqual(sm.next_transition(self.cfg, at(0, 8)), at(0, 9, 30))
        self.assertEqual(sm.next_transition(self.cfg, at(0, 13)), at(0, 16, 30))

    def test_next_transition_crosses_midnight(self):
        self.assertEqual(sm.next_transition(self.cfg, at(0, 23)), at(1, 6))

    def test_seconds_are_respected(self):
        dt = at(0, 7, 29) + timedelta(seconds=30)
        self.assertEqual(sm.resolve(self.cfg, dt).phase, "offpeak")
        self.assertEqual(sm.next_transition(self.cfg, dt), at(0, 7, 30))

    def test_weekend_has_no_peak(self):
        sat = at(2, 7)
        self.assertEqual(sm.resolve(self.cfg, sat).phase, "offpeak")
        self.assertEqual(sm.next_transition(self.cfg, sat), at(2, 22))

    def test_no_transition_without_windows(self):
        cfg = replace(self.cfg, windows=())
        self.assertIsNone(sm.next_transition(cfg, at(0, 8)))
        self.assertEqual(
            sm.resolve(cfg, at(0, 8)).status_note, ("PRESS POWER BUTTON TO INTERACT")
        )

    def test_forced_phase_never_transitions(self):
        cfg = replace(self.cfg, force_phase="overnight")
        state = sm.resolve(cfg, at(0, 8))
        self.assertEqual(state.phase, "overnight")
        self.assertIsNone(state.until)
        self.assertIn("back at later", state.status_note)

    def test_dst_spring_forward_uses_wall_clock(self):
        # 2026-03-08 is the US spring-forward Sunday; boundaries stay on the
        # wall clock and the epoch conversion never raises.
        dt = datetime(2026, 3, 8, 1, 30)
        until = sm.next_transition(self.cfg, dt)
        self.assertEqual(until, datetime(2026, 3, 8, 6, 0))
        self.assertIsInstance(int(until.timestamp()), int)

    def test_upcoming_transitions_for_a_weekday(self):
        got = sm.upcoming_transitions(self.cfg, at(0, 0), timedelta(hours=24))
        self.assertEqual(
            got,
            [
                (at(0, 6), "offpeak"),
                (at(0, 7, 30), "peak"),
                (at(0, 9, 30), "offpeak"),
                (at(0, 16, 30), "peak"),
                (at(0, 19), "offpeak"),
                (at(0, 22), "overnight"),
            ],
        )

    @settings(max_examples=200, deadline=None)
    @given(st.integers(min_value=0, max_value=14 * 1440 * 60))
    def test_until_properties(self, seconds):
        dt = datetime(2026, 10, 5) + timedelta(seconds=seconds)
        state = sm.resolve(self.cfg, dt)
        self.assertIn(state.phase, self.cfg.phases)
        self.assertIsNotNone(state.until)
        self.assertGreater(state.until, dt)
        self.assertLessEqual(state.until, dt + timedelta(days=8))
        # The phase is constant on [dt, until) and differs at until.
        self.assertEqual(
            sm.resolve(self.cfg, state.until - timedelta(seconds=1)).phase,
            state.phase,
        )
        self.assertNotEqual(sm.resolve(self.cfg, state.until).phase, state.phase)


class TestResolve(unittest.TestCase):
    def test_force_fast_poll(self):
        cfg = replace(sm.default_config(), force_fast_poll=True)
        state = sm.resolve(cfg, at(0, 3))
        self.assertEqual(state.phase, "overnight")
        self.assertEqual(state.presentation, "interactive")
        self.assertEqual(state.poll_interval, 60)
        self.assertEqual(state.status_note, "")
        self.assertFalse(state.suspend)
        self.assertFalse(state.realtime)

    def test_resolve_defaults_to_now(self):
        state = sm.resolve(sm.default_config())
        self.assertIn(state.phase, ("peak", "offpeak", "overnight"))

    def test_note_template(self):
        self.assertEqual(sm.render_note("back {until}", at(0, 6)), "back 6:00 AM")
        self.assertEqual(sm.render_note("back {until}", at(0, 18, 5)), "back 6:05 PM")
        self.assertEqual(sm.render_note("back {until}", at(0, 0)), "back 12:00 AM")
        self.assertEqual(sm.render_note("back {until}", None), "back later")
        self.assertEqual(sm.render_note("static", None), "static")

    def test_phase_note(self):
        cfg = sm.default_config()
        midday = at(0, 13)
        self.assertEqual(sm.phase_note(cfg, "interactive", midday), "")
        self.assertEqual(
            sm.phase_note(cfg, "idle", midday), "PRESS POWER BUTTON TO INTERACT"
        )
        # Dormant requested at midday: the overnight phase's next occurrence
        # (22:00) ends at 06:00.
        self.assertIn("back at 6:00 AM", sm.phase_note(cfg, "dormant", midday))
        # During the phase itself the current end is used.
        self.assertIn("back at 6:00 AM", sm.phase_note(cfg, "dormant", at(0, 23)))

    def test_phase_note_without_matching_phase(self):
        cfg = sm.parse_config({"phases": {"overnight": {"presentation": "idle"}}})
        self.assertEqual(sm.phase_note(cfg, "dormant", at(0, 13)), "")

    def test_phase_note_for_unscheduled_phase(self):
        cfg = sm.parse_config(
            {"windows": [{"phase": "peak", "start": "07:00", "end": "08:00"}]}
        )
        self.assertIn("back at later", sm.phase_note(cfg, "dormant", at(0, 13)))


class TestPolicyHeader(unittest.TestCase):
    def test_format(self):
        cfg = sm.default_config()
        state = sm.resolve(cfg, at(0, 8))
        header = sm.format_policy_header(state, cfg)
        self.assertEqual(
            header,
            f"v=1;phase=peak;until={int(at(0, 9, 30).timestamp())};suspend=0;"
            "session=90;fast=600;hold=2700;sl=8,12",
        )
        header.encode("ascii")

    def test_without_until_and_custom_interaction(self):
        cfg = sm.parse_config(
            {
                "windows": [],
                "interaction": {
                    "session_timeout": 30,
                    "session_lighting": {"warmth": 0},
                    "fast_poll_hold": 0,
                    "hold": 60,
                },
            }
        )
        state = sm.resolve(cfg, at(0, 8))
        self.assertEqual(
            sm.format_policy_header(state, cfg),
            "v=1;phase=offpeak;suspend=1;session=30;fast=0;hold=60;sl=8,0",
        )


class TestParseConfig(unittest.TestCase):
    def test_empty_document_is_defaults(self):
        self.assertEqual(sm.parse_config({}), sm.default_config())

    def test_to_dict_round_trips(self):
        cfg = sm.parse_config(
            {
                "phases": {"weekend": {"poll_interval": 900}},
                "windows": [
                    {
                        "phase": "weekend",
                        "days": ["sat", "sun"],
                        "start": "08:00",
                        "end": "00:00",
                    },
                    {
                        "phase": "peak",
                        "start": "07:00",
                        "end": "08:00",
                        "view": "morning",
                    },
                ],
            }
        )
        doc = json.loads(json.dumps(cfg.to_dict()))
        self.assertEqual(sm.parse_config(doc), cfg)
        self.assertEqual(doc["windows"][0]["end"], "24:00")

    def test_phase_merge_keeps_unspecified_fields(self):
        cfg = sm.parse_config({"phases": {"peak": {"lighting": {"brightness": 4}}}})
        peak = cfg.phases["peak"]
        self.assertEqual(peak.lighting, sm.Lighting(4, 12))
        self.assertEqual(peak.poll_interval, 60)
        self.assertEqual(peak.presentation, "interactive")
        self.assertIn("offpeak", cfg.phases)
        self.assertEqual(cfg.windows, sm.default_config().windows)

    def test_new_phase_gets_field_defaults(self):
        cfg = sm.parse_config({"phases": {"weekend": {}}})
        self.assertEqual(cfg.phases["weekend"], sm.PhaseConfig(name="weekend"))

    def test_windows_replace_wholesale(self):
        cfg = sm.parse_config({"windows": []})
        self.assertEqual(cfg.windows, ())

    def test_end_midnight_aliases(self):
        cfg = sm.parse_config(
            {
                "windows": [
                    {"phase": "peak", "start": "22:00", "end": "00:00"},
                    {"phase": "peak", "start": "00:00", "end": "24:00"},
                ]
            }
        )
        self.assertEqual(cfg.windows[0].end, 1440)
        self.assertEqual(cfg.windows[1].end, 1440)

    def test_rejections(self):
        bad = [
            ([], "expected an object"),
            ({"bogus": 1}, "unknown key"),
            ({"version": 2}, "only version 1"),
            ({"version": True}, "only version 1"),
            ({"default_phase": "nope"}, "default_phase"),
            ({"default_phase": 3}, "default_phase"),
            ({"phases": []}, "expected an object"),
            ({"phases": {"Bad Name": {}}}, "phase names"),
            ({"phases": {"peak": []}}, "expected an object"),
            ({"phases": {"peak": {"colour": 1}}}, "unknown key"),
            ({"phases": {"peak": {"poll_interval": 5}}}, "outside 30..7200"),
            ({"phases": {"peak": {"poll_interval": True}}}, "expected an integer"),
            ({"phases": {"peak": {"poll_interval": "60"}}}, "expected an integer"),
            ({"phases": {"peak": {"presentation": "loud"}}}, "expected one of"),
            ({"phases": {"peak": {"status_note": 3}}}, "expected a string"),
            ({"phases": {"peak": {"status_note": "x" * 121}}}, "printable"),
            ({"phases": {"peak": {"status_note": "a\nb"}}}, "printable"),
            ({"phases": {"peak": {"lighting": {"brightness": 25}}}}, "outside 0..24"),
            ({"phases": {"peak": {"lighting": {"hue": 1}}}}, "unknown key"),
            ({"phases": {"peak": {"realtime": "yes"}}}, "true or false"),
            ({"phases": {f"p{i}": {} for i in range(14)}}, "at most 16 phases"),
            ({"windows": {}}, "expected a list"),
            ({"windows": [{}] * 65}, "at most 64 windows"),
            ({"windows": [{"phase": "peak", "start": "07:00"}]}, "missing required"),
            (
                {"windows": [{"phase": 1, "start": "07:00", "end": "08:00"}]},
                "phase name",
            ),
            (
                {"windows": [{"phase": "x", "start": "07:00", "end": "08:00"}]},
                "undefined phase",
            ),
            (
                {"windows": [{"phase": "peak", "start": "7:00", "end": "08:00"}]},
                "HH:MM",
            ),
            ({"windows": [{"phase": "peak", "start": 7, "end": "08:00"}]}, "HH:MM"),
            (
                {"windows": [{"phase": "peak", "start": "24:30", "end": "08:00"}]},
                "past 24:00",
            ),
            (
                {"windows": [{"phase": "peak", "start": "24:00", "end": "08:00"}]},
                "must differ",
            ),
            (
                {"windows": [{"phase": "peak", "start": "08:00", "end": "08:00"}]},
                "must differ",
            ),
            (
                {
                    "windows": [
                        {"phase": "peak", "start": "07:00", "end": "08:00", "days": []}
                    ]
                },
                "non-empty",
            ),
            (
                {
                    "windows": [
                        {
                            "phase": "peak",
                            "start": "07:00",
                            "end": "08:00",
                            "days": ["mon", "funday"],
                        }
                    ]
                },
                "days[1]",
            ),
            (
                {
                    "windows": [
                        {
                            "phase": "peak",
                            "start": "07:00",
                            "end": "08:00",
                            "view": "noon",
                        }
                    ]
                },
                "view",
            ),
            (
                {
                    "windows": [
                        {"phase": "peak", "start": "07:00", "end": "08:00", "x": 1}
                    ]
                },
                "unknown key",
            ),
            ({"interaction": {"session_timeout": 5}}, "outside 10..1800"),
            ({"interaction": {"hold": 99999}}, "outside 0..14400"),
            ({"interaction": {"fast_poll_hold": -1}}, "outside 0..7200"),
            ({"interaction": {"nope": 1}}, "unknown key"),
            ({"interaction": {"session_lighting": {"warmth": 30}}}, "outside 0..24"),
        ]
        for doc, fragment in bad:
            with self.subTest(doc=doc):
                with self.assertRaises(sm.ConfigError) as ctx:
                    sm.parse_config(doc)
                self.assertIn(fragment, str(ctx.exception))


class TestEnvOverrides(unittest.TestCase):
    def test_no_env_is_identity(self):
        cfg, warnings = sm.apply_env_overrides(sm.default_config(), {}, False)
        self.assertEqual(cfg, sm.default_config())
        self.assertEqual(warnings, [])

    def test_empty_values_are_unset(self):
        env = dict.fromkeys(sm._WINDOW_ENV, "")
        env.update(OFFPEAK_INTERVAL="", FORCE_PHASE="", FORCE_FAST_POLL="")
        cfg, warnings = sm.apply_env_overrides(sm.default_config(), env, False)
        self.assertEqual(cfg, sm.default_config())
        self.assertEqual(warnings, [])

    def test_env_beats_file_for_intervals(self):
        base = sm.parse_config({"phases": {"offpeak": {"poll_interval": 900}}})
        cfg, _ = sm.apply_env_overrides(base, {"OFFPEAK_INTERVAL": "300"}, True)
        self.assertEqual(cfg.phases["offpeak"].poll_interval, 300)

    def test_intervals_are_clamped_and_invalid_ignored(self):
        cfg, _ = sm.apply_env_overrides(
            sm.default_config(),
            {"OFFPEAK_INTERVAL": "1", "OVERNIGHT_INTERVAL": "999999"},
            False,
        )
        self.assertEqual(cfg.phases["offpeak"].poll_interval, 30)
        self.assertEqual(cfg.phases["overnight"].poll_interval, 7200)
        cfg, _ = sm.apply_env_overrides(
            sm.default_config(), {"OFFPEAK_INTERVAL": "soon"}, False
        )
        self.assertEqual(cfg.phases["offpeak"].poll_interval, 600)
        cfg, _ = sm.apply_env_overrides(
            sm.default_config(), {"OFFPEAK_INTERVAL": "nan"}, False
        )
        self.assertEqual(cfg.phases["offpeak"].poll_interval, 600)

    def test_interval_env_for_missing_phase_warns(self):
        base = replace(
            sm.default_config(),
            phases={"peak": sm.default_config().phases["peak"]},
            windows=(),
            default_phase="peak",
        )
        _cfg, warnings = sm.apply_env_overrides(base, {"OFFPEAK_INTERVAL": "60"}, True)
        self.assertTrue(any("OFFPEAK_INTERVAL" in w for w in warnings))

    def test_window_env_ignored_with_custom_windows(self):
        base = sm.parse_config({"windows": []})
        cfg, warnings = sm.apply_env_overrides(base, {"PEAK_AM_START": "6"}, True)
        self.assertEqual(cfg.windows, ())
        self.assertIn("PEAK_AM_START", warnings[0])

    def test_window_env_builds_windows(self):
        cfg, warnings = sm.apply_env_overrides(
            sm.default_config(),
            {"PEAK_AM_START": "6.25", "PEAK_WEEKENDS": "yes", "OVERNIGHT_END": "0"},
            False,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(
            cfg.windows[0], sm.Window("peak", 375, 570, sm.ALL_DAYS, "morning")
        )
        self.assertEqual(cfg.windows[2], sm.Window("overnight", 1320, 0, sm.ALL_DAYS))
        self.assertTrue(cfg.windows[2].matches(at(0, 23, 59)))
        self.assertFalse(cfg.windows[2].matches(at(1, 0, 0)))

    def test_legacy_overnight_edge_cases(self):
        self.assertEqual(
            sm._legacy_overnight_window(-1, -2),
            sm.Window("overnight", 0, 1440, sm.ALL_DAYS),
        )
        self.assertIsNone(sm._legacy_overnight_window(24, 0))
        self.assertIsNone(sm._legacy_overnight_window(5, 5))
        self.assertEqual(
            sm._legacy_overnight_window(25, 6),
            sm.Window("overnight", 1440, 360, sm.ALL_DAYS),
        )

    def test_empty_env_windows_are_skipped(self):
        cfg, warnings = sm.apply_env_overrides(
            sm.default_config(),
            {
                "PEAK_AM_START": "10",
                "PEAK_AM_END": "9",
                "OVERNIGHT_START": "3",
                "OVERNIGHT_END": "3",
            },
            False,
        )
        self.assertEqual([w.phase for w in cfg.windows], ["peak"])
        self.assertEqual(len(warnings), 2)

    def test_force_phase(self):
        cfg, warnings = sm.apply_env_overrides(
            sm.default_config(), {"FORCE_PHASE": "peak"}, False
        )
        self.assertEqual(cfg.force_phase, "peak")
        self.assertEqual(warnings, [])
        cfg, warnings = sm.apply_env_overrides(
            sm.default_config(), {"FORCE_PHASE": "lunch"}, False
        )
        self.assertEqual(cfg.force_phase, "")
        self.assertIn("FORCE_PHASE", warnings[0])

    def test_force_fast_poll_truthiness(self):
        for value, expected in (("1", True), ("ON", True), ("0", False), ("", False)):
            cfg, _ = sm.apply_env_overrides(
                sm.default_config(), {"FORCE_FAST_POLL": value}, False
            )
            self.assertEqual(cfg.force_fast_poll, expected, value)


class TestLoadAndStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "schedule.json")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, content, bump=0):
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(self.path, mode) as f:
            f.write(
                content if isinstance(content, (str, bytes)) else json.dumps(content)
            )
        # Force a distinct mtime so the store notices the change.
        st = os.stat(self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + bump * 10**9))

    def test_missing_file_uses_defaults(self):
        cfg, warnings = sm.load_config(self.path, {})
        self.assertEqual(cfg, sm.default_config())
        self.assertEqual(warnings, [])
        cfg, _ = sm.load_config(None, {})
        self.assertEqual(cfg, sm.default_config())

    def test_load_errors(self):
        self.write("{not json")
        with self.assertRaisesRegex(sm.ConfigError, "not valid JSON"):
            sm.load_config(self.path, {})
        self.write(b"\xff\xfe")
        with self.assertRaisesRegex(sm.ConfigError, "not valid JSON"):
            sm.load_config(self.path, {})
        self.write(" " * (sm.MAX_CONFIG_BYTES + 1))
        with self.assertRaisesRegex(sm.ConfigError, "larger than"):
            sm.load_config(self.path, {})
        os.remove(self.path)
        os.mkdir(self.path)
        with self.assertRaisesRegex(sm.ConfigError, "cannot read"):
            sm.load_config(self.path, {})

    def test_store_hot_reload_and_fail_safe(self):
        self.write({"phases": {"offpeak": {"poll_interval": 900}}})
        store, logs = quiet_store(self.path)
        self.assertEqual(store.source, self.path)
        self.assertEqual(store.get().phases["offpeak"].poll_interval, 900)
        self.assertEqual(store.resolve(at(0, 13)).poll_interval, 900)

        # Cached while unchanged.
        first = store.get()
        self.assertIs(store.get(), first)

        # A broken edit is rejected and the last good config stays.
        self.write({"phases": {"offpeak": {"poll_interval": 1}}}, bump=1)
        self.assertEqual(store.get().phases["offpeak"].poll_interval, 900)
        self.assertIn("outside 30..7200", store.error)
        self.assertTrue(any("rejected" in line for line in logs))

        # A good edit is picked up and clears the error.
        self.write({"phases": {"offpeak": {"poll_interval": 1200}}}, bump=2)
        self.assertEqual(store.get().phases["offpeak"].poll_interval, 1200)
        self.assertEqual(store.error, "")

        # Deleting the file reverts to the built-in defaults.
        os.remove(self.path)
        self.assertEqual(store.get(), sm.default_config())
        self.assertEqual(store.source, "built-in defaults")

    def test_store_logs_env_warnings(self):
        self.write({"windows": []})
        store, logs = quiet_store(self.path, PEAK_AM_START="6")
        self.assertEqual(store.get().windows, ())
        self.assertTrue(any("warning" in line for line in logs))

    def test_invalid_file_at_startup_keeps_env_defaults(self):
        self.write("[]")
        store, _ = quiet_store(self.path, OFFPEAK_INTERVAL="300")
        self.assertEqual(store.source, "built-in defaults")
        self.assertIn("expected an object", store.error)
        self.assertEqual(store.get().phases["offpeak"].poll_interval, 300)

    def test_default_path_from_env(self):
        store = sm.ConfigStore(env={"SCHEDULE_CONFIG": self.path}, log=lambda *_: None)
        self.assertEqual(store.path, self.path)
        store = sm.ConfigStore(env={}, log=lambda *_: None)
        self.assertEqual(store.path, sm.DEFAULT_CONFIG_PATH)

    def test_store_set_and_clear_override(self):
        store, _ = quiet_store(self.path)
        self.assertEqual(store.get().force_phase, "")
        self.assertFalse(store.get().force_fast_poll)

        store.set_override(force_phase="overnight", force_fast_poll=True)
        self.assertEqual(store.get().force_phase, "overnight")
        self.assertTrue(store.get().force_fast_poll)

        with self.assertRaisesRegex(sm.ConfigError, "unknown phase"):
            store.set_override(force_phase="nonexistent_phase")

        store.set_override(force_phase="auto")
        self.assertEqual(store.get().force_phase, "")
        self.assertTrue(store.get().force_fast_poll)

        store.clear_override()
        self.assertEqual(store.get().force_phase, "")
        self.assertFalse(store.get().force_fast_poll)

    def test_store_save_config_and_reset(self):
        store, _ = quiet_store(self.path)
        valid_doc = {
            "version": 1,
            "phases": {"offpeak": {"poll_interval": 450}},
            "windows": [{"phase": "offpeak", "start": "00:00", "end": "24:00"}],
        }
        cfg, warnings = store.save_config(valid_doc)
        self.assertEqual(cfg.phases["offpeak"].poll_interval, 450)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(store.source, self.path)

        with self.assertRaises(sm.ConfigError):
            store.save_config({"phases": {"offpeak": {"poll_interval": 10}}})
        self.assertEqual(store.get().phases["offpeak"].poll_interval, 450)

        cfg, _ = store.reset_to_default()
        self.assertEqual(cfg.phases["offpeak"].poll_interval, 600)

    def test_store_save_fallback_on_os_error(self):
        store, _ = quiet_store(self.path)
        cache_fallback_dir = os.path.join(self.dir, "cache_fallback")
        os.makedirs(cache_fallback_dir, exist_ok=True)
        from unittest.mock import patch

        orig_makedirs = os.makedirs

        def fake_makedirs(path, exist_ok=True):
            if "read_only" in path:
                raise OSError("Read-only file system")
            return orig_makedirs(path, exist_ok=exist_ok)

        with patch("paths.resolve_cache_dir", return_value=cache_fallback_dir):
            with patch("os.makedirs", side_effect=fake_makedirs):
                cfg, _ = store.save_config(
                    {"version": 1, "default_phase": "offpeak"},
                    target_path=os.path.join(self.dir, "read_only", "schedule.json"),
                )
                self.assertTrue(
                    os.path.exists(os.path.join(cache_fallback_dir, "schedule.json"))
                )
                self.assertEqual(
                    store.path, os.path.join(cache_fallback_dir, "schedule.json")
                )


class TestExampleConfig(unittest.TestCase):
    def test_example_file_is_valid_and_matches_defaults(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "config", "schedule.example.json"
        )
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(sm.parse_config(doc), sm.default_config())


class TestModularStateViews(unittest.TestCase):
    def test_phase_default_views_and_interactive_resolution(self):
        doc = {
            "version": 1,
            "default_phase": "overnight",
            "phases": {
                "overnight": {
                    "presentation": "dormant",
                    "view": "weather",
                    "interaction_view": "morning",
                    "views": ["weather", "morning"],
                },
                "peak": {
                    "presentation": "interactive",
                    "view": "bus_focus",
                    "views": ["bus_focus", "citibike_focus"],
                },
            },
            "windows": [
                {
                    "phase": "peak",
                    "start": "08:00",
                    "end": "10:00",
                    "views": ["bus_focus", "weather"],
                }
            ],
        }
        cfg = sm.parse_config(doc)
        p_overnight = cfg.phases["overnight"]
        self.assertEqual(p_overnight.view, "weather")
        self.assertEqual(p_overnight.interaction_view, "morning")
        self.assertEqual(p_overnight.views, ("weather", "morning"))

        # Test dormant state resolution (midnight outside window)
        dt_night = datetime(2026, 10, 11, 23, 0)
        state_dormant = sm.resolve(cfg, dt_night, interactive=False)
        self.assertEqual(state_dormant.phase, "overnight")
        self.assertEqual(state_dormant.presentation, "dormant")
        self.assertEqual(state_dormant.view, "weather")
        self.assertEqual(state_dormant.views, ("weather", "morning"))
        self.assertEqual(state_dormant.interaction_view, "morning")

        # Test power-button interactive wake resolution
        state_wake = sm.resolve(cfg, dt_night, interactive=True)
        self.assertEqual(state_wake.view, "morning")
        self.assertEqual(state_wake.views, ("weather", "morning"))

        # Test window resolution with views list
        dt_peak = datetime(2026, 10, 11, 8, 30)
        state_peak = sm.resolve(cfg, dt_peak)
        self.assertEqual(state_peak.phase, "peak")
        self.assertEqual(state_peak.view, "bus_focus")
        self.assertEqual(state_peak.views, ("bus_focus", "weather"))

        # Policy header includes views=
        policy_hdr = sm.format_policy_header(state_peak, cfg)
        self.assertIn("views=bus_focus,weather", policy_hdr)

    def test_rejection_of_invalid_views(self):
        bad_doc = {"phases": {"peak": {"view": "illegal view! with spaces"}}}
        with self.assertRaises(sm.ConfigError):
            sm.parse_config(bad_doc)

        bad_doc2 = {"phases": {"peak": {"views": ["not_a_valid_view_name_xyz"]}}}
        with self.assertRaises(sm.ConfigError):
            sm.parse_config(bad_doc2)


if __name__ == "__main__":
    unittest.main()
