import json
import math
from datetime import date, datetime, time
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from mileage import EARTH_RADIUS_MI, MileageStore, load_snapshot


class MileageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.redis_patch = patch('mileage.redis', None)
        self.redis_patch.start()
        self.addCleanup(self.redis_patch.stop)
        self.path = Path(self.tmp.name) / 'odometer.json'
        self.store = MileageStore(state_path=self.path)

    def feed(self, t, miles, mph=60, valid=True):
        self.store.tick(mph, t, lat=0, lon=math.degrees(miles / EARTH_RADIUS_MI), fix_ok=valid)

    def drive(self, start=0, seconds=10, offset=0):
        for t in range(start, start+seconds+1):
            self.feed(t, offset+(t-start)/60)

    def test_known_distance_includes_confirmation_window(self):
        self.drive(seconds=60)
        self.assertAlmostEqual(self.store.odometer_miles, 1.0, places=8)
        self.assertAlmostEqual(self.store.trip_miles, 1.0, places=8)

    def test_unconfirmed_motion_is_discarded(self):
        self.feed(0, 0)
        self.feed(1, 1/60)
        self.feed(2, 1/60, mph=0)
        self.assertEqual(self.store.odometer_miles, 0)

    def test_parked_drift_does_not_arm(self):
        for t in range(300):
            self.feed(t, (t%4)*0.00001, mph=3)
        self.assertEqual(self.store.odometer_miles, 0)

    def test_stop_suppresses_jitter_immediately(self):
        self.drive()
        before = self.store.odometer_miles
        for t in range(11, 100):
            self.feed(t, 10/60+(t%4)*0.00001, mph=0.4)
        self.assertEqual(self.store.odometer_miles, before)
        self.assertFalse(self.store._driving)

    def test_unchanged_coordinates_update_stop_timers(self):
        self.drive()
        for t in range(11, 60): self.feed(t, 10/60, mph=0)
        self.assertFalse(self.store._driving)
        self.assertEqual(self.store._last_tick, 59)

    def test_slow_motion_times_out_even_above_parked_threshold(self):
        self.drive()
        for t in range(11, 70): self.feed(t, 10/60+(t-10)*3/3600, mph=3)
        self.assertFalse(self.store._driving)
        before = self.store.odometer_miles
        for t in range(70, 100): self.feed(t, 10/60+(t-10)*3/3600, mph=3)
        self.assertEqual(self.store.odometer_miles, before)

    def test_fix_loss_does_not_bridge_distance_or_confirmation(self):
        self.drive()
        before = self.store.odometer_miles
        self.feed(11, 11/60, valid=False)
        self.feed(12, 12/60)
        self.feed(13, 13/60)
        self.assertEqual(self.store.odometer_miles, before)
        self.feed(14, 14/60)
        self.feed(15, 15/60)
        self.assertAlmostEqual(self.store.odometer_miles, before+3/60)

    def test_gap_does_not_bridge_distance(self):
        self.drive()
        before = self.store.odometer_miles
        self.feed(20, 20/60)
        self.assertEqual(self.store.odometer_miles, before)
        self.drive(start=20, offset=20/60, seconds=5)
        self.assertAlmostEqual(self.store.odometer_miles, before+4/60)

    def test_duplicate_and_backwards_timestamps_add_nothing(self):
        self.drive()
        before = self.store.odometer_miles
        self.feed(10, 11/60)
        self.feed(9, 12/60)
        self.assertEqual(self.store.odometer_miles, before)
        self.assertFalse(self.store._driving)

    def test_position_jump_rejected_against_speed(self):
        for t in range(5): self.feed(t, t*5/3600, mph=5)
        before = self.store.odometer_miles
        self.feed(5, 4*5/3600+0.02, mph=5)  # 72 mph implied; below old 120 cap.
        self.assertEqual(self.store.odometer_miles, before)
        self.assertFalse(self.store._driving)

    def test_bad_values_never_poison_totals(self):
        self.drive()
        before = self.store.odometer_miles
        for args in ((float('nan'), 11, 0, 0), (10, float('inf'), 0, 0),
                     (10, 11, float('nan'), 0), (10, 11, 91, 0),
                     (10, 11, 0, 181), (-1, 11, 0, 0), (200, 11, 0, 0)):
            mph, t, lat, lon = args
            self.store.tick(mph, t, lat=lat, lon=lon, fix_ok=True)
        self.assertEqual(self.store.odometer_miles, before)

    def test_known_distance_at_multiple_sample_rates(self):
        for hz in (1, 5, 10):
            with self.subTest(hz=hz):
                store = MileageStore(state_path=Path(self.tmp.name) / f'{hz}.json')
                for n in range(10*hz+1):
                    t = n/hz
                    store.tick(36, t, lat=0, lon=math.degrees((36*t/3600)/EARTH_RADIUS_MI), fix_ok=True)
                self.assertAlmostEqual(store.odometer_miles, 0.1, places=8)

    def test_read_only_snapshot_does_not_overwrite_live_data(self):
        self.drive()
        self.store.flush()
        original = self.path.read_bytes()
        original_time = self.path.stat().st_mtime_ns
        self.drive(start=11, offset=11/60, seconds=5)
        snapshot = load_snapshot(state_path=self.path)
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual(original_time, self.path.stat().st_mtime_ns)
        self.assertLess(snapshot['odometer_miles'], self.store.odometer_miles)

    def test_persistence_and_trip_reset_preserve_odometer(self):
        self.drive()
        self.store.reset_trip()
        reloaded = MileageStore(state_path=self.path)
        self.assertEqual(reloaded.trip_miles, 0)
        self.assertAlmostEqual(reloaded.odometer_miles, 10/60, places=4)

    def test_newer_reset_trip_wins_but_odometer_never_decreases(self):
        self.store.odometer_miles = 100
        self.store.trip_miles = 20
        self.store.updated_at = '2026-01-01T00:00:00+00:00'
        self.store._merge_record({'odometer_miles': 99, 'trip_miles': 0, 'updated_at': '2026-02-01T00:00:00+00:00'})
        self.assertEqual(self.store.odometer_miles, 100)
        self.assertEqual(self.store.trip_miles, 0)

    def test_redis_only_load_keeps_trip_when_file_is_missing(self):
        with patch.object(MileageStore, '_read_redis', return_value={
            'odometer_miles': '100', 'trip_miles': '12',
            'updated_at': '2026-01-01T00:00:00+00:00',
        }):
            store = MileageStore(state_path=Path(self.tmp.name) / 'missing.json', read_only=True)
        self.assertEqual(store.odometer_miles, 100)
        self.assertEqual(store.trip_miles, 12)
        self.assertFalse(store.state_path.exists())

    def test_trip_resets_on_first_fix_of_a_new_day(self):
        noon = datetime.combine(date.today(), time(12)).timestamp()
        for start in (noon - 86400, noon):
            for t in range(11):
                self.store.tick(60, start + t, lat=0, lon=math.degrees((t/60)/EARTH_RADIUS_MI), fix_ok=True)
        self.assertAlmostEqual(self.store.odometer_miles, 20/60, places=6)
        self.assertAlmostEqual(self.store.trip_miles, 10/60, places=6)
        self.assertEqual(self.store.trip_date, date.today().isoformat())

    def test_first_dated_fix_keeps_existing_trip(self):
        self.store.trip_miles = 5
        self.drive()
        self.assertAlmostEqual(self.store.trip_miles, 5 + 10/60, places=6)

    def test_old_trip_cleared_on_load_but_odometer_kept(self):
        self.path.write_text(json.dumps({'odometer_miles': 50, 'trip_miles': 7, 'trip_date': '2020-01-01'}))
        store = MileageStore(state_path=self.path)
        self.assertEqual((store.odometer_miles, store.trip_miles, store.trip_date), (50, 0, ''))

    def test_merge_prefers_newer_trip_day(self):
        self.store._merge_record({'trip_miles': 4, 'trip_date': '2026-01-02', 'updated_at': '2026-01-01T00:00:00+00:00'})
        self.store._merge_record({'trip_miles': 9, 'trip_date': '2026-01-01', 'updated_at': '2026-02-01T00:00:00+00:00'})
        self.assertEqual((self.store.trip_miles, self.store.trip_date), (4, '2026-01-02'))

    def test_invalid_persisted_numbers_ignored(self):
        self.store._apply_record({'odometer_miles': 'NaN', 'trip_miles': -1})
        self.assertEqual(self.store.odometer_miles, 0)
        self.assertEqual(self.store.trip_miles, 0)


if __name__ == '__main__': unittest.main()
