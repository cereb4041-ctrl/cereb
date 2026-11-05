"""JV-Data 仕様書 4.9.0.1 / JV-Link インターフェース仕様への準拠テスト。"""
import unittest
from datetime import date, datetime

from jv_odds import db, records as R
from jv_odds.collector import OddsCollector
from jv_odds.config import Settings
from jv_odds.envcheck import NG, OK, WARN, run_env_check
from jv_odds.jvlink import JVLinkClient, JVLinkError, make_race_key, validate_rt_key
from jv_odds.mock_jvlink import FakeJVLinkCOM, MockJVLinkClient, build_o1, build_o2, build_ra

RID = "2026110705040211"

# 仕様書「フォーマット」シートの位置(1始まり)・バイト数をそのまま転記した期待値
SPEC = {
    "RA": {"length": 1272, "競走名本題": (33, 60), "発走時刻": (874, 4), "変更前発走時刻": (878, 4),
           "登録頭数": (882, 2), "出走頭数": (884, 2)},
    "O1": {"length": 962, "発表月日時分": (28, 8), "登録頭数": (36, 2), "出走頭数": (38, 2),
           "発売フラグ": (40, 3), "複勝着払キー": (43, 1), "単勝オッズ": (44, 8, 28),
           "複勝オッズ": (268, 12, 28), "枠連オッズ": (604, 9, 36), "単勝票数合計": (928, 11),
           "複勝票数合計": (939, 11), "枠連票数合計": (950, 11)},
    "O2": {"length": 2042, "発売フラグ": (40, 1), "馬連オッズ": (41, 13, 153), "馬連票数合計": (2030, 11)},
}


class TestLayoutMatchesSpec(unittest.TestCase):
    def test_header(self):
        self.assertEqual(R.RACE_KEY, (12, 16))
        for f, exp in ((R.H_YEAR, (12, 4)), (R.H_MONTHDAY, (16, 4)), (R.H_JYO_CD, (20, 2)),
                       (R.H_KAIJI, (22, 2)), (R.H_NICHIJI, (24, 2)), (R.H_RACE_NUM, (26, 2))):
            self.assertEqual(tuple(f), exp)

    def test_ra(self):
        s = SPEC["RA"]
        self.assertEqual(R.RECORD_LENGTH["RA"], s["length"])
        self.assertEqual(tuple(R.RA_POS["race_name"]), s["競走名本題"])
        self.assertEqual(tuple(R.RA_POS["post_time"]), s["発走時刻"])
        self.assertEqual(tuple(R.RA_POS["post_time_before"]), s["変更前発走時刻"])
        self.assertEqual(tuple(R.RA_POS["toroku_tosu"]), s["登録頭数"])
        self.assertEqual(tuple(R.RA_POS["syusso_tosu"]), s["出走頭数"])

    def test_o1(self):
        s = SPEC["O1"]
        self.assertEqual(R.RECORD_LENGTH["O1"], s["length"])
        self.assertEqual(tuple(R.O_HAPPYO), s["発表月日時分"])
        self.assertEqual(tuple(R.O_TOROKU_TOSU), s["登録頭数"])
        self.assertEqual(tuple(R.O_SYUSSO_TOSU), s["出走頭数"])
        self.assertEqual(tuple(R.O1_SALE_FLAGS), s["発売フラグ"])
        self.assertEqual(tuple(R.O1_FUKU_CHAKU), s["複勝着払キー"])
        self.assertEqual(tuple(R.O1_WIN), s["単勝オッズ"])
        self.assertEqual(tuple(R.O1_PLACE), s["複勝オッズ"])
        self.assertEqual(tuple(R.O1_WAKUREN), s["枠連オッズ"])
        self.assertEqual([tuple(f) for f in R.O1_VOTES],
                         [s["単勝票数合計"], s["複勝票数合計"], s["枠連票数合計"]])

    def test_o2(self):
        s = SPEC["O2"]
        self.assertEqual(R.RECORD_LENGTH["O2"], s["length"])
        self.assertEqual(tuple(R.O2_SALE_FLAG), s["発売フラグ"])
        self.assertEqual(tuple(R.O2_QUINELLA), s["馬連オッズ"])
        self.assertEqual(tuple(R.O2_VOTES), s["馬連票数合計"])

    def test_blocks_contiguous(self):
        R.verify_layouts()

    def test_ra_post_time_byte_offset_with_multibyte_name(self):
        """競走名(全角)の後でもバイト位置 874 から発走時刻が読めること（str 位置ではなく byte 位置）。"""
        raw = build_ra(RID, "天皇賞（秋）　ＧⅠ", "15:40", 18).encode("cp932")
        self.assertEqual(raw[873:877], b"1540")
        r = R.parse_ra(raw)
        self.assertEqual((r.post_time, r.syusso_tosu), ("15:40", 18))
        self.assertTrue(r.race_name.startswith("天皇賞"))

    def test_length_error(self):
        raw = build_o1(RID, datetime(2026, 11, 7, 9, 0), {1: 2.0}, {1: (1.1, 1.2)}).encode("cp932")
        with self.assertRaises(R.RecordLengthError):
            R.parse_o1(raw[:900])
        R.parse_o1(raw[:-2])  # 末尾 CRLF なしは許容


class TestHappyo(unittest.TestCase):
    def test_unset_for_final_odds(self):
        # 仕様: 発表月日時分は「中間オッズのみ設定」
        self.assertIsNone(R.happyo_datetime("2026", "1107", "00000000"))
        self.assertIsNone(R.happyo_datetime("2026", "1107", "        "))

    def test_year_rollover(self):
        self.assertEqual(R.happyo_datetime("2027", "0105", "12311530"), "2026-12-31 15:30:00")
        self.assertEqual(R.happyo_datetime("2026", "1107", "11061530"), "2026-11-06 15:30:00")

    def test_o2_upper_limit(self):
        raw = build_o2(RID, datetime(2026, 11, 7, 9, 0), {(1, 2): 99999.9})
        self.assertEqual(R.parse_o2(raw).quinella[0].odds, 99999.9)


class TestFinalOddsSaved(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)

    def _o1(self, kubun, win, happyo=None):
        raw = bytearray(build_o1(RID, happyo or datetime(2026, 11, 7, 9, 0), win,
                                 {u: (1.0, 1.5) for u in win}, kubun=kubun).encode("cp932"))
        if happyo is None:
            raw[27:35] = b"00000000"  # 最終/確定オッズは発表月日時分なし
        return R.parse_o1(bytes(raw))

    def test_final_odds_not_dropped(self):
        mid = self._o1("1", {1: 2.0, 2: 5.0}, datetime(2026, 11, 7, 15, 30))
        self.assertEqual(db.save_odds_record(self.conn, mid, "0B31", "2026-11-07 15:30:20"), 2)
        final = self._o1("3", {1: 2.1, 2: 5.0})
        self.assertIsNone(final.happyo_at)
        self.assertEqual(db.save_odds_record(self.conn, final, "0B31", "2026-11-07 15:41:05"), 1)
        row = self.conn.execute("SELECT * FROM odds_snapshots ORDER BY happyo_at DESC").fetchone()
        self.assertEqual((row["data_kubun"], row["happyo_estimated"], row["happyo_at"]),
                         ("3", 1, "2026-11-07 15:41:00"))
        # 同じ最終オッズを再受信しても増えない
        self.assertEqual(db.save_odds_record(self.conn, final, "0B31", "2026-11-07 15:51:00"), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0], 2)
        # 確定 (区分 4) は値が同じでも区分が変わったのでスナップショットを残す
        conf = self._o1("4", {1: 2.1, 2: 5.0})
        db.save_odds_record(self.conn, conf, "0B31", "2026-11-07 16:01:00")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0], 3)

    def test_deleted_record_skipped(self):
        self.assertEqual(db.save_odds_record(self.conn, self._o1("0", {1: 2.0}), "0B31"), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0], 0)

    def test_migration_adds_column(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8")
                           .replace("    happyo_estimated INTEGER NOT NULL DEFAULT 0, -- 1 = 発表時刻が未設定で取得時刻で代用\n", ""))
        self.assertNotIn("happyo_estimated", {r[1] for r in conn.execute("PRAGMA table_info(odds_snapshots)")})
        db.init_db(conn)
        self.assertIn("happyo_estimated", {r[1] for r in conn.execute("PRAGMA table_info(odds_snapshots)")})


class TestRtKey(unittest.TestCase):
    def test_valid(self):
        for spec in ("0B31", "0B32", "0B41", "0B42"):
            validate_rt_key(spec, RID)
            validate_rt_key(spec, "202611070511")  # YYYYMMDDJJRR
        validate_rt_key("0B15", "20261107")
        self.assertEqual(make_race_key(2026, "1107", "05", 4, 2, 11), RID)

    def test_invalid(self):
        bad = [("0B31", "20261107"),          # オッズに日付キー
               ("0B31", "1107" + "05"),       # 月日+場（要望書の例。仕様外）
               ("0B31", "2026110705040213"),  # 13R
               ("0B31", "2026130705040201"),  # 13 月
               ("0B14", RID)]                 # 開催情報(一括)にレースキー
        for spec, key in bad:
            with self.assertRaises(JVLinkError) as cm:
                validate_rt_key(spec, key)
            self.assertEqual(cm.exception.code, -114, (spec, key))
            self.assertFalse(cm.exception.retryable)
        with self.assertRaises(JVLinkError) as cm:
            validate_rt_key("0B11x", RID)
        self.assertEqual(cm.exception.code, -111)

    def test_odds_specs_are_not_0b11_0b12(self):
        from jv_odds.collector import REALTIME_SPECS, TIMESERIES_SPECS
        self.assertEqual(REALTIME_SPECS, (("0B31", "O1"), ("0B32", "O2")))
        self.assertEqual(TIMESERIES_SPECS, (("0B41", "O1"), ("0B42", "O2")))


class GetsCOM:
    """JVGets 経由の読み出しと JVClose 呼び出しを検証する偽 COM。"""

    def __init__(self, open_codes, records):
        self.open_codes = list(open_codes)
        self.records = list(records)
        self.closed = 0
        self.opened = False

    def JVRTOpen(self, spec, key):
        if self.opened:
            return -202
        self.opened = True
        return self.open_codes.pop(0)

    def JVGets(self, buff, size, filename):
        if self.records:
            r = self.records.pop(0)
            return len(r), memoryview(r + b"\x00" * 10), bytearray()  # 余分なバイトは切り詰められること
        return 0, None, bytearray()

    def JVClose(self):
        self.closed += 1
        self.opened = False


class TestJVLinkClient(unittest.TestCase):
    def _client(self, com):
        c = JVLinkClient()
        c._jv = com
        return c

    def test_close_after_no_data(self):
        com = GetsCOM([-1, -1], [])
        c = self._client(com)
        self.assertEqual(c.rt_records("0B31", RID), [])
        self.assertEqual(c.rt_records("0B31", RID), [])  # -202 にならない
        self.assertEqual(com.closed, 2)

    def test_gets_returns_raw_sjis_bytes(self):
        raw = build_ra(RID, "ジャパンカップ", "15:40", 18).encode("cp932")
        com = GetsCOM([0], [raw])
        out = self._client(com).rt_records("0B15", "20261107")
        self.assertEqual(out, [raw])
        self.assertEqual(R.parse_ra(out[0]).race_name, "ジャパンカップ")
        self.assertEqual(com.closed, 1)

    def test_invalid_key_never_reaches_com(self):
        com = GetsCOM([0], [])
        with self.assertRaises(JVLinkError):
            self._client(com).rt_records("0B31", "110705")
        self.assertFalse(com.opened)


class TestEnvCheck(unittest.TestCase):
    def _status(self, report):
        return {c.name: c.status for c in report.checks}

    def test_all_ok(self):
        rep = run_env_check("JVDTLab.JVLink", "UNKNOWN", com_factory=FakeJVLinkCOM)
        st = self._status(rep)
        self.assertTrue(rep.ok, rep.render())
        self.assertEqual(st["JVRTOpen 不正キー"], OK)
        self.assertEqual(st["JVRTOpen キー形式 16桁"], OK)
        self.assertEqual(st["JVRTOpen 連続呼び出し"], OK)

    def test_auth_error(self):
        rep = run_env_check("x", "UNKNOWN", com_factory=lambda: FakeJVLinkCOM(auth_code=-302))
        self.assertFalse(rep.ok)
        self.assertEqual(self._status(rep)["利用キー認証"], NG)

    def test_jvinit_failure(self):
        rep = run_env_check("x", "UNKNOWN", com_factory=lambda: FakeJVLinkCOM(init_code=-101))
        self.assertEqual(self._status(rep)["JVInit"], NG)

    def test_12digit_rejected_is_warning(self):
        rep = run_env_check("x", "UNKNOWN", com_factory=lambda: FakeJVLinkCOM(reject_12digit=True))
        self.assertTrue(rep.ok)
        self.assertEqual(self._status(rep)["JVRTOpen キー形式 12桁"], WARN)

    def test_missing_close_detected(self):
        class NoClose(FakeJVLinkCOM):
            def JVClose(self):
                return 0
        rep = run_env_check("x", "UNKNOWN", com_factory=NoClose)
        self.assertEqual(self._status(rep)["JVRTOpen 連続呼び出し"], NG)

    def test_race_probe_with_mock(self):
        mock = MockJVLinkClient(race_dates=[date(2026, 11, 7)])
        rep = run_env_check("x", "UNKNOWN", race_key="2026110705040201",
                            com_factory=FakeJVLinkCOM, fetch=mock.rt_records)
        st = self._status(rep)
        self.assertEqual((st["実取得 0B31"], st["実取得 0B32"]), (OK, OK), rep.render())


class TestRaceFilter(unittest.TestCase):
    def test_non_jra_and_deleted_excluded(self):
        day = date(2026, 11, 7)

        class C(MockJVLinkClient):
            def stored_records(self, *a, **k):
                recs = super().stored_records(*a, **k)
                local = bytearray(recs[0]); local[19:21] = b"44"; local[2:3] = b"A"  # 地方
                deleted = bytearray(recs[1]); deleted[2:3] = b"0"
                return [bytes(local), bytes(deleted)] + recs[2:]

        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        db.init_db(conn)
        c = OddsCollector(C(race_dates=[day], races_per_venue=2), conn, Settings())
        self.assertEqual(c.refresh_races([day]), 2)


if __name__ == "__main__":
    unittest.main()
