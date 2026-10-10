"""
NJ Transit GTFS-BUS client.

Unlike the BUSDATA/DepartureVision product (which can be blocked by a backend
provisioning issue), the GTFS-G2 endpoints work for any account subscribed to
GTFS-BUS. We use two of them:

  * getGTFS        -> the static schedule (a zip of standard GTFS CSVs). This is
                      the source of the FULL list of upcoming departures, not
                      just the one active vehicle the public website returns.
  * getTripUpdates -> GTFS-Realtime (protobuf) trip predictions, merged onto the
                      static schedule by (trip_id, stop_id) for live ETAs.

The static feed only covers a short rolling window (a handful of days), so it is
re-downloaded periodically; the derived, small index is cached to disk so a
restart does not re-parse the whole state's stop_times.txt.
"""

import csv
import datetime
import io
import json
import os
import threading
import time
import zipfile
from typing import Any, Optional

import requests
from logsafe import redact
from paths import resolve_cache_dir

try:
    from google.transit import gtfs_realtime_pb2

    _REALTIME_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only when the dep is missing
    _REALTIME_AVAILABLE = False

_DAY_KEYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

# GTFS-Realtime OccupancyStatus enum -> the string the renderer understands
# (it title-cases "FEW_SEATS_AVAILABLE" -> "Few Seats Available"). NO_DATA -> None.
_OCCUPANCY = {
    0: "EMPTY",
    1: "MANY_SEATS_AVAILABLE",
    2: "FEW_SEATS_AVAILABLE",
    3: "STANDING_ROOM_ONLY",
    4: "CRUSHED_STANDING_ROOM_ONLY",
    5: "FULL",
    6: "NOT_ACCEPTING_PASSENGERS",
    7: None,
}

# Live/scheduled eta markers (DejaVu Sans has both glyphs; the renderer already
# uses ● separators and → arrows).
LIVE_MARK = "\u25cf "  # filled circle
SCHED_MARK = "\u25cb "  # hollow circle

# How often the static schedule is re-downloaded. The upstream window is only a
# few days, so refresh daily.
STATIC_TTL = 20 * 3600
# Keep only departures within this window of "now" (a little slack behind, an
# hour or two ahead) -- enough for a commute board.
LOOKBACK_SECS = 120
LOOKAHEAD_SECS = 3 * 3600
# After a failed static download, don't retry for this long (the download is
# ~55 MB with a 180 s timeout; retrying per request would tie up threads).
NEGATIVE_TTL = 15 * 60
# Realtime feeds are reused for this long across stops/requests.
REALTIME_TTL = 15


def hms_to_secs(value: str) -> int:
    """Parses a GTFS HH:MM:SS time (may exceed 24:00:00) into seconds."""
    try:
        h, m, s = (int(x) for x in value.strip().split(":"))
        return h * 3600 + m * 60 + s
    except Exception:
        return -1


def service_day_origin(
    service_date: datetime.date, tz: Optional[datetime.tzinfo] = None
) -> float:
    """
    Unix time that GTFS stop times of `service_date` are measured from:
    "noon minus 12h" in local time (per the GTFS spec). This differs from
    local midnight by an hour on DST-change days. tz=None means the server's
    local timezone.
    """
    noon = datetime.datetime(
        service_date.year, service_date.month, service_date.day, 12, 0, tzinfo=tz
    )
    return noon.timestamp() - 12 * 3600


def format_clock(epoch: float) -> str:
    """Formats a unix epoch as a local 'H:MM AM/PM' clock string."""
    dt = datetime.datetime.fromtimestamp(epoch)
    return dt.strftime("%I:%M %p").lstrip("0")


def format_eta(epoch: float, now: float, live: bool = False) -> str:
    """
    Human ETA like '● in 5 mins (5:35 PM)' (live, real-time) or
    '○ in 9 mins (5:39 PM)' (scheduled). The leading glyph is the live/scheduled
    marker; the renderer's parse_minutes regex still finds the minute count.
    """
    mins = int(round((epoch - now) / 60.0))
    if mins < 0:
        mins = 0
    return f"{LIVE_MARK if live else SCHED_MARK}in {mins} mins ({format_clock(epoch)})"


class GTFSBusTracker:
    """Fetches and merges NJ Transit GTFS-BUS static schedule + realtime."""

    BASE_URL = os.environ.get(
        "NJT_GTFS_BASE_URL", "https://pcsdata.njtransit.com"
    ).rstrip("/")
    AUTH_PATH = "/api/GTFSG2/authenticateUser"
    STATIC_PATH = "/api/GTFSG2/getGTFS"
    TRIPS_PATH = "/api/GTFSG2/getTripUpdates"
    VEHICLES_PATH = "/api/GTFSG2/getVehiclePositions"

    def __init__(
        self,
        route: str = "126",
        stops: Optional[list[str]] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        base_url: Optional[str] = None,
        cache_dir: Optional[str] = None,
        session: Optional[requests.Session] = None,
        tz: Optional[datetime.tzinfo] = None,
    ):
        raw_user = (
            username
            or os.environ.get("NJT_USERNAME")
            or os.environ.get("NJT_API_USERNAME")
            or ""
        )
        self.username = raw_user.split("@")[0] if "@" in raw_user else raw_user
        self.password = (
            password
            or os.environ.get("NJT_PASSWORD")
            or os.environ.get("NJT_API_PASSWORD")
            or ""
        )
        self.route = route
        self.stops = list(stops or [])
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.cache_dir = cache_dir or resolve_cache_dir()
        self.session = session or requests.Session()
        self.tz = tz  # None = server local time

        self.token: Optional[str] = None
        self.token_expiry: float = 0
        self._index: Optional[dict[str, Any]] = None
        self._realtime: Optional[dict[str, dict[str, Any]]] = None
        self._realtime_at: float = 0
        self._occupancy: Optional[dict[str, dict[str, Any]]] = None
        self._occupancy_at: float = 0

        # Thread-safety: request threads, the warm-up thread and the background
        # rebuild thread share this object.
        self._session_lock = threading.RLock()  # requests.Session use
        self._token_lock = threading.Lock()  # token refresh
        self._state_lock = threading.Lock()  # _index / _last_failure / _build_thread
        self._build_lock = threading.Lock()  # one static rebuild at a time
        self._realtime_lock = threading.Lock()  # one realtime refresh at a time
        self._build_thread: Optional[threading.Thread] = None
        self._last_failure: float = 0.0

    @property
    def auth_url(self) -> str:
        return self.base_url + self.AUTH_PATH

    @property
    def static_url(self) -> str:
        return self.base_url + self.STATIC_PATH

    @property
    def trips_url(self) -> str:
        return self.base_url + self.TRIPS_PATH

    @property
    def vehicles_url(self) -> str:
        return self.base_url + self.VEHICLES_PATH

    @property
    def _index_path(self) -> str:
        return os.path.join(self.cache_dir, "gtfs_index.json")

    def get_token(self) -> str:
        with self._token_lock:
            if self.token and time.time() < self.token_expiry:
                return self.token
            if not self.username or not self.password:
                raise ValueError(
                    "Missing NJ Transit credentials (NJT_USERNAME/NJT_PASSWORD)."
                )
            with self._session_lock:
                resp = self.session.post(
                    self.auth_url,
                    data={"username": self.username, "password": self.password},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
            if str(data.get("Authenticated")).lower() == "true" and data.get(
                "UserToken"
            ):
                self.token = data["UserToken"]
                self.token_expiry = time.time() + 82800  # 23h
                return self.token
            raise RuntimeError(f"NJ Transit GTFS auth failed: {redact(data)}")

    def _index_is_fresh(self, index: dict[str, Any]) -> bool:
        if not index or index.get("route") != self.route:
            return False
        if time.time() - index.get("built_at", 0) > STATIC_TTL:
            return False
        valid_until = index.get("valid_until", "")
        return (
            bool(valid_until)
            and datetime.date.today().strftime("%Y%m%d") <= valid_until
        )

    def _load_cached_index(self) -> Optional[dict[str, Any]]:
        try:
            with open(self._index_path, encoding="utf-8") as f:
                index = json.load(f)
            if index.get("stops") == self.stops and self._index_is_fresh(index):
                return index
        except Exception:
            pass
        return None

    def _save_index(self, index: dict[str, Any]) -> None:
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = self._index_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(index, f)
            os.replace(tmp, self._index_path)
        except Exception:
            pass

    def _current_index(self) -> Optional[dict[str, Any]]:
        with self._state_lock:
            return self._index

    def _negative_cached(self) -> bool:
        with self._state_lock:
            return (
                bool(self._last_failure)
                and time.time() - self._last_failure < NEGATIVE_TTL
            )

    def _download_static(self) -> bytes:
        """
        Downloads the static GTFS zip using the tracker session.
        """
        token = self.get_token()
        with self._session_lock:
            resp = self.session.get(
                self.static_url, params={"token": token}, timeout=180
            )
            resp.raise_for_status()
            return resp.content

    def rebuild_index(self) -> Optional[dict[str, Any]]:
        """
        Downloads and rebuilds the static index, single-flight: concurrent
        callers wait for the in-progress build and then reuse its result.
        Failures are negative-cached for NEGATIVE_TTL. Returns the current
        index (possibly the old one if the rebuild failed).
        """
        with self._build_lock:
            current = self._current_index()
            if current is not None and self._index_is_fresh(current):
                return current
            if self._negative_cached():
                return current
            try:
                index = self.build_index(self._download_static())
            except Exception as e:
                with self._state_lock:
                    self._last_failure = time.time()
                print(
                    f"[GTFS] static schedule unavailable ({redact(e)}); retrying in {NEGATIVE_TTL // 60} min"
                )
                return current
            with self._state_lock:
                self._index = index
                self._last_failure = 0.0
            self._save_index(index)
            return index

    def _start_background_rebuild(self) -> None:
        if self._negative_cached():
            return
        with self._state_lock:
            if self._build_thread is not None and self._build_thread.is_alive():
                return
            self._build_thread = threading.Thread(
                target=self.rebuild_index, daemon=True, name="GTFSRebuild"
            )
            self._build_thread.start()

    def ensure_index(self, wait: bool = False) -> Optional[dict[str, Any]]:
        """
        Returns the static index, checking freshness (STATIC_TTL and
        valid_until) on every call. A stale index keeps being served while a
        single background rebuild runs; with no index at all the disk cache is
        tried first. wait=True rebuilds synchronously (used by the warm-up
        thread) instead of in the background.
        """
        current = self._current_index()
        if current is not None and self._index_is_fresh(current):
            return current
        if current is None:
            cached = self._load_cached_index()
            if cached is not None:
                with self._state_lock:
                    self._index = cached
                return cached
            if wait:
                return self.rebuild_index()
            self._start_background_rebuild()
            return None
        if wait:
            return self.rebuild_index()
        self._start_background_rebuild()
        return self._current_index()

    def build_index(self, zip_bytes: bytes) -> dict[str, Any]:
        """Parses the static GTFS zip into a small index for this route/stops."""
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            services: dict[str, Any] = {}
            with z.open("calendar.txt") as f:
                for r in csv.DictReader(io.TextIOWrapper(f, "utf-8")):
                    services[r["service_id"]] = {
                        "days": [r[k] for k in _DAY_KEYS],
                        "start": r["start_date"],
                        "end": r["end_date"],
                    }

            exceptions: dict[str, dict[str, int]] = {}
            try:
                with z.open("calendar_dates.txt") as f:
                    for r in csv.DictReader(io.TextIOWrapper(f, "utf-8")):
                        exceptions.setdefault(r["date"], {})[r["service_id"]] = int(
                            r["exception_type"]
                        )
            except KeyError:
                pass

            route_ids = set()
            with z.open("routes.txt") as f:
                for r in csv.DictReader(io.TextIOWrapper(f, "utf-8")):
                    if r.get("route_short_name") == self.route:
                        route_ids.add(r["route_id"])

            trips: dict[str, Any] = {}
            with z.open("trips.txt") as f:
                for r in csv.DictReader(io.TextIOWrapper(f, "utf-8")):
                    if r["route_id"] in route_ids:
                        trips[r["trip_id"]] = {
                            "headsign": (r.get("trip_headsign") or "").strip(),
                            "service_id": r.get("service_id", ""),
                            "direction": r.get("direction_id", ""),
                        }

            deps: dict[str, list[dict[str, Any]]] = {sid: [] for sid in self.stops}
            with z.open("stop_times.txt") as f:
                for r in csv.DictReader(io.TextIOWrapper(f, "utf-8")):
                    tid = r["trip_id"]
                    sid = r["stop_id"]
                    if tid in trips and sid in deps:
                        secs = hms_to_secs(
                            r.get("departure_time") or r.get("arrival_time") or ""
                        )
                        if secs >= 0:
                            deps[sid].append({"trip_id": tid, "secs": secs})
            for sid in deps:
                deps[sid].sort(key=lambda d: d["secs"])

            valid_until = max((s["end"] for s in services.values()), default="")
            return {
                "route": self.route,
                "stops": self.stops,
                "built_at": time.time(),
                "valid_until": valid_until,
                "trips": trips,
                "deps": deps,
                "services": services,
                "exceptions": exceptions,
            }

    def active_services(self, index: dict[str, Any], date: datetime.date) -> set:
        ymd = date.strftime("%Y%m%d")
        dow = date.weekday()  # 0 = Monday
        active = set()
        for sid, s in index["services"].items():
            if s["start"] <= ymd <= s["end"] and s["days"][dow] == "1":
                active.add(sid)
        for sid, typ in index["exceptions"].get(ymd, {}).items():
            if typ == 1:
                active.add(sid)
            elif typ == 2:
                active.discard(sid)
        return active

    def fetch_realtime(self) -> dict[str, dict[str, Any]]:
        """Fetches GTFS-RT trip updates, keyed trip_id -> stop_id -> {time,delay,vehicle_id}."""
        index = self._current_index() or {}
        known = index.get("trips", {})
        out: dict[str, dict[str, Any]] = {}
        if not _REALTIME_AVAILABLE:
            print("[GTFS] gtfs-realtime-bindings unavailable; skipping realtime.")
            return out
        try:
            token = self.get_token()
            with self._session_lock:
                resp = self.session.get(
                    self.trips_url, params={"token": token}, timeout=60
                )
                resp.raise_for_status()
                content = resp.content
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(content)
        except Exception as e:
            print(f"[GTFS] realtime unavailable ({redact(e)})")
            return out

        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue
            tu = entity.trip_update
            tid = tu.trip.trip_id
            if known and tid not in known:
                continue
            vid = tu.vehicle.id if tu.HasField("vehicle") and tu.vehicle.id else None
            for stu in tu.stop_time_update:
                sid = stu.stop_id
                if self.stops and sid not in self.stops:
                    continue
                when = None
                delay = None
                if stu.HasField("departure") and stu.departure.HasField("time"):
                    when = stu.departure.time
                    if stu.departure.HasField("delay"):
                        delay = stu.departure.delay
                elif stu.HasField("arrival") and stu.arrival.HasField("time"):
                    when = stu.arrival.time
                    if stu.arrival.HasField("delay"):
                        delay = stu.arrival.delay
                out.setdefault(tid, {})[sid] = {
                    "time": when,
                    "delay": delay,
                    "vehicle_id": vid,
                }
        return out

    def fetch_occupancy(self) -> dict[str, dict[str, Any]]:
        """
        Fetches GTFS-RT vehicle positions (small, ~86 KB) and returns
        trip_id -> {occupancy, vehicle_id}. The trip-update feed carries no
        occupancy, but the vehicle feed does.
        """
        out: dict[str, dict[str, Any]] = {}
        if not _REALTIME_AVAILABLE:
            return out
        known = (self._current_index() or {}).get("trips", {})
        try:
            token = self.get_token()
            with self._session_lock:
                resp = self.session.get(
                    self.vehicles_url, params={"token": token}, timeout=30
                )
                resp.raise_for_status()
                content = resp.content
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(content)
        except Exception as e:
            print(f"[GTFS] vehicle positions unavailable ({redact(e)})")
            return out
        for entity in feed.entity:
            if not entity.HasField("vehicle"):
                continue
            v = entity.vehicle
            tid = v.trip.trip_id
            if not tid or (known and tid not in known):
                continue
            out[tid] = {
                "occupancy": _OCCUPANCY.get(v.occupancy_status),
                "vehicle_id": v.vehicle.id or None,
            }
        return out

    def get_upcoming(
        self,
        stop_id: str,
        limit: int = 3,
        allow_realtime: bool = True,
        now: Optional[datetime.datetime] = None,
    ) -> list[dict[str, Any]]:
        """
        Returns the next `limit` upcoming departures for a stop, merging the
        static schedule with realtime predictions. Each item match the canonical
        Arrival shape the renderer consumes.
        """
        index = self.ensure_index()
        if not index:
            return []
        now = now or datetime.datetime.now()
        now_epoch = now.timestamp()
        tz = now.tzinfo or self.tz
        today = now.astimezone(tz).date() if tz is not None else now.date()

        realtime: dict[str, dict[str, Any]] = {}
        occupancy: dict[str, dict[str, Any]] = {}
        if allow_realtime:
            realtime, occupancy = self._get_realtime()

        # Evaluate yesterday's service day too: GTFS times >= 24:00:00 belong
        # to the previous service day (e.g. 25:10 = 01:10 tomorrow), so between
        # midnight and ~03:00 they would otherwise vanish. Times are measured
        # from "noon minus 12h" of the service date, which is DST-correct.
        service_days = []
        for service_date in (today - datetime.timedelta(days=1), today):
            service_days.append(
                (
                    self.active_services(index, service_date),
                    service_day_origin(service_date, tz),
                )
            )

        # Dedup by the *scheduled* (minute, headsign, direction): the static feed
        # lists the same physical departure under multiple trip_ids (service-day
        # variants), and realtime can shift one copy's time, so keying on the
        # scheduled time collapses them. Prefer the copy that has realtime data.
        by_key: dict[tuple, dict[str, Any]] = {}
        for active, origin in service_days:
            for d in index["deps"].get(stop_id, []):
                trip = index["trips"].get(d["trip_id"])
                if not trip or trip["service_id"] not in active:
                    continue
                sched_epoch = origin + d["secs"]
                if (
                    sched_epoch < now_epoch - LOOKBACK_SECS
                    or sched_epoch > now_epoch + LOOKAHEAD_SECS
                ):
                    continue
                pred = (
                    (realtime.get(d["trip_id"]) or {}).get(stop_id)
                    if realtime
                    else None
                )
                veh = occupancy.get(d["trip_id"]) or {}
                if pred and pred.get("time"):
                    epoch, live = float(pred["time"]), True
                else:
                    epoch, live = sched_epoch, False
                key = (int(sched_epoch // 60), trip["headsign"], trip["direction"])
                row = {
                    "trip_id": d["trip_id"],
                    "epoch": epoch,
                    "live": live,
                    "direction": trip["direction"],
                    "headsign": trip["headsign"],
                    "vehicle_id": (pred or {}).get("vehicle_id")
                    or veh.get("vehicle_id"),
                    "occupancy": veh.get("occupancy"),
                }
                if key not in by_key or (live and not by_key[key]["live"]):
                    by_key[key] = row

        rows = sorted(by_key.values(), key=lambda r: r["epoch"])
        result: list[dict[str, Any]] = []
        for r in rows:
            result.append(
                {
                    "route": self.route,
                    "destination": r["headsign"] or f"{self.route} bus",
                    "eta": format_eta(r["epoch"], now_epoch, live=r["live"]),
                    "epoch": r["epoch"],
                    "occupancy": r.get("occupancy"),
                    "vehicle_id": r["vehicle_id"],
                    "live": r["live"],
                }
            )
            if len(result) >= limit:
                break
        return result

    def _get_realtime(self) -> tuple:
        """Returns (trip updates, occupancy), refreshed at most every REALTIME_TTL (single-flight)."""
        with self._realtime_lock:
            if (
                self._realtime is not None
                and time.time() - self._realtime_at < REALTIME_TTL
            ):
                return self._realtime, self._occupancy or {}
            realtime = self.fetch_realtime()
            occupancy = self.fetch_occupancy()
            self._realtime = realtime
            self._realtime_at = time.time()
            self._occupancy = occupancy
            self._occupancy_at = time.time()
            return realtime, occupancy
