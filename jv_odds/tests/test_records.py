import unittest
from datetime import datetime

from jv_odds.mock_jvlink import build_o1, build_o2, build_ra
from jv_odds.records import (STATUS_CANCEL_AFTER, STATUS_CANCEL_BEFORE, STATUS_NO_VOTE,
                             parse_o1, parse_o2, parse_odds_value, parse_ra, to_bytes)

RID = "2026110705040201"


class TestOddsValue(unittest.TestCase):
    def test_values(self):
        self.assertEqual(parse_odds_value("0123"), (12.3, None))
        self.assertEqual(parse_odds_value("9999"), (999.9, None))
        self.assertEqual(parse_odds_value("0000"), (None, STATUS_NO_VOTE))
        self.assertEqual(parse_odds_value("----"), (None, STATUS_CANCEL_BEFORE))
        self.assertEqual(parse_odds_value("****"), (None, STATUS_CANCEL_AFTER))


class TestRecords(unittest.TestCase):
    def test_ra(self):
        raw = build_ra(RID, "アルゼンチン共和国杯", "15:40", 16)
        self.assertEqual(len(to_bytes(raw)), 1272)
        r = parse_ra(raw)
        self.assertEqual(r.race_id, RID)
        self.assertEqual(r.race_date, "2026-11-07")
        self.assertEqual((r.jyo_cd, r.kaiji, r.nichiji, r.race_num), ("05", 4, 2, 1))
        self.assertEqual(r.race_name, "アルゼンチン共和国杯")
        self.assertEqual(r.post_time, "15:40")
        self.assertEqual(r.syusso_tosu, 16)

    def test_o1(self):
        happyo = datetime(2026, 11, 7, 9, 30)
        raw = build_o1(RID, happyo, {1: 3.2, 2: 12.5, 3: None},
                       {1: (1.1, 1.5), 2: (2.0, 3.4), 3: (1.0, 1.0)})
        self.assertEqual(len(to_bytes(raw)), 962)
        o = parse_o1(raw)
        self.assertEqual(o.race_id, RID)
        self.assertEqual(o.happyo_at, "2026-11-07 09:30:00")
        by = {w.umaban: w for w in o.win_place}
        self.assertEqual(by[1].win_odds, 3.2)
        self.assertEqual(by[1].win_ninki, 1)
        self.assertEqual((by[2].place_odds_min, by[2].place_odds_max), (2.0, 3.4))
        self.assertIsNone(by[3].win_odds)
        self.assertEqual(by[3].odds_status, STATUS_CANCEL_BEFORE)

    def test_o2(self):
        raw = build_o2(RID, datetime(2026, 11, 7, 9, 30), {(1, 2): 5.6, (1, 3): 123.4, (2, 3): None})
        self.assertEqual(len(to_bytes(raw)), 2042)
        o = parse_o2(raw)
        by = {(q.umaban1, q.umaban2): q for q in o.quinella}
        self.assertEqual(by[(1, 2)].odds, 5.6)
        self.assertEqual(by[(1, 2)].ninki, 1)
        self.assertEqual(by[(1, 3)].odds, 123.4)
        self.assertEqual(by[(2, 3)].odds_status, STATUS_CANCEL_BEFORE)


if __name__ == "__main__":
    unittest.main()
