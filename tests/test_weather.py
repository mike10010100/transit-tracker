import json
import unittest
from unittest.mock import MagicMock, patch

from weather import (
    WeatherSnapshot,
    WeatherTracker,
    get_mock_weather_data,
    interpret_wmo_code,
)


class TestWeather(unittest.TestCase):
    def test_interpret_wmo_code(self):
        desc, glyph = interpret_wmo_code(0)
        self.assertEqual(desc, "Clear sky")
        self.assertEqual(glyph, "SUN")

        desc, glyph = interpret_wmo_code(63)
        self.assertEqual(desc, "Moderate rain")
        self.assertEqual(glyph, "RAIN")

        desc, glyph = interpret_wmo_code(9999)
        self.assertEqual(desc, "Unknown")
        self.assertEqual(glyph, "CLOUD")

    def test_mock_weather_data(self):
        mock = get_mock_weather_data()
        self.assertIsInstance(mock, WeatherSnapshot)
        self.assertEqual(mock.temp, 63)
        self.assertEqual(mock.status, "ok")
        self.assertTrue(len(mock.forecast) >= 3)
        d = mock.to_dict()
        self.assertEqual(d["temp"], 63)
        self.assertIn("forecast", d)
        self.assertEqual(len(d["forecast"]), len(mock.forecast))

    @patch("urllib.request.urlopen")
    def test_tracker_fetch_live_success(self, mock_urlopen):
        sample_api_resp = {
            "current": {
                "temperature_2m": 72.4,
                "apparent_temperature": 71.1,
                "relative_humidity_2m": 45,
                "weather_code": 1,
                "wind_speed_10m": 9.2,
            },
            "daily": {
                "time": ["2026-10-11", "2026-10-12"],
                "temperature_2m_max": [75.1, 70.3],
                "temperature_2m_min": [55.2, 52.0],
                "weather_code": [1, 2],
                "precipitation_probability_max": [10, 20],
            },
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(sample_api_resp).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        tracker = WeatherTracker(cache_ttl=60)
        snapshot = tracker.get_weather()

        self.assertEqual(snapshot.temp, 72)
        self.assertEqual(snapshot.apparent_temp, 71)
        self.assertEqual(snapshot.humidity, 45)
        self.assertEqual(snapshot.condition, "Mainly clear")
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(len(snapshot.forecast), 2)
        self.assertEqual(snapshot.forecast[0].temp_max, 75)

        # Verify caching (mock_urlopen called only once)
        snapshot2 = tracker.get_weather()
        self.assertEqual(snapshot2.temp, 72)
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("urllib.request.urlopen")
    def test_tracker_fetch_error_with_cache_returns_stale(self, mock_urlopen):
        tracker = WeatherTracker(cache_ttl=1)
        tracker._cache = WeatherSnapshot(
            temp=70,
            apparent_temp=69,
            condition="Clear",
            condition_glyph="SUN",
            temp_max=75,
            temp_min=60,
            humidity=50,
            wind_speed=5,
            precip_prob=0,
            forecast=(),
            status="ok",
            as_of=100.0,
        )
        tracker._cache_time = 50.0  # expired

        mock_urlopen.side_effect = Exception("network error")
        snapshot = tracker.get_weather()

        self.assertEqual(snapshot.temp, 70)
        self.assertEqual(snapshot.status, "stale")

    @patch("urllib.request.urlopen")
    def test_tracker_fetch_error_without_cache_returns_error_mock(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("offline")
        tracker = WeatherTracker()
        snapshot = tracker.get_weather()

        self.assertEqual(snapshot.status, "error")
        self.assertEqual(snapshot.temp, 63)  # from mock
