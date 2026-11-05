import unittest
from datetime import date, datetime, time

from jv_odds import db
from jv_odds.collector import OddsCollector, is_target
from jv_odds.config import Settings
from jv_odds.jvlink import JVLinkError
from jv_odds.mock_jvlink import MockJVLinkClient, build_o1, build_o2
from jv_odds.records import parse_o1, parse_o2
from jv_odds.scheduler import final_fetch_times, next_tick, run_day

RID = "2026110705040201"


def mem_conn():
    conn = db.connect(":memory:")
    db.init_db(conn)
    return conn


class TestDiffSave(unittest.TestCase):
    def test_only_changes_are_saved(self):
        conn = mem_conn()
        t1, t2 = datetime(2026, 11, 7, 9, 0), datetime(2026, 11, 7, 9, 10)
        place = {1: (1.1, 1.3), 2: (2.0, 3.0), 3: (4.0, 6.0)}
        r1 = parse_o1(build_o1(RID, t1, {1: 2.0, 2: 5.0, 3: 10.0}, place))
        self.assertEqual(db.save_odds_record(conn, r1, "0B31"), 3)
        # 同一発表時刻の再受信は無視
        self.assertEqual(db.save_odds_record(conn, r1, "0B31"), 0)
        # 2 番だけ変化
        r2 = parse_o1(build_o1(RID, t2, {1: 2.0, 2: 5.5, 3: 10.0}, place))
        self.assertEqual(db.save_odds_record(conn, r2, "0B31"), 1)
        latest = {r["umaban"]: r["win_odds"]
                  for r in conn.execute("SELECT * FROM v_latest_win_place WHERE race_id=?", (RID,))}
        self.assertEqual(latest, {1: 2.0, 2: 5.5, 3: 10.0})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0], 2)

    def test_quinella_diff(self):
        conn = mem_conn()
        q = {(1, 2): 4.0, (1, 3): 8.0, (2, 3): 12.0}
        self.assertEqual(db.save_odds_record(
            conn, parse_o2(build_o2(RID, datetime(2026, 11, 7, 9, 0), q)), "0B32"), 3)
        q[(1, 3)] = 7.5
        self.assertEqual(db.save_odds_record(
            conn, parse_o2(build_o2(RID, datetime(2026, 11, 7, 9, 10), q)), "0B32"), 1)


class FlakyClient(MockJVLinkClient):
    """最初の n 回の JVRTOpen を失敗させる。"""
    def __init__(self, fail_times, **kw):
        super().__init__(**kw)
        self.fail_times = fail_times
        self.resets = 0

    def reset(self):
        self.resets += 1

    def rt_records(self, dataspec, key):
        if self.fail_times > 0 and dataspec.startswith("0B3"):
            self.fail_times -= 1
            raise JVLinkError("JVRTOpen", -502)
        return super().rt_records(dataspec, key)


class TestCollector(unittest.TestCase):
    def setUp(self):
        self.st = Settings(retry_count=1, retry_wait_seconds=0)
        self.day = date(2026, 11, 7)

    def test_retry_and_reset(self):
        client = FlakyClient(1, race_dates=[self.day], races_per_venue=1)
        c = OddsCollector(client, mem_conn(), self.st)
        c.refresh_races([self.day])
        res = c.run_cycle(datetime(2026, 11, 7, 9, 0))
        self.assertEqual(res.status, "OK")
        self.assertEqual(res.races_ok, 2)
        self.assertGreaterEqual(client.resets, 1)

    def test_failure_does_not_raise(self):
        client = FlakyClient(100, race_dates=[self.day], races_per_venue=1)
        c = OddsCollector(client, mem_conn(), self.st)
        c.refresh_races([self.day])
        res = c.run_cycle(datetime(2026, 11, 7, 9, 0))
        self.assertEqual(res.status, "ERROR")
        row = c.conn.execute("SELECT status, error_message FROM fetch_runs").fetchone()
        self.assertEqual(row["status"], "ERROR")
        self.assertIn("-502", row["error_message"])

    def test_window(self):
        conn = mem_conn()
        c = OddsCollector(MockJVLinkClient(race_dates=[self.day], races_per_venue=1), conn, self.st)
        c.refresh_races([self.day])
        race = db.races_on(conn, "2026-11-07")[0]  # 09:50 発走
        self.assertFalse(is_target(race, datetime(2026, 11, 7, 8, 0), self.st))
        self.assertTrue(is_target(race, datetime(2026, 11, 7, 9, 40), self.st))
        self.assertFalse(is_target(race, datetime(2026, 11, 7, 9, 50), self.st))


class TestScheduler(unittest.TestCase):
    def test_next_tick(self):
        self.assertEqual(next_tick(datetime(2026, 11, 7, 9, 3, 12), 10), datetime(2026, 11, 7, 9, 10))
        self.assertEqual(next_tick(datetime(2026, 11, 7, 9, 10, 0), 10), datetime(2026, 11, 7, 9, 20))
        self.assertEqual(next_tick(datetime(2026, 11, 7, 23, 55), 10), datetime(2026, 11, 8, 0, 0))

    def test_run_day_with_fake_clock(self):
        day = date(2026, 11, 7)
        clock = [datetime(2026, 11, 7, 8, 0)]
        st = Settings(retry_count=0, retry_wait_seconds=0, start_time=time(8, 30))
        client = MockJVLinkClient(race_dates=[day], races_per_venue=2)
        c = OddsCollector(client, mem_conn(), st)
        cycles = run_day(c, st, now_fn=lambda: clock[0],
                         sleep_fn=lambda s: clock.__setitem__(0, clock[0] + __import__("datetime").timedelta(seconds=s)))
        # 08:30～10:25 (最終 10:25 発走) を 10 分刻み + 締切直前取得
        self.assertGreater(cycles, 10)
        runs = c.conn.execute("SELECT COUNT(*) FROM fetch_runs").fetchone()[0]
        self.assertGreater(runs, 10)
        finals = final_fetch_times(db.races_on(c.conn, "2026-11-07"), st)
        self.assertIn(datetime(2026, 11, 7, 9, 47), finals)
        self.assertGreater(c.conn.execute("SELECT COUNT(*) FROM odds_quinella").fetchone()[0], 0)

    def test_run_day_no_races_exits(self):
        st = Settings(retry_count=0, retry_wait_seconds=0)
        client = MockJVLinkClient(race_dates=[date(2026, 11, 7)], races_per_venue=1)
        c = OddsCollector(client, mem_conn(), st)
        slept = []
        cycles = run_day(c, st, now_fn=lambda: datetime(2026, 11, 4, 9, 0), sleep_fn=slept.append)
        self.assertEqual((cycles, slept), (0, []))


if __name__ == "__main__":
    unittest.main()
