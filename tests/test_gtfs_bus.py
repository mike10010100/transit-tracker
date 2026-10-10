"""Unit tests for the NJ Transit GTFS-BUS client (static index + realtime merge)."""

import datetime
import io
import os
import tempfile
import time
import unittest
import zipfile
from unittest.mock import MagicMock, patch

from gtfs_bus import (
    LIVE_MARK,
    SCHED_MARK,
    STATIC_TTL,
    GTFSBusTracker,
    format_clock,
    format_eta,
    hms_to_secs,
)


def make_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WEEKDAY,1,1,1,1,1,0,0,20200101,20301231\n"
            "WEEKEND,0,0,0,0,0,1,1,20200101,20301231\n",
        )
        z.writestr("calendar_dates.txt", "service_id,date,exception_type\n")
        z.writestr(
            "routes.txt",
            "route_id,agency_id,route_short_name,route_long_name,route_type\n"
            "126,NJB,126,Hoboken - New York,3\n",
        )
        z.writestr(
            "trips.txt",
            "trip_id,route_id,service_id,trip_headsign,direction_id,block_id,shape_id\n"
            "A,126,WEEKDAY,126 NEW YORK,0,,\n"
            "B,126,WEEKDAY,126 NEW YORK,0,,\n"  # duplicate departure (other service variant)
            "C,126,WEEKEND,126 NEW YORK,0,,\n"
            "D,999,WEEKDAY,OTHER ROUTE,0,,\n",  # different route, must be ignored
        )
        z.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
            "A,08:00:00,08:00:00,20512,1,,\n"
            "B,08:00:00,08:00:00,20512,1,,\n"
            "C,09:00:00,09:00:00,20512,1,,\n"
            "D,09:00:00,09:00:00,20512,1,,\n",
        )
    return buf.getvalue()


def build_tracker():
    t = GTFSBusTracker(route="126", stops=["20512"], cache_dir="/tmp/nonexistent")
    t._index = t.build_index(make_zip())
    return t


def eta_minutes(eta_str):
    # "in 7 mins (5:54 PM)" -> 7
    return int(eta_str.split("in ", 1)[1].split(" mins")[0])


class TestParsingHelpers(unittest.TestCase):
    def test_hms_to_secs(self):
        self.assertEqual(hms_to_secs("08:00:00"), 8 * 3600)
        self.assertEqual(hms_to_secs("25:30:00"), 25 * 3600 + 30 * 60)
        self.assertEqual(hms_to_secs("bad"), -1)

    def test_format_eta_rounds_and_marks(self):
        now = 1_000_000.0
        live = format_eta(now + 7 * 60 + 20, now, live=True)
        self.assertTrue(live.startswith(LIVE_MARK))
        self.assertEqual(eta_minutes(live), 7)
        sched = format_eta(now + 7 * 60 + 20, now, live=False)
        self.assertTrue(sched.startswith(SCHED_MARK))
        self.assertEqual(eta_minutes(sched), 7)

    def test_format_clock_not_empty(self):
        self.assertIn(":", format_clock(1_000_000.0))

    def test_format_eta_negative_minutes(self):
        now = 1_000_000.0
        past = format_eta(now - 100, now, live=False)
        self.assertTrue(past.startswith(SCHED_MARK))
        self.assertEqual(eta_minutes(past), 0)


class TestBuildIndex(unittest.TestCase):
    def test_index_contents(self):
        t = build_tracker()
        self.assertEqual(
            set(t._index["trips"].keys()), {"A", "B", "C"}
        )  # route 126 only
        self.assertEqual(len(t._index["deps"]["20512"]), 3)  # A, B, C (not D)
        self.assertGreater(len(t._index["services"]), 0)
        self.assertEqual(t._index["valid_until"], "20301231")

    def test_active_services_weekday_vs_weekend(self):
        t = build_tracker()
        monday = datetime.date(2026, 10, 12)
        saturday = datetime.date(2026, 10, 10)
        self.assertIn("WEEKDAY", t.active_services(t._index, monday))
        self.assertNotIn("WEEKEND", t.active_services(t._index, monday))
        self.assertIn("WEEKEND", t.active_services(t._index, saturday))

    def test_active_services_exception_removes(self):
        t = build_tracker()
        t._index["exceptions"] = {"20261012": {"WEEKDAY": 2}}
        monday = datetime.date(2026, 10, 12)
        self.assertNotIn("WEEKDAY", t.active_services(t._index, monday))

    def test_active_services_exception_adds(self):
        t = build_tracker()
        t._index["exceptions"] = {"20261012": {"HOLIDAY_SVC": 1}}
        monday = datetime.date(2026, 10, 12)
        self.assertIn("HOLIDAY_SVC", t.active_services(t._index, monday))

    def test_build_index_without_calendar_dates(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(
                "calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                "WEEKDAY,1,1,1,1,1,0,0,20200101,20301231\n",
            )
            z.writestr(
                "routes.txt",
                "route_id,agency_id,route_short_name,route_long_name,route_type\n"
                "126,NJB,126,Hoboken - New York,3\n",
            )
            z.writestr(
                "trips.txt",
                "trip_id,route_id,service_id,trip_headsign,direction_id,block_id,shape_id\n"
                "A,126,WEEKDAY,126 NEW YORK,0,,\n",
            )
            z.writestr(
                "stop_times.txt",
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
                "A,08:00:00,08:00:00,20512,1,,\n",
            )
        t = GTFSBusTracker(route="126", stops=["20512"], cache_dir="/tmp/nonexistent")
        idx = t.build_index(buf.getvalue())
        self.assertEqual(idx["exceptions"], {})
        self.assertIn("A", idx["trips"])


class TestGetUpcoming(unittest.TestCase):
    def test_static_only_with_dedup_and_window(self):
        t = build_tracker()
        # Monday 07:30 -> A/B (08:00) dedup to one; C (WEEKEND) excluded; D route ignored.
        now = datetime.datetime(2026, 10, 12, 7, 30)
        out = t.get_upcoming("20512", limit=5, allow_realtime=False, now=now)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["live"])
        self.assertEqual(out[0]["destination"], "126 NEW YORK")
        self.assertLessEqual(eta_minutes(out[0]["eta"]), 31)

    def test_window_excludes_far_future(self):
        t = build_tracker()
        now = datetime.datetime(2026, 10, 12, 6, 0)  # 08:00 is 2h away -> in window
        self.assertEqual(len(t.get_upcoming("20512", allow_realtime=False, now=now)), 1)
        now2 = datetime.datetime(2026, 10, 12, 4, 0)  # 08:00 is 4h away -> out
        self.assertEqual(
            len(t.get_upcoming("20512", allow_realtime=False, now=now2)), 0
        )

    def test_realtime_overrides_schedule_and_marks_live(self):
        t = build_tracker()
        now = datetime.datetime(2026, 10, 12, 7, 30)
        base = datetime.datetime(2026, 10, 12, 8, 0).timestamp()
        # A predicts 10 minutes late; B has no realtime, but A should win the dedup.
        t.fetch_realtime = MagicMock(
            return_value={
                "A": {"20512": {"time": base + 600, "delay": 600, "vehicle_id": "v9"}},
            }
        )
        t.fetch_occupancy = MagicMock(
            return_value={
                "A": {"occupancy": "FEW_SEATS_AVAILABLE", "vehicle_id": "v9"},
            }
        )
        out = t.get_upcoming("20512", limit=5, allow_realtime=True, now=now)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["live"])
        self.assertEqual(out[0]["vehicle_id"], "v9")
        self.assertEqual(out[0]["occupancy"], "FEW_SEATS_AVAILABLE")
        self.assertTrue(out[0]["eta"].startswith(LIVE_MARK))
        # 08:10 vs now 07:30 -> ~40 mins
        self.assertIn(eta_minutes(out[0]["eta"]), (39, 40, 41))

    def test_realtime_unavailable_falls_back_to_schedule(self):
        t = build_tracker()
        t.fetch_realtime = MagicMock(return_value={})
        t.fetch_occupancy = MagicMock(return_value={})
        now = datetime.datetime(2026, 10, 12, 7, 30)
        out = t.get_upcoming("20512", limit=5, now=now)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["live"])
        self.assertIsNone(out[0]["occupancy"])
        self.assertTrue(out[0]["eta"].startswith(SCHED_MARK))

    def test_limit_respected(self):
        t = build_tracker()
        now = datetime.datetime(2026, 10, 12, 7, 30)
        out = t.get_upcoming("20512", limit=1, allow_realtime=False, now=now)
        self.assertEqual(len(out), 1)

    def test_no_index_returns_empty(self):
        t = GTFSBusTracker(route="126", stops=["20512"], cache_dir="/tmp/nonexistent")
        t.ensure_index = MagicMock(return_value=None)
        self.assertEqual(t.get_upcoming("20512"), [])


class TestFetchOccupancy(unittest.TestCase):
    def test_maps_vehicle_occupancy_and_filters_to_known_trips(self):
        from google.transit import gtfs_realtime_pb2

        t = build_tracker()  # knows trips A, B, C
        fm = gtfs_realtime_pb2.FeedMessage()
        fm.header.gtfs_realtime_version = "2.0"
        e1 = fm.entity.add()
        e1.id = "0"
        e1.vehicle.trip.trip_id = "A"
        e1.vehicle.vehicle.id = "v1"
        e1.vehicle.occupancy_status = 2  # FEW_SEATS_AVAILABLE
        e2 = fm.entity.add()
        e2.id = "1"
        e2.vehicle.trip.trip_id = "ZZZ"  # not our route -> filtered out
        e2.vehicle.vehicle.id = "v2"
        e2.vehicle.occupancy_status = 1
        t.get_token = MagicMock(return_value="tok")
        t.session.get = MagicMock(
            return_value=MagicMock(
                raise_for_status=MagicMock(), content=fm.SerializeToString()
            )
        )

        out = t.fetch_occupancy()
        self.assertEqual(out["A"]["occupancy"], "FEW_SEATS_AVAILABLE")
        self.assertEqual(out["A"]["vehicle_id"], "v1")
        self.assertNotIn("ZZZ", out)

    def test_fetch_errors_return_empty(self):
        t = build_tracker()
        t.get_token = MagicMock(side_effect=Exception("auth down"))
        self.assertEqual(t.fetch_occupancy(), {})

    def test_no_data_occupancy_is_none(self):
        from google.transit import gtfs_realtime_pb2

        t = build_tracker()
        fm = gtfs_realtime_pb2.FeedMessage()
        fm.header.gtfs_realtime_version = "2.0"
        e = fm.entity.add()
        e.id = "0"
        e.vehicle.trip.trip_id = "A"
        e.vehicle.occupancy_status = 7  # NO_DATA_AVAILABLE
        t.get_token = MagicMock(return_value="tok")
        t.session.get = MagicMock(
            return_value=MagicMock(
                raise_for_status=MagicMock(), content=fm.SerializeToString()
            )
        )
        self.assertIsNone(t.fetch_occupancy()["A"]["occupancy"])


class TestAuth(unittest.TestCase):
    def test_missing_credentials_raises(self):
        with patch.dict(
            "os.environ",
            {
                "NJT_USERNAME": "",
                "NJT_PASSWORD": "",
                "NJT_API_USERNAME": "",
                "NJT_API_PASSWORD": "",
            },
        ):
            t = GTFSBusTracker(username="", password="", route="126", stops=["20512"])
            with self.assertRaises(ValueError):
                t.get_token()

    def test_auth_mints_token(self):
        t = GTFSBusTracker(username="u", password="p", route="126", stops=["20512"])
        t.session.post = MagicMock(
            return_value=MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(
                    return_value={"Authenticated": "True", "UserToken": "tok"}
                ),
            )
        )
        self.assertEqual(t.get_token(), "tok")
        self.assertGreater(t.token_expiry, 0)

    def test_failed_auth_raises(self):
        t = GTFSBusTracker(username="u", password="p", route="126", stops=["20512"])
        t.session.post = MagicMock(
            return_value=MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={"Authenticated": "False"}),
            )
        )
        with self.assertRaises(RuntimeError):
            t.get_token()

    def test_get_token_cached(self):
        t = GTFSBusTracker(username="u", password="p", route="126", stops=["20512"])
        t.token = "cached_token_xyz"
        t.token_expiry = time.time() + 1000
        self.assertEqual(t.get_token(), "cached_token_xyz")

    def test_url_and_path_properties(self):
        t = GTFSBusTracker(
            base_url="https://api.example.com", cache_dir="/var/cache/gtfs"
        )
        self.assertEqual(
            t.auth_url, "https://api.example.com/api/GTFSG2/authenticateUser"
        )
        self.assertEqual(t.static_url, "https://api.example.com/api/GTFSG2/getGTFS")
        self.assertEqual(
            t.trips_url, "https://api.example.com/api/GTFSG2/getTripUpdates"
        )
        self.assertEqual(
            t.vehicles_url, "https://api.example.com/api/GTFSG2/getVehiclePositions"
        )
        self.assertEqual(t._index_path, "/var/cache/gtfs/gtfs_index.json")


class TestIndexCachingAndLifecycle(unittest.TestCase):
    def test_index_is_fresh(self):
        t = GTFSBusTracker(route="126", stops=["20512"])
        self.assertFalse(t._index_is_fresh({}))
        self.assertFalse(t._index_is_fresh({"route": "other"}))
        self.assertFalse(
            t._index_is_fresh(
                {"route": "126", "built_at": time.time() - STATIC_TTL - 10}
            )
        )
        self.assertFalse(
            t._index_is_fresh(
                {
                    "route": "126",
                    "built_at": time.time(),
                    "valid_until": "20200101",
                }
            )
        )
        self.assertTrue(
            t._index_is_fresh(
                {
                    "route": "126",
                    "built_at": time.time(),
                    "valid_until": "20351231",
                }
            )
        )

    def test_load_and_save_cached_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            t = GTFSBusTracker(route="126", stops=["20512"], cache_dir=tmpdir)
            idx = {
                "route": "126",
                "stops": ["20512"],
                "built_at": time.time(),
                "valid_until": "20351231",
                "trips": {"A": {}},
            }
            t._save_index(idx)
            loaded = t._load_cached_index()
            self.assertEqual(loaded, idx)

            with open(t._index_path, "w", encoding="utf-8") as f:
                f.write("not-json")
            self.assertIsNone(t._load_cached_index())

            bad_tracker = GTFSBusTracker(
                route="126", stops=["20512"], cache_dir="/dev/null/notdir"
            )
            bad_tracker._save_index(idx)

    def test_ensure_index_branches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            t = GTFSBusTracker(route="126", stops=["20512"], cache_dir=tmpdir)
            t._index = {"mock": True}
            self.assertEqual(t.ensure_index(), {"mock": True})
            t._index = None

            idx = {
                "route": "126",
                "stops": ["20512"],
                "built_at": time.time(),
                "valid_until": "20351231",
                "trips": {"A": {}},
            }
            t._save_index(idx)
            self.assertEqual(t.ensure_index(), idx)
            self.assertEqual(t._index, idx)

            t2 = GTFSBusTracker(
                route="126",
                stops=["20512"],
                cache_dir=os.path.join(tmpdir, "new_cache"),
            )
            t2.get_token = MagicMock(return_value="tok")
            zip_bytes = make_zip()
            t2.session.get = MagicMock(
                return_value=MagicMock(
                    raise_for_status=MagicMock(),
                    content=zip_bytes,
                )
            )
            # When cold and wait=False, ensure_index dispatches background rebuild and returns None
            self.assertIsNone(t2.ensure_index(wait=False))

            # When wait=True, ensure_index rebuilds synchronously and returns the index
            res = t2.ensure_index(wait=True)
            self.assertIsNotNone(res)
            self.assertEqual(res["route"], "126")
            self.assertIn("A", res["trips"])

            t3 = GTFSBusTracker(
                route="126",
                stops=["20512"],
                cache_dir=os.path.join(tmpdir, "err_cache"),
            )
            t3.get_token = MagicMock(side_effect=Exception("network error"))
            self.assertIsNone(t3.ensure_index(wait=True))


class TestRealtimeFetching(unittest.TestCase):
    def test_fetch_realtime_full_protobuf(self):
        from google.transit import gtfs_realtime_pb2

        t = build_tracker()  # known trips A, B, C; stops 20512
        fm = gtfs_realtime_pb2.FeedMessage()
        fm.header.gtfs_realtime_version = "2.0"

        # Entity 0: no trip update
        e0 = fm.entity.add()
        e0.id = "e0"

        # Entity 1: unknown trip
        e1 = fm.entity.add()
        e1.id = "e1"
        e1.trip_update.trip.trip_id = "UNKNOWN_TRIP"

        # Entity 2: trip A, vehicle v1, departure update
        e2 = fm.entity.add()
        e2.id = "e2"
        e2.trip_update.trip.trip_id = "A"
        e2.trip_update.vehicle.id = "v1"

        stu_ignored = e2.trip_update.stop_time_update.add()
        stu_ignored.stop_id = "OTHER_STOP"

        stu_dep = e2.trip_update.stop_time_update.add()
        stu_dep.stop_id = "20512"
        stu_dep.departure.time = 1700000000
        stu_dep.departure.delay = 120

        # Entity 3: trip B, no vehicle, arrival update
        e3 = fm.entity.add()
        e3.id = "e3"
        e3.trip_update.trip.trip_id = "B"
        stu_arr = e3.trip_update.stop_time_update.add()
        stu_arr.stop_id = "20512"
        stu_arr.arrival.time = 1700000500
        stu_arr.arrival.delay = 60

        t.get_token = MagicMock(return_value="tok")
        t.session.get = MagicMock(
            return_value=MagicMock(
                raise_for_status=MagicMock(),
                content=fm.SerializeToString(),
            )
        )

        res = t.fetch_realtime()
        self.assertIn("A", res)
        self.assertEqual(
            res["A"]["20512"], {"time": 1700000000, "delay": 120, "vehicle_id": "v1"}
        )
        self.assertIn("B", res)
        self.assertEqual(
            res["B"]["20512"], {"time": 1700000500, "delay": 60, "vehicle_id": None}
        )
        self.assertNotIn("UNKNOWN_TRIP", res)

    def test_fetch_realtime_disabled_and_error(self):
        t = build_tracker()
        with patch("gtfs_bus._REALTIME_AVAILABLE", False):
            self.assertEqual(t.fetch_realtime(), {})

        t.get_token = MagicMock(side_effect=Exception("connection failed"))
        self.assertEqual(t.fetch_realtime(), {})

    def test_fetch_occupancy_disabled_and_no_vehicle(self):
        from google.transit import gtfs_realtime_pb2

        t = build_tracker()
        with patch("gtfs_bus._REALTIME_AVAILABLE", False):
            self.assertEqual(t.fetch_occupancy(), {})

        fm = gtfs_realtime_pb2.FeedMessage()
        fm.header.gtfs_realtime_version = "2.0"
        e = fm.entity.add()
        e.id = "e0"
        # No vehicle field
        t.get_token = MagicMock(return_value="tok")
        t.session.get = MagicMock(
            return_value=MagicMock(
                raise_for_status=MagicMock(),
                content=fm.SerializeToString(),
            )
        )
        self.assertEqual(t.fetch_occupancy(), {})

    def test_get_upcoming_in_memory_realtime_cache(self):
        t = build_tracker()
        now = datetime.datetime(2026, 10, 12, 7, 30)
        t.fetch_realtime = MagicMock(return_value={})
        t.fetch_occupancy = MagicMock(return_value={})

        t.get_upcoming("20512", allow_realtime=True, now=now)
        self.assertEqual(t.fetch_realtime.call_count, 1)

        t.get_upcoming("20512", allow_realtime=True, now=now)
        # Second call within 15s must reuse cache
        self.assertEqual(t.fetch_realtime.call_count, 1)


if __name__ == "__main__":
    unittest.main()
