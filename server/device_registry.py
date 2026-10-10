"""
Device registry and state isolation for Transit Tracker.

Maintains per-device telemetry, isolated action/diagnostic queues,
and disk persistence for client logs and diagnostics dumps.
"""

import logging
import os
import re
import threading
import time
from collections import deque
from typing import Any, Optional

logger = logging.getLogger(__name__)


def sanitize_client_id(client_id: Any) -> str:
    """
    Sanitizes a client ID string.
    Allows only [a-zA-Z0-9_\\-], trims whitespace, truncates to 64 chars,
    and falls back to 'default' if empty or invalid.
    """
    if client_id is None:
        return "default"
    s = str(client_id).strip()
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "", s)
    if not cleaned:
        return "default"
    return cleaned[:64]


class DeviceRecord:
    """Represents a registered client device and its current state."""

    def __init__(
        self,
        client_id: str,
        remote_ip: str = "",
        last_seen: float = 0.0,
        battery: Optional[float] = None,
        charging: Optional[bool] = None,
        client_mode: str = "",
        client_version: str = "",
        firmware_version: str = "",
        pending_action: str = "",
        pending_diag: str = "",
        target_mode: str = "",
        recent_logs: Optional[deque] = None,
        last_diagnostics_text: str = "",
        last_diagnostics_time: float = 0.0,
    ) -> None:
        self.client_id = sanitize_client_id(client_id)
        self.remote_ip = remote_ip
        self.last_seen = float(last_seen)
        self.battery = float(battery) if battery is not None else None
        self.charging = bool(charging) if charging is not None else None
        self.client_mode = client_mode
        self.client_version = client_version
        self.firmware_version = firmware_version
        self.pending_action = pending_action
        self.pending_diag = pending_diag
        self.target_mode = target_mode
        self.recent_logs = recent_logs if recent_logs is not None else deque(maxlen=200)
        self.last_diagnostics_text = last_diagnostics_text
        self.last_diagnostics_time = float(last_diagnostics_time)

    def is_online(self, poll_interval: float = 60.0) -> bool:
        """
        Determines whether the device is considered online based on its
        last_seen timestamp and expected poll interval.
        """
        if self.last_seen <= 0.0:
            return False
        cutoff = max(120.0, 2.5 * float(poll_interval))
        return (time.time() - self.last_seen) < cutoff

    def to_dict(self) -> dict[str, Any]:
        """Serializes device record to dictionary for API and Web UI."""
        return {
            "client_id": self.client_id,
            "remote_ip": self.remote_ip,
            "ip": self.remote_ip,
            "last_seen": self.last_seen,
            "battery": self.battery,
            "charging": self.charging,
            "client_mode": self.client_mode,
            "client_version": self.client_version,
            "firmware_version": self.firmware_version,
            "pending_action": self.pending_action,
            "pending_diag": self.pending_diag,
            "target_mode": self.target_mode,
            "recent_logs": list(self.recent_logs),
            "last_diagnostics_text": self.last_diagnostics_text,
            "last_diagnostics_time": self.last_diagnostics_time,
            "is_online": self.is_online(),
            "online": self.is_online(),
            "status": "online" if self.is_online() else "offline",
        }


MAX_REGISTERED_DEVICES = int(os.environ.get("TRACKER_MAX_DEVICES", "64"))
MAX_LOG_FILE_SIZE = 512 * 1024  # 512 KB
MAX_DIAG_FILE_SIZE = 512 * 1024  # 512 KB


class DeviceRegistry:
    """Thread-safe per-device registry and queue manager."""

    def __init__(self, max_devices: int = MAX_REGISTERED_DEVICES) -> None:
        self._lock = threading.RLock()
        self._devices: dict[str, DeviceRecord] = {}
        self.max_devices = max(1, max_devices)

    def _evict_one_locked(self) -> None:
        """Evicts the oldest offline device or oldest seen non-default device to bound memory."""
        offline_devs = [
            d
            for d in self._devices.values()
            if d.client_id != "default" and not d.is_online()
        ]
        if offline_devs:
            evict = min(offline_devs, key=lambda d: d.last_seen)
            del self._devices[evict.client_id]
            logger.info(
                "Evicted offline device %s from registry (limit %d reached)",
                evict.client_id,
                self.max_devices,
            )
            return

        non_defaults = [d for d in self._devices.values() if d.client_id != "default"]
        if non_defaults:
            evict = min(non_defaults, key=lambda d: d.last_seen)
            del self._devices[evict.client_id]
            logger.info(
                "Evicted oldest device %s from registry (limit %d reached)",
                evict.client_id,
                self.max_devices,
            )

    def get_or_register(self, client_id: str, remote_ip: str = "") -> DeviceRecord:
        """
        Retrieves an existing device record or automatically registers a new one.
        Updates remote_ip if provided.
        """
        safe_id = sanitize_client_id(client_id)
        with self._lock:
            if safe_id not in self._devices:
                if len(self._devices) >= self.max_devices:
                    self._evict_one_locked()
                self._devices[safe_id] = DeviceRecord(
                    client_id=safe_id,
                    remote_ip=remote_ip,
                    last_seen=0.0,
                )
            elif remote_ip:
                self._devices[safe_id].remote_ip = remote_ip
            return self._devices[safe_id]

    def get_device(self, client_id: str) -> Optional[DeviceRecord]:
        """Returns the device record for client_id if registered, else None."""
        safe_id = sanitize_client_id(client_id)
        with self._lock:
            return self._devices.get(safe_id)

    def update_telemetry(
        self,
        client_id: str,
        remote_ip: str = "",
        battery: Optional[float] = None,
        charging: Optional[bool] = None,
        client_mode: str = "",
        client_version: str = "",
        firmware_version: str = "",
    ) -> DeviceRecord:
        """
        Updates last_seen timestamp and reported telemetry fields for a device.
        """
        safe_id = sanitize_client_id(client_id)
        with self._lock:
            record = self.get_or_register(safe_id, remote_ip=remote_ip)
            record.last_seen = time.time()
            if remote_ip:
                record.remote_ip = remote_ip
            if battery is not None:
                try:
                    record.battery = float(battery)
                except (ValueError, TypeError):
                    pass
            if charging is not None:
                if isinstance(charging, str):
                    record.charging = charging.lower() in ("1", "true", "yes")
                else:
                    record.charging = bool(charging)
            if client_mode:
                record.client_mode = client_mode
            if client_version:
                record.client_version = client_version
            if firmware_version:
                record.firmware_version = firmware_version
            return record

    def pop_action(self, client_id: str) -> str:
        """
        Returns and clears pending action for a specific client.
        Does not affect other devices' queues.
        """
        safe_id = sanitize_client_id(client_id)
        with self._lock:
            record = self._devices.get(safe_id)
            if record is None:
                return ""
            action = record.pending_action
            record.pending_action = ""
            return action

    def pop_diag(self, client_id: str) -> str:
        """
        Returns and clears pending diagnostic request for a specific client.
        Does not affect other devices' queues.
        """
        safe_id = sanitize_client_id(client_id)
        with self._lock:
            record = self._devices.get(safe_id)
            if record is None:
                return ""
            diag = record.pending_diag
            record.pending_diag = ""
            return diag

    def set_action(self, client_id_or_all: str, action: str) -> None:
        """
        Sets pending action for a specific client, or broadcasts to all registered devices
        if client_id_or_all is 'all' or empty.
        """
        with self._lock:
            target = (client_id_or_all or "").strip()
            if not target or target.lower() == "all":
                if not self._devices:
                    rec = self.get_or_register("default")
                    rec.pending_action = action
                else:
                    for rec in self._devices.values():
                        rec.pending_action = action
            else:
                rec = self.get_or_register(target)
                rec.pending_action = action

    def set_diag(self, client_id_or_all: str, diag_mode: str) -> None:
        """
        Sets pending diagnostic request for a specific client, or broadcasts to all
        registered devices if client_id_or_all is 'all' or empty.
        """
        with self._lock:
            target = (client_id_or_all or "").strip()
            if not target or target.lower() == "all":
                if not self._devices:
                    rec = self.get_or_register("default")
                    rec.pending_diag = diag_mode
                else:
                    for rec in self._devices.values():
                        rec.pending_diag = diag_mode
            else:
                rec = self.get_or_register(target)
                rec.pending_diag = diag_mode

    def set_mode(self, client_id_or_all: str, mode: str) -> None:
        """
        Sets target run mode for a specific client, or broadcasts to all
        registered devices if client_id_or_all is 'all' or empty.
        """
        with self._lock:
            target = (client_id_or_all or "").strip()
            if not target or target.lower() == "all":
                if not self._devices:
                    rec = self.get_or_register("default")
                    rec.target_mode = mode
                else:
                    for rec in self._devices.values():
                        rec.target_mode = mode
            else:
                rec = self.get_or_register(target)
                rec.target_mode = mode

    def save_diagnostics(self, client_id: str, text: str, cache_dir: str) -> None:
        """
        Updates device diagnostic text in memory and writes to disk at:
        os.path.join(cache_dir, "devices", safe_id, "diagnostics.txt")
        """
        safe_id = sanitize_client_id(client_id)
        bounded_text = (
            text[:MAX_DIAG_FILE_SIZE] if len(text) > MAX_DIAG_FILE_SIZE else text
        )
        now = time.time()
        with self._lock:
            record = self.get_or_register(safe_id)
            record.last_diagnostics_text = bounded_text
            record.last_diagnostics_time = now

            try:
                dev_dir = os.path.join(cache_dir, "devices", safe_id)
                os.makedirs(dev_dir, exist_ok=True)
                diag_path = os.path.join(dev_dir, "diagnostics.txt")
                with open(diag_path, "w", encoding="utf-8") as f:
                    f.write(bounded_text)
            except OSError as e:
                logger.warning("Failed to save diagnostics file for %s: %s", safe_id, e)

    def append_log(self, client_id: str, text: str, cache_dir: str) -> None:
        """
        Appends log text to device recent_logs deque in memory and writes to disk at:
        os.path.join(cache_dir, "devices", safe_id, "client.log")
        """
        safe_id = sanitize_client_id(client_id)
        bounded_text = text[:65536] if len(text) > 65536 else text
        with self._lock:
            record = self.get_or_register(safe_id)
            record.last_seen = time.time()
            lines = bounded_text.splitlines()
            if not lines and bounded_text:
                lines = [bounded_text]
            for line in lines:
                record.recent_logs.append(line)

            try:
                dev_dir = os.path.join(cache_dir, "devices", safe_id)
                os.makedirs(dev_dir, exist_ok=True)
                log_path = os.path.join(dev_dir, "client.log")
                if (
                    os.path.isfile(log_path)
                    and os.path.getsize(log_path) > MAX_LOG_FILE_SIZE
                ):
                    try:
                        keep_bytes = MAX_LOG_FILE_SIZE // 2
                        with open(log_path, "rb") as f:
                            f.seek(-keep_bytes, os.SEEK_END)
                            tail = f.read()
                        nl = tail.find(b"\n")
                        if nl != -1:
                            tail = tail[nl + 1 :]
                        with open(log_path, "wb") as f:
                            f.write(tail)
                    except OSError:
                        pass
                with open(log_path, "a", encoding="utf-8") as f:
                    if bounded_text.endswith("\n"):
                        f.write(bounded_text)
                    else:
                        f.write(bounded_text + "\n")
            except OSError as e:
                logger.warning("Failed to append log file for %s: %s", safe_id, e)

    def list_devices(self) -> list[DeviceRecord]:
        """
        Returns list of registered devices sorted by last_seen descending.
        """
        with self._lock:
            return sorted(
                self._devices.values(), key=lambda d: d.last_seen, reverse=True
            )

    def clear(self) -> None:
        """Clears all registered devices."""
        with self._lock:
            self._devices.clear()


_global_registry: Optional[DeviceRegistry] = None
_registry_lock = threading.Lock()


def get_device_registry() -> DeviceRegistry:
    """Returns the singleton DeviceRegistry instance."""
    global _global_registry
    if _global_registry is None:
        with _registry_lock:
            if _global_registry is None:
                _global_registry = DeviceRegistry()
    return _global_registry


def reset_device_registry() -> DeviceRegistry:
    """Resets the singleton DeviceRegistry instance for testing."""
    global _global_registry
    with _registry_lock:
        _global_registry = DeviceRegistry()
        return _global_registry
