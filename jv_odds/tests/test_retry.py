"""実機の不安定動作（-503・通信エラー・COM 切断）に対する再試行とクリーンアップのテスト。"""
import unittest
from datetime import date, datetime
from unittest import mock

from jv_odds import db
from jv_odds.collector import OddsCollector
from jv_odds.config import Settings, retry_policy
from jv_odds.envcheck import WARN, OK, run_env_check
from jv_odds.jvlink import JVLinkClient, JVLinkError, describe_exception
from jv_odds.mock_jvlink import FakeJVLinkCOM, MockJVLinkClient, build_o1
from jv_odds.retry import RetryPolicy, call_with_retry

RID = "2026110705040201"


class com_error(Exception):
    """pywintypes.com_error の代用（クラス名で判定される）。"""


class TestPolicy(unittest.TestCase):
    def test_exponential_waits(self):
        p = RetryPolicy(max_attempts=6, base_wait=2, max_wait=30, jitter=0)
        self.assertEqual([p.wait_for(i) for i in range(1, 6)], [2, 4, 8, 16, 30])
        self.assertEqual(p.wait_for(1, code=-504), 30)  # メンテナンス中は上限まで待つ

    def test_jitter_bounds(self):
        p = RetryPolicy(base_wait=10, jitter=0.2)
        for _ in range(50):
            self.assertTrue(8 <= p.wait_for(1) <= 12)

    def test_settings_defaults(self):
        p = retry_policy(Settings())
        self.assertEqual((p.max_attempts, p.base_wait, p.max_wait), (5, 2.0, 30.0))


class TestCallWithRetry(unittest.TestCase):
    def setUp(self):
        self.sleeps, self.resets = [], []
        self.policy = RetryPolicy(max_attempts=5, base_wait=2, max_wait=30, jitter=0)

    def _run(self, func, history=None):
        return call_with_retry("test", func, self.policy, reset=lambda: self.resets.append(1),
                               sleep=self.sleeps.append, history=history)

    def test_recovers_after_503(self):
        errs = [JVLinkError("JVGets", -503, stage="JVGets"), JVLinkError("JVRTOpen", -502)]
        def f():
            if errs:
                raise errs.pop(0)
            return "ok"
        hist = []
        with self.assertLogs("jv_odds.retry", "WARNING") as cm:
            self.assertEqual(self._run(f, hist), "ok")
        self.assertEqual(self.sleeps, [2, 4])
        self.assertEqual(len(self.resets), 2)  # 失敗のたびにリセット
        self.assertEqual([h.ok for h in hist], [False, False, True])
        self.assertIn("stage=JVGets code=-503", cm.output[0])

    def test_fatal_not_retried(self):
        def f():
            raise JVLinkError("JVRTOpen", -302)
        with self.assertRaises(JVLinkError), self.assertLogs("jv_odds.retry", "ERROR"):
            self._run(f)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(self.resets), 1)

    def test_exhausted(self):
        calls = []
        def f():
            calls.append(1)
            raise JVLinkError("JVGets", -503)
        with self.assertRaises(JVLinkError) as cm, self.assertLogs("jv_odds.retry", "ERROR"):
            self._run(f)
        self.assertEqual(cm.exception.code, -503)
        self.assertEqual(len(calls), 5)
        self.assertEqual(self.sleeps, [2, 4, 8, 16])
        self.assertEqual(len(self.resets), 5)

    def test_com_error_retried_with_hresult(self):
        errs = [com_error(-2147023174, "RPC サーバーを利用できません。", None, None)]
        def f():
            if errs:
                raise errs.pop(0)
            return 1
        with self.assertLogs("jv_odds.retry", "WARNING") as cm:
            self.assertEqual(self._run(f), 1)
        self.assertIn("hresult=0x800706BA", cm.output[0])

    def test_reset_failure_does_not_abort(self):
        errs = [JVLinkError("JVGets", -503)]
        def f():
            if errs:
                raise errs.pop(0)
            return 1
        def bad_reset():
            raise RuntimeError("boom")
        with self.assertLogs("jv_odds.retry", "WARNING"):
            self.assertEqual(call_with_retry("t", f, self.policy, reset=bad_reset,
                                             sleep=self.sleeps.append), 1)


class StageCOM:
    """JVRTOpen / JVGets の戻り値を台本どおりに返す偽 COM。"""

    def __init__(self, open_code=0, gets=(), open_exc=None):
        self.open_code, self.gets, self.open_exc = open_code, list(gets), open_exc
        self.closed = self.cancelled = 0

    def JVRTOpen(self, spec, key):
        if self.open_exc:
            raise self.open_exc
        return self.open_code

    def JVGets(self, buff, size, filename):
        item = self.gets.pop(0) if self.gets else 0
        if isinstance(item, bytes):
            return len(item), memoryview(item), bytearray()
        return item, None, bytearray(b"0B31TEST.rtd")

    def JVFiledelete(self, f):
        pass

    def JVClose(self):
        self.closed += 1

    def JVCancel(self):
        self.cancelled += 1


class TestClientStages(unittest.TestCase):
    def _client(self, com, **kw):
        c = JVLinkClient(**kw)
        c._jv = com
        return c

    def test_gets_503_reports_stage_and_closes(self):
        rec = build_o1(RID, datetime(2026, 11, 7, 9, 0), {1: 2.0}, {1: (1.1, 1.2)}).encode("cp932")
        com = StageCOM(0, [rec, -503])
        c = self._client(com)
        with self.assertRaises(JVLinkError) as cm, self.assertLogs("jv_odds.jvlink", "ERROR") as lg:
            c.rt_records("0B31", RID)
        e = cm.exception
        self.assertEqual((e.code, e.stage, e.records_read, e.dataspec, e.key), (-503, "JVGets", 1, "0B31", RID))
        self.assertEqual(com.closed, 1)
        self.assertTrue(any("JVGets ループ内でエラー" in m and "code=-503" in m for m in lg.output))

    def test_rtopen_error_reports_stage(self):
        com = StageCOM(-502)
        with self.assertRaises(JVLinkError) as cm, self.assertLogs("jv_odds.jvlink", "ERROR"):
            self._client(com).rt_records("0B31", RID)
        self.assertEqual(cm.exception.stage, "JVRTOpen")
        self.assertEqual(com.closed, 1)

    def test_rtopen_exception_still_closes(self):
        com = StageCOM(open_exc=com_error(-2147023174, "RPC", None, None))
        with self.assertRaises(com_error), self.assertLogs("jv_odds.jvlink", "ERROR"):
            self._client(com).rt_records("0B31", RID)
        self.assertEqual(com.closed, 1)

    def test_downloading_forever_times_out(self):
        com = StageCOM(0, [-3] * 1000)
        c = self._client(com, download_timeout=0)
        clock = iter(range(0, 10000))
        with mock.patch("jv_odds.jvlink.time.sleep"), \
                mock.patch("jv_odds.jvlink.time.monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(JVLinkError) as cm:
                c.rt_records("0B31", RID)
        self.assertEqual((cm.exception.code, cm.exception.stage), (-502, "JVGets"))
        self.assertEqual(com.closed, 1)

    def test_reset_releases_com(self):
        com = StageCOM()
        c = self._client(com)
        c._opened = c._downloading = True
        with self.assertLogs("jv_odds.jvlink", "INFO"):
            c.reset()
        self.assertEqual((com.cancelled, com.closed), (1, 1))
        self.assertIsNone(c._jv)
        self.assertFalse(c._opened)

    def test_describe_exception(self):
        self.assertIn("stage=JVGets", describe_exception(JVLinkError("JVGets", -503, stage="JVGets")))
        self.assertEqual(describe_exception(ValueError("x")), "ValueError: x")


class AlwaysFail(MockJVLinkClient):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.rt_calls = 0
        self.resets = 0

    def reset(self):
        self.resets += 1

    def rt_records(self, dataspec, key):
        self.rt_calls += 1
        raise JVLinkError(f"JVGets({dataspec},{key})", -503, stage="JVGets")


class TestCircuitBreaker(unittest.TestCase):
    def test_degraded_after_first_exhausted_race(self):
        day = date(2026, 11, 7)
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        db.init_db(conn)
        client = AlwaysFail(race_dates=[day], races_per_venue=2)  # 4 レース
        c = OddsCollector(client, conn, Settings(retry_wait_seconds=0))
        c.refresh_races([day])
        sleeps = []
        c.sleep = sleeps.append
        with self.assertLogs("jv_odds", "WARNING"):
            res = c.run_cycle(datetime(2026, 11, 7, 9, 0))
        self.assertEqual(res.status, "ERROR")
        # 1 レース目: 5 回、残り 3 レース: 1 回ずつ（0B31 で失敗するので 0B32 は呼ばれない）
        self.assertEqual(client.rt_calls, 5 + 3)
        self.assertEqual(len(sleeps), 4)
        row = conn.execute("SELECT error_message FROM fetch_runs").fetchone()
        self.assertIn("stage=JVGets", row[0])
        self.assertIn("code=-503", row[0])
        # 次のサイクルは通常の再試行に戻る
        client.rt_calls = 0
        with self.assertLogs("jv_odds", "WARNING"):
            c.run_cycle(datetime(2026, 11, 7, 9, 10))
        self.assertEqual(client.rt_calls, 5 + 3)

    def test_recovery_clears_degraded(self):
        day = date(2026, 11, 7)
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        db.init_db(conn)

        class FailFirstRace(MockJVLinkClient):
            def reset(self):
                pass

            def rt_records(self, dataspec, key):
                if key == RID:  # 最初のレースだけ失敗し、以降は成功
                    raise JVLinkError("JVGets", -503, stage="JVGets")
                return super().rt_records(dataspec, key)

        c = OddsCollector(FailFirstRace(race_dates=[day], races_per_venue=2), conn,
                          Settings(retry_wait_seconds=0))
        c.refresh_races([day])
        c.sleep = lambda s: None
        with self.assertLogs("jv_odds", "WARNING"):
            res = c.run_cycle(datetime(2026, 11, 7, 9, 0))
        self.assertEqual((res.status, res.races_ok), ("PARTIAL", 3))
        self.assertFalse(c.degraded)


class TestEnvCheckRetry(unittest.TestCase):
    def test_attempts_reported(self):
        mockc = MockJVLinkClient(race_dates=[date(2026, 11, 7)])
        fails = {"0B31": 2}

        def flaky(spec, key):
            if fails.get(spec):
                fails[spec] -= 1
                raise JVLinkError(f"JVGets({spec})", -503, stage="JVGets")
            return mockc.rt_records(spec, key)

        with self.assertLogs("jv_odds", "WARNING"):
            rep = run_env_check("x", "UNKNOWN", race_key=RID, com_factory=FakeJVLinkCOM, fetch=flaky,
                                policy=RetryPolicy(5, 0, 0, 0), sleep=lambda s: None)
        st = {c.name: c.status for c in rep.checks}
        self.assertEqual(st["実取得 0B31"], OK)
        self.assertEqual(st["  0B31 試行 1"], WARN)
        self.assertEqual(st["  0B31 試行 2"], WARN)
        self.assertTrue(rep.ok)


if __name__ == "__main__":
    unittest.main()
