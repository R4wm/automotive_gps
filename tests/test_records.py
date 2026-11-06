import json
import math
from datetime import date, datetime, time
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from mileage import EARTH_RADIUS_MI
from records import TIMING_VERSION, PersonalRecordsStore, RunTracker, load_snapshot

NOON = datetime.combine(date.today(), time(12)).timestamp()
DAY = 86400


def lon(miles):
    return math.degrees(miles / EARTH_RADIUS_MI)


def speeds(accel, hz=1, start=0.0, top=100.0, idle=3):
    """(seconds, mph): stopped for idle seconds, then constant acceleration."""
    out = [(n / hz, start) for n in range(idle * hz)]
    t0 = idle
    n = 0
    while True:
        mph = min(start + accel * n / hz, top)
        out.append((t0 + n / hz, mph))
        if mph >= top:
            return out
        n += 1


class Feeder:
    """Feed (t, mph) samples with positions integrated from speed."""

    def __init__(self, sink, t0=NOON):
        self.sink, self.t0, self.miles, self.last = sink, t0, 0.0, None

    def feed(self, samples, fix_ok=True):
        out = []
        for t, mph in samples:
            if self.last is not None:
                lt, lmph = self.last
                self.miles += (lmph + mph) / 2 * (t - lt) / 3600
            self.last = (t, mph)
            if isinstance(self.sink, RunTracker):
                out += self.sink.tick(mph, self.t0 + t, 0.0, lon(self.miles))
            else:
                out += self.sink.tick(mph, self.t0 + t, lat=0.0, lon=lon(self.miles), fix_ok=fix_ok)
        return out


class RunTrackerTests(unittest.TestCase):
    def runs(self, samples):
        return dict(Feeder(RunTracker()).feed(samples))

    def test_known_times_at_multiple_sample_rates(self):
        # 6 mph/s from a stop: 0-60 takes 10 s, 20-60 takes 40/6 s and the
        # 1/8 mile solves 3t^2 = 450 mph*s.
        for hz in (1, 5, 10):
            with self.subTest(hz=hz):
                runs = self.runs(speeds(6, hz=hz))
                self.assertAlmostEqual(runs['zero_sixty'], 10, places=6)
                self.assertAlmostEqual(runs['twenty_sixty'], 40 / 6, places=6)
                self.assertAlmostEqual(runs['eighth_mile'], math.sqrt(150), places=5)

    def test_launch_between_fixes(self):
        # Leaves the line at t=3.4 with fixes on whole seconds; timing from
        # the 5 mph crossing would read 0.83 s quick.
        for accel in (4, 6, 9):
            with self.subTest(accel=accel):
                samples = [(t, min(max(0.0, accel * (t - 3.4)), 100.0)) for t in range(40)]
                runs = self.runs(samples)
                self.assertAlmostEqual(runs['zero_sixty'], 60 / accel, places=6)
                self.assertAlmostEqual(runs['eighth_mile'], math.sqrt(900 / accel), places=6)

    def test_rolling_start_is_not_zero_sixty(self):
        runs = self.runs(speeds(6, start=4))  # creeping at 4 mph, never stopped
        self.assertNotIn('zero_sixty', runs)
        self.assertNotIn('eighth_mile', runs)
        self.assertAlmostEqual(runs['twenty_sixty'], 40 / 6, places=6)

    def test_slow_roll_away_is_not_a_launch(self):
        samples = [(t, 0.0) for t in range(3)] + [(t, 3.0) for t in range(3, 12)]
        samples += [(t, 3 + 6 * (t - 11)) for t in range(12, 25)]
        self.assertEqual(set(self.runs(samples)), {'twenty_sixty'})

    def test_held_position_at_launch_does_not_shorten_eighth_mile(self):
        # The receiver holds position near standstill, then catches up.
        tracker, miles, last, out = RunTracker(), 0.0, None, []
        for t, mph in speeds(6):
            if last is not None:
                miles += (last + mph) / 2 / 3600
            last = mph
            out += tracker.tick(mph, NOON + t, 0.0, lon(miles if t > 5 else 0.0))
        self.assertAlmostEqual(dict(out)['eighth_mile'], math.sqrt(150), places=6)

    def test_cruising_around_sixty_is_not_a_run(self):
        cruise = [(t, 58 + 4 * (t % 2)) for t in range(120)]
        self.assertEqual(self.runs(cruise), {})

    def test_dropping_below_twenty_restarts_twenty_sixty_only(self):
        samples = speeds(6, top=30)  # 30 mph at t=8
        samples += [(t, 30 - 6 * (t - 8)) for t in range(9, 13)]  # down to 6 mph
        samples += [(t, 6 + 6 * (t - 12)) for t in range(13, 23)]  # 60 at t=21
        runs = self.runs(samples)
        self.assertAlmostEqual(runs['zero_sixty'], 21 - 3, places=6)
        self.assertAlmostEqual(runs['twenty_sixty'], 40 / 6, places=6)

    def test_fix_loss_and_gap_never_time_across(self):
        for broken in ('invalidate', 'gap'):
            with self.subTest(broken=broken):
                tracker = RunTracker()
                feeder = Feeder(tracker)
                samples = speeds(6)
                feeder.feed(samples[:8])  # reach ~30 mph
                if broken == 'invalidate':
                    tracker.invalidate()
                    rest = samples[8:]
                else:
                    rest = [(t + 2, mph) for t, mph in samples[8:]]
                self.assertEqual(feeder.feed(rest), [])

    def test_one_fix_spike_to_sixty_is_not_a_finish(self):
        samples = speeds(4, top=50)
        t = samples[-1][0]
        samples += [(t + 1, 55), (t + 2, 62), (t + 3, 55), (t + 4, 58), (t + 5, 61), (t + 6, 64)]
        runs = self.runs(samples)
        expected = t + 5 - 1 / 3 - 3  # 60 crossed between 58 and 61
        self.assertAlmostEqual(runs['zero_sixty'], expected, places=6)

    def test_glitch_jump_resets(self):
        samples = speeds(6, top=40) + [(11, 95), (12, 96)]
        self.assertNotIn('zero_sixty', self.runs(samples))

    def test_bad_values_reset_without_records(self):
        tracker = RunTracker()
        for args in ((float('nan'), NOON, 0, 0), (10, float('inf'), 0, 0),
                     (10, NOON, 91, 0), (-1, NOON, 0, 0), (200, NOON, 0, 0)):
            self.assertEqual(tracker.tick(*args), [])

    def test_too_slow_to_count(self):
        runs = self.runs(speeds(1.2))  # 5->60 in 45.8 s, 20->60 in 33.3 s
        self.assertNotIn('zero_sixty', runs)
        self.assertNotIn('twenty_sixty', runs)


class RecordsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.redis_patch = patch('records.redis', None)
        self.redis_patch.start()
        self.addCleanup(self.redis_patch.stop)
        self.path = Path(self.tmp.name) / 'records.json'
        self.store = PersonalRecordsStore(state_path=self.path)

    def run_at(self, t0, accel):
        new = Feeder(self.store, t0=t0).feed(speeds(accel))
        return {(r.kind, r.scope) for r in new}

    def test_today_and_best_scopes(self):
        self.assertEqual(self.run_at(NOON - DAY, 6), {
            ('eighth_mile', 'best'), ('zero_sixty', 'best'), ('twenty_sixty', 'best')})
        self.assertEqual(self.run_at(NOON - DAY + 600, 5), set())  # slower, same day
        best = dict(self.store.best)
        self.assertEqual(self.run_at(NOON, 5), {
            ('eighth_mile', 'today'), ('zero_sixty', 'today'), ('twenty_sixty', 'today')})
        self.assertEqual(self.store.best, best)
        self.assertEqual(self.store.today_date, date.today().isoformat())
        self.assertEqual(self.run_at(NOON + 600, 8), {
            ('eighth_mile', 'best'), ('zero_sixty', 'best'), ('twenty_sixty', 'best')})
        self.assertAlmostEqual(self.store.today['zero_sixty'], 60 / 8, places=2)

    def test_persistence_and_read_only_snapshot(self):
        self.run_at(NOON, 6)
        self.assertTrue(self.store.maybe_flush())
        self.assertFalse(self.store.maybe_flush())
        original = self.path.read_bytes()
        snap = load_snapshot(state_path=self.path)
        self.assertEqual(original, self.path.read_bytes())
        reloaded = PersonalRecordsStore(state_path=self.path)
        self.assertEqual(reloaded.best, self.store.best)
        self.assertEqual(reloaded.today, self.store.today)
        self.assertEqual(snap['best_zero_sixty_sec'], 10)
        self.assertEqual(snap['timing_version'], TIMING_VERSION)

    def test_stale_day_cleared_on_load(self):
        self.run_at(NOON - DAY, 6)
        self.store.flush()
        reloaded = PersonalRecordsStore(state_path=self.path)
        self.assertEqual(reloaded.today, dict.fromkeys(reloaded.today))
        self.assertEqual(reloaded.best, self.store.best)

    def test_file_keeps_best_and_ignores_bad_numbers(self):
        self.path.write_text(json.dumps({
            'timing_version': TIMING_VERSION,
            'best_eighth_mile_sec': 9.5, 'best_zero_sixty_sec': 'NaN',
            'best_twenty_sixty_sec': -3, 'top_speed_mph': 90,
        }))
        store = PersonalRecordsStore(state_path=self.path)
        self.assertEqual(store.best, {'eighth_mile': 9.5, 'zero_sixty': None, 'twenty_sixty': None})

    def test_times_from_older_timing_are_dropped(self):
        self.path.write_text(json.dumps({
            'device_id': 'car', 'best_eighth_mile_sec': 9.5,
            'today_date': '2999-01-01', 'today_eighth_mile_sec': 9.5,
        }))
        store = PersonalRecordsStore(state_path=self.path)
        self.assertEqual(store.best, dict.fromkeys(store.best))
        self.assertEqual((store.today_date, store.device_id), ('', 'car'))
        self.assertEqual(json.loads(self.path.read_text())['timing_version'], TIMING_VERSION)

    def test_merge_keeps_fastest_and_newest_day(self):
        v = TIMING_VERSION
        self.store._merge_record({'timing_version': v, 'best_zero_sixty_sec': 7,
                                  'today_date': '2026-01-02', 'today_zero_sixty_sec': 9})
        self.store._merge_record({'timing_version': str(v), 'best_zero_sixty_sec': 8,
                                  'today_date': '2026-01-01', 'today_zero_sixty_sec': 6})
        self.assertEqual(self.store.best['zero_sixty'], 7)
        self.assertEqual((self.store.today_date, self.store.today['zero_sixty']), ('2026-01-02', 9))

    def test_lost_fix_invalidates_run(self):
        feeder = Feeder(self.store)
        samples = speeds(6)
        feeder.feed(samples[:8])
        feeder.feed([samples[8]], fix_ok=False)
        self.assertEqual(feeder.feed(samples[9:]), [])

    def test_reset_clears_everything(self):
        self.run_at(NOON, 6)
        self.store.reset()
        reloaded = PersonalRecordsStore(state_path=self.path)
        self.assertEqual(reloaded.best, dict.fromkeys(reloaded.best))
        self.assertEqual(reloaded.today, dict.fromkeys(reloaded.today))


if __name__ == '__main__': unittest.main()
