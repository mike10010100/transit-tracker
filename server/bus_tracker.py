import datetime
import os
import re
import threading
import time
from typing import Any, Optional, TypedDict

import requests
from logsafe import redact


class ArrivalRecord(TypedDict, total=False):
    route: Optional[str]
    destination: Optional[str]
    eta: Optional[str]
    occupancy: Optional[str]
    vehicle_id: Optional[str]
    live: Optional[bool]
    # Unix time of the departure. When present, the renderer recomputes the
    # countdown from it at render time (the `eta` text is only a fallback).
    epoch: Optional[float]


def load_env_file(env_file: str) -> None:
    """
    Minimal .env parser used when python-dotenv is unavailable. Reads
    KEY=value pairs, ignoring blanks/comments, and only sets keys that are not
    already present in the environment.
    """
    if not os.path.exists(env_file):
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k not in os.environ:
                    os.environ[k] = v


_local_env = os.path.join(os.path.dirname(__file__), ".env")
_parent_env = os.path.join(os.path.dirname(__file__), "..", ".env")
_env_file = _local_env if os.path.exists(_local_env) else _parent_env

try:
    from dotenv import load_dotenv

    if os.path.exists(_env_file):
        load_dotenv(_env_file)
    else:
        load_dotenv()
except ImportError:
    # Native fallback if python-dotenv is not installed
    load_env_file(_env_file)

_DUE_RE = re.compile(
    r"\b(approach\w*|due|now|arriving|boarding|board|all aboard)\b", re.IGNORECASE
)
_MINS_RE = re.compile(r"\b(\d+)\s*min", re.IGNORECASE)
_CLOCK_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?:\s*([AaPp])\.?[Mm]\.?)?\b")


def estimate_departure_epoch(
    status: str, dep_time: str, now: Optional[float] = None
) -> Optional[float]:
    """
    Estimates the departure's unix time at fetch time from the upstream status
    ("in 5 mins", "APPROACHING") or, failing that, its clock time ("8:35 AM",
    "20:35"). Returns None when neither can be parsed.
    """
    now = time.time() if now is None else now
    if status:
        m = _MINS_RE.search(status)
        if m:
            return now + int(m.group(1)) * 60
        if _DUE_RE.search(status):
            return now
    for text in (dep_time, status):
        m = _CLOCK_RE.search(text or "")
        if not m:
            continue
        hour, minute, ampm = (
            int(m.group(1)),
            int(m.group(2)),
            (m.group(3) or "").lower(),
        )
        if ampm:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if ampm == "p" else 0)
        if hour > 23 or minute > 59:
            continue
        local_now = datetime.datetime.fromtimestamp(now)
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # A clock time well in the past means "tomorrow" (e.g. 12:05 AM seen at 11:58 PM).
        if candidate.timestamp() < now - 6 * 3600:
            candidate += datetime.timedelta(days=1)
        return candidate.timestamp()
    return None


def normalize_arrival(t: dict[str, Any], now: Optional[float] = None) -> ArrivalRecord:
    """
    Maps a raw NJ Transit trip (from either BUSDV2 or GraphQL) to the canonical
    Arrival record consumed by the renderer. Centralizing this mapping keeps the
    two upstream sources behind a single domain shape.
    """
    status = (t.get("departurestatus") or "").strip()
    dep_time = (t.get("departuretime") or "").strip()
    eta_str = (
        f"{status} ({dep_time})"
        if status and dep_time
        else (status or dep_time or "Scheduled")
    )
    record: ArrivalRecord = {
        "route": t.get("public_route"),
        "destination": (t.get("header") or "").strip(),
        "eta": eta_str,
        "occupancy": t.get("passload"),
        "vehicle_id": t.get("vehicle_id"),
    }
    epoch = estimate_departure_epoch(status, dep_time, now=now)
    if epoch is not None:
        record["epoch"] = epoch
    return record


TEST_HOST_MARKER = "testpcsdata"


def describe_base_url(base_url: Optional[str] = None) -> str:
    """
    Startup notice for the effective BUSDV2 base URL. Warns loudly when it is
    NJ Transit's *test* environment (the historical default).
    """
    url = (base_url or NJTransitBusTracker.DEFAULT_BASE_URL).rstrip("/")
    if TEST_HOST_MARKER in url.lower():
        return (
            f"[Tracker] WARNING: BUSDV2 base URL is the NJ Transit TEST host ({url}); "
            f"production credentials are being sent to the test environment. "
            f"Set NJT_BASE_URL=https://pcsdata.njtransit.com to use production."
        )
    return f"[Tracker] BUSDV2 base URL: {url}"


class NJTransitBusTracker:
    """
    Client for NJ Transit Bus DepartureVision (BUSDV2) API.
    Handles automated 24-hour token minting and renewal.
    """

    DEFAULT_BASE_URL = os.environ.get("NJT_BASE_URL", "https://pcsdata.njtransit.com")

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        raw_user = (
            username
            or os.environ.get("NJT_USERNAME")
            or os.environ.get("NJT_API_USERNAME")
            or ""
        )
        # NJ Transit API requires username handle, not email
        self.username = raw_user.split("@")[0] if "@" in raw_user else raw_user
        self.password = (
            password
            or os.environ.get("NJT_PASSWORD")
            or os.environ.get("NJT_API_PASSWORD")
        )
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.token: Optional[str] = None
        self.token_expiry: float = 0
        self.session = requests.Session()
        # requests.Session and the token are shared by request threads.
        self._lock = threading.RLock()

    @property
    def auth_url(self) -> str:
        return f"{self.base_url}/api/BUSDV2/authenticateUser"

    @property
    def bus_dv_url(self) -> str:
        return f"{self.base_url}/api/BUSDV2/getBusDV"

    def get_token(self) -> str:
        """
        Retrieves a valid token. If expired or not present, automatically
        authenticates with NJ Transit to obtain a new 24-hour token.
        """
        with self._lock:
            return self._get_token_locked()

    def _get_token_locked(self) -> str:
        if not self.token or time.time() > self.token_expiry:
            if not self.username or not self.password:
                raise ValueError(
                    "Missing NJ Transit credentials. Please provide username and password, "
                    "or set NJT_USERNAME and NJT_PASSWORD in environment variables."
                )

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
                self.token_expiry = time.time() + 82800
            else:
                raise RuntimeError(f"NJ Transit authentication failed: {data}")

        return self.token

    GRAPHQL_URL = "https://www.njtransit.com/api/graphql/graphql"
    GRAPHQL_QUERY = """
    query BusArrivalsByStopID($stopID: ID!) {
      getBusArrivalsByStopID(stopID: $stopID) {
        publicRoute
        header
        lanegate
        departuretime
        departurestatus
        schedDepTime
        vehicleId
        passload
      }
    }
    """

    def get_arrivals_graphql(
        self, stop_id: str, route: str = "126"
    ) -> list[dict[str, Any]]:
        """
        Fetches live arrivals via NJ Transit's public web GraphQL API.
        No token or authentication required; highly reliable fallback.
        """
        with self._lock:
            resp = self.session.post(
                self.GRAPHQL_URL,
                json={
                    "operationName": "BusArrivalsByStopID",
                    "variables": {"stopID": str(stop_id)},
                    "query": self.GRAPHQL_QUERY,
                },
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
        data = payload.get("data", {}).get("getBusArrivalsByStopID") or []
        results = []
        for item in data:
            pub_route = item.get("publicRoute") or ""
            if route and pub_route.strip() != route.strip():
                continue
            results.append(
                {
                    "public_route": pub_route,
                    "header": item.get("header"),
                    "lanegate": item.get("lanegate"),
                    "departuretime": item.get("departuretime"),
                    "departurestatus": item.get("departurestatus"),
                    "vehicle_id": item.get("vehicleId"),
                    "passload": item.get("passload"),
                }
            )
        return results

    # Arrival fetch statuses
    STATUS_OK = "ok"  # Upstream responded and returned upcoming buses
    STATUS_EMPTY = "empty"  # Upstream responded but there are genuinely no buses
    STATUS_ERROR = "error"  # Upstream could not be reached; data is unknown

    def get_arrivals_with_status(
        self, stop_id: str, route: str = "126"
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        Fetches upcoming bus arrivals and returns (status, trips).

        Unlike get_arrivals(), this distinguishes an upstream failure
        (STATUS_ERROR, arrivals unknown) from a successful response with no
        upcoming buses (STATUS_EMPTY). Collapsing both into an empty list
        previously caused the dashboard to falsely claim "no buses".
        """
        try:
            token = self.get_token()
            with self._lock:
                resp = self.session.post(
                    self.bus_dv_url,
                    data={
                        "token": token,
                        "stop": str(stop_id),
                        "route": route,
                        "direction": "",
                        "IP": "",
                    },
                    timeout=8,
                )
                resp.raise_for_status()
                data = resp.json()
            trips = data.get("DVTrip")
            # BUSDV2 can answer 200 with an error payload (e.g. {"message":
            # {"message":"unknown user"}, "DVTrip":null}) when the account is not
            # provisioned for DepartureVision. Treat a null trip list or an error
            # message as unusable and fall through to the public GraphQL, which
            # still returns the next departure per route -- better than reporting
            # zero buses. A genuine empty list ([]) stays EMPTY.
            if trips is None or data.get("message"):
                raise RuntimeError(f"BUSDV2 unusable ({data.get('message')})")
        except Exception as e:
            # Automatic fallback to official website GraphQL API
            try:
                trips = self.get_arrivals_graphql(stop_id=stop_id, route=route)
            except Exception as e2:
                print(
                    f"[Tracker] Both BUSDV2 ({redact(e)}) and GraphQL ({redact(e2)}) failed."
                )
                return self.STATUS_ERROR, []

        return (self.STATUS_OK if trips else self.STATUS_EMPTY), trips

    def get_arrivals(self, stop_id: str, route: str = "126") -> list[dict[str, Any]]:
        """
        Fetches upcoming bus arrivals for a specific stop number and route.
        Returns an empty list on both success-with-no-buses and upstream
        failure; prefer get_arrivals_with_status() when the distinction matters.
        """
        _status, trips = self.get_arrivals_with_status(stop_id=stop_id, route=route)
        return trips

    def get_summary(
        self, stops: dict[str, str], route: str = "126"
    ) -> dict[str, list[ArrivalRecord]]:
        """
        Fetches simplified arrival summaries for multiple stops.
        stops format: {"Stop Name": "5-digit-stop-id"}
        """
        summary: dict[str, list[ArrivalRecord]] = {}
        for stop_name, stop_id in stops.items():
            trips = self.get_arrivals(stop_id=stop_id, route=route)
            summary[stop_name] = [normalize_arrival(t) for t in trips]
        return summary

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "NJTransitBusTracker":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


if __name__ == "__main__":
    STOPS_TO_TRACK = {
        "Washington St at 9th St (Stop #20512)": "20512",
        "Clinton St at 9th St (Stop #20494)": "20494",
    }

    tracker = NJTransitBusTracker()
    try:
        results = tracker.get_summary(STOPS_TO_TRACK, route="126")
        for name, arrivals in results.items():
            print(f"\n=== {name} ===")
            if not arrivals:
                print("  No buses reported in the next hour.")
            for a in arrivals:
                load = (
                    f" [Occupancy: {a['occupancy']}]"
                    if a["occupancy"] and a["occupancy"] != "EMPTY"
                    else ""
                )
                bus_num = f" (Bus #{a['vehicle_id']})" if a["vehicle_id"] else ""
                print(
                    f"  [{a['route']}] {a['destination']} -> {a['eta']}{bus_num}{load}"
                )
    except ValueError as e:
        print(f"Setup error: {e}")
    except Exception as e:
        print(f"Error fetching arrivals: {e}")
