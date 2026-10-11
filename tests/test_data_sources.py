import json
import os
import unittest
from unittest.mock import MagicMock, patch

from data_sources import (
    CitiBikeDataSource,
    DataSourceRegistry,
    HttpJsonDataSource,
    NJTransitDataSource,
    StaticDataSource,
    SystemDataSource,
    WeatherDataSource,
    resolve_field_path,
)


class TestDataSources(unittest.TestCase):
    def test_resolve_field_path(self):
        data = {
            "state": "on",
            "attributes": {
                "temperature": 72.5,
                "sensors": [{"id": "s1", "val": 10}, {"id": "s2", "val": 20}],
            },
        }
        self.assertEqual(resolve_field_path(data, "state"), "on")
        self.assertEqual(resolve_field_path(data, "attributes.temperature"), 72.5)
        self.assertEqual(resolve_field_path(data, "attributes.sensors[0].val"), 10)
        self.assertEqual(resolve_field_path(data, "attributes.sensors[1].id"), "s2")
        self.assertIsNone(resolve_field_path(data, "attributes.nonexistent"))
        self.assertIsNone(resolve_field_path(data, "attributes.sensors[5]"))
        self.assertIsNone(resolve_field_path(None, "anything"))
        self.assertEqual(resolve_field_path(data, ""), data)

    def test_system_data_source(self):
        src = SystemDataSource(batt_level=80, is_charging=True, phase="peak")
        data = src.get_data(is_mock=False)
        self.assertEqual(data["batt_level"], 80)
        self.assertTrue(data["is_charging"])
        self.assertEqual(data["phase"], "peak")
        self.assertIn("time_str", data)

        src.update_state(batt_level=90, is_charging=False, phase="offpeak")
        data2 = src.get_data(is_mock=False)
        self.assertEqual(data2["batt_level"], 90)
        self.assertFalse(data2["is_charging"])
        self.assertEqual(data2["phase"], "offpeak")

        mock_data = src.get_data(is_mock=True)
        self.assertEqual(mock_data["batt_level"], 90)

    def test_static_data_source(self):
        src = StaticDataSource({"temp": 68, "status": "ok"})
        self.assertEqual(src.get_data()["temp"], 68)

    @patch("urllib.request.urlopen")
    def test_http_json_data_source(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"state": "cool", "attributes": {"current_temp": 71}}
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        with patch.dict(os.environ, {"TEST_HA_KEY": "my_secret_token"}):
            src = HttpJsonDataSource(
                url="http://ha.local:8123/api/states/climate.ac",
                headers={"Authorization": "Bearer ${TEST_HA_KEY}"},
                fields={
                    "state": "state",
                    "temp": "attributes.current_temp",
                },
                cache_ttl=60,
            )
            data = src.get_data(is_mock=False)
            self.assertEqual(data["state"], "cool")
            self.assertEqual(data["temp"], 71)

            # Caching check
            data2 = src.get_data(is_mock=False)
            self.assertEqual(data2["temp"], 71)
            self.assertEqual(mock_urlopen.call_count, 1)

            # Mock mode check
            mock_data = src.get_data(is_mock=True)
            self.assertEqual(mock_data["state"], "mock_state")
            self.assertEqual(mock_data["temp"], "mock_temp")

    @patch("urllib.request.urlopen")
    def test_http_json_error_handling(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("network unreachable")
        src = HttpJsonDataSource(url="http://fail.local/api")
        data = src.get_data(is_mock=False)
        self.assertEqual(data, {})

    def test_weather_data_source(self):
        src = WeatherDataSource()
        data = src.get_data(is_mock=True)
        self.assertEqual(data.temp, 63)

    def test_njtransit_data_source(self):
        mock_tracker = MagicMock()
        mock_tracker.get_arrivals_with_status.return_value = (
            "ok",
            [{"route": "126", "destination": "NY", "eta": "5m"}],
        )
        src = NJTransitDataSource(tracker=mock_tracker)
        data = src.get_data(is_mock=False)
        self.assertIn("stops", data)
        self.assertIn("status", data)

        mock_data = src.get_data(is_mock=True)
        self.assertIn("stops", mock_data)

    def test_citibike_data_source(self):
        mock_tracker = MagicMock()
        mock_tracker.get_station_status.return_value = [{"name": "9th St", "ebikes": 5}]
        mock_tracker.get_mock_data.return_value = [{"name": "Mock 9th St", "ebikes": 2}]
        src = CitiBikeDataSource(tracker=mock_tracker)

        data = src.get_data(is_mock=False)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "9th St")

        mock_data = src.get_data(is_mock=True)
        self.assertEqual(mock_data[0]["name"], "Mock 9th St")

    def test_data_source_registry(self):
        reg = DataSourceRegistry()
        reg.register("custom_static", StaticDataSource({"val": 123}))
        self.assertIsNotNone(reg.get("custom_static"))
        self.assertIsNone(reg.get("unknown"))

        s1 = reg.create_source({"type": "static", "data": {"a": 1}})
        self.assertIsInstance(s1, StaticDataSource)

        s2 = reg.create_source({"type": "http_json", "url": "http://example.com"})
        self.assertIsInstance(s2, HttpJsonDataSource)

        s3 = reg.create_source({"type": "weather"})
        self.assertIsInstance(s3, WeatherDataSource)

        s4 = reg.create_source({"type": "njtransit"})
        self.assertIsInstance(s4, NJTransitDataSource)

        s5 = reg.create_source({"type": "citibike"})
        self.assertIsInstance(s5, CitiBikeDataSource)

        s6 = reg.create_source({"type": "system"})
        self.assertIsInstance(s6, SystemDataSource)

        s7 = reg.create_source({"type": "invalid_unknown"})
        self.assertIsNone(s7)
