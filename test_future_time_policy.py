"""Future defaults and recommendations share UTC alignment without rounding inputs."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from configuration_profile import default_time
from guidance import recommendations


class FutureTimePolicyTest(unittest.TestCase):
    def test_defaults_and_candidates_never_start_before_clock(self):
        for now in (datetime(2026, 10, 29, 2, 17, tzinfo=timezone.utc),
                    datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
                    datetime(2026, 10, 29, 3, tzinfo=timezone.utc)):
            for interval in (1, 3, 6):
                with self.subTest(now=now, interval=interval):
                    with patch('configuration_profile.datetime') as clock:
                        clock.now.return_value = now
                        clock.fromtimestamp.side_effect = datetime.fromtimestamp
                        value = default_time(interval, 12)
                    candidates = recommendations({'duration_hours': 12, 'forecast_interval_hours': interval}, {}, now=now)['suggestions']['time']['candidates']
                    start = datetime.fromisoformat(value['start_time'])
                    self.assertGreaterEqual(start, now)
                    self.assertEqual(start.hour % interval, 0)
                    self.assertEqual((start.minute, start.second, start.microsecond), (0, 0, 0))
                    self.assertEqual(value, candidates[0]['value'])
                    self.assertEqual(datetime.fromisoformat(value['end_time']) - start, timedelta(hours=12))

    def test_explicit_times_are_not_rounded_by_recommendation(self):
        request = {'start_time': '2026-10-29T03:17:00Z', 'end_time': '2026-10-29T15:17:00Z'}
        time = recommendations(request, {}, now=datetime(2026, 10, 29, 2, tzinfo=timezone.utc))['suggestions']['time']
        self.assertEqual(time['value'], list(request.values()))
        self.assertNotIn('candidates', time)
