"""
JV-Link のモック。Windows/JV-Link が無い環境での動作確認・テスト用。

JV-Data 仕様と同じ固定長レイアウトでレコード (RA / O1 / O2) を組み立てて返すため、
実機と同じパーサ・保存処理の経路をそのまま検証できる。
"""
from __future__ import annotations

import logging
import random
from datetime import date, datetime, timedelta
from typing import Optional

from .jvlink import JVLinkError
from .records import ENCODING

log = logging.getLogger(__name__)


def _put(buf: bytearray, pos: int, value: str) -> None:
    b = value.encode(ENCODING)
    buf[pos - 1:pos - 1 + len(b)] = b


def _blank(length: int) -> bytearray:
    return bytearray(b" " * length)


def _header(buf: bytearray, spec: str, kubun: str, race_id: str) -> None:
    _put(buf, 1, spec)
    _put(buf, 3, kubun)
    _put(buf, 4, datetime.now().strftime("%Y%m%d"))
    _put(buf, 12, race_id)


def build_ra(race_id: str, race_name: str, post_time: str, tosu: int) -> str:
    buf = _blank(1272)
    _header(buf, "RA", "2", race_id)
    name = race_name.encode(ENCODING)[:60].decode(ENCODING, "ignore")
    _put(buf, 33, name)
    _put(buf, 874, post_time.replace(":", ""))
    _put(buf, 882, f"{tosu:02d}")
    _put(buf, 884, f"{tosu:02d}")
    buf[-2:] = b"\r\n"
    return buf.decode(ENCODING)


def _odds_str(v: Optional[float], width: int) -> str:
    if v is None:
        return "-" * width
    return f"{min(int(round(v * 10)), 10 ** width - 1):0{width}d}"


def build_o1(race_id: str, happyo: datetime, win: dict[int, Optional[float]],
             place: dict[int, tuple[float, float]], kubun: str = "1") -> str:
    buf = _blank(962)
    _header(buf, "O1", kubun, race_id)
    _put(buf, 28, happyo.strftime("%m%d%H%M"))
    _put(buf, 36, f"{len(win):02d}")
    _put(buf, 38, f"{len(win):02d}")
    _put(buf, 40, "777")
    order = sorted((u for u in win if win[u] is not None), key=lambda u: win[u])
    for i, uma in enumerate(sorted(win)):
        p = 44 + i * 8
        ninki = order.index(uma) + 1 if uma in order else 0
        _put(buf, p, f"{uma:02d}{_odds_str(win[uma], 4)}{ninki:02d}")
    for i, uma in enumerate(sorted(place)):
        p = 268 + i * 12
        lo, hi = place[uma]
        ninki = order.index(uma) + 1 if uma in order else 0
        _put(buf, p, f"{uma:02d}{_odds_str(lo, 4)}{_odds_str(hi, 4)}{ninki:02d}")
    _put(buf, 928, f"{123456:011d}")
    _put(buf, 939, f"{65432:011d}")
    _put(buf, 950, f"{0:011d}")
    buf[-2:] = b"\r\n"
    return buf.decode(ENCODING)


def build_o2(race_id: str, happyo: datetime, odds: dict[tuple[int, int], Optional[float]],
             kubun: str = "1") -> str:
    buf = _blank(2042)
    _header(buf, "O2", kubun, race_id)
    _put(buf, 28, happyo.strftime("%m%d%H%M"))
    n = len({u for k in odds for u in k})
    _put(buf, 36, f"{n:02d}")
    _put(buf, 38, f"{n:02d}")
    _put(buf, 40, "7")
    order = sorted((k for k in odds if odds[k] is not None), key=lambda k: odds[k])
    for i, (u1, u2) in enumerate(sorted(odds)):
        p = 41 + i * 13
        ninki = order.index((u1, u2)) + 1 if (u1, u2) in order else 0
        _put(buf, p, f"{u1:02d}{u2:02d}{_odds_str(odds[(u1, u2)], 6)}{ninki:03d}")
    _put(buf, 2030, f"{987654:011d}")
    buf[-2:] = b"\r\n"
    return buf.decode(ENCODING)


class MockJVLinkClient:
    """JVLinkClient と同じインターフェースを持つモック。"""

    def __init__(self, race_dates: Optional[list[date]] = None, fail_rate: float = 0.0,
                 seed: int = 0, races_per_venue: int = 12):
        today = date.today()
        self.race_dates = race_dates or [today]
        self.fail_rate = fail_rate
        self.rng = random.Random(seed)
        self.races_per_venue = races_per_venue
        self._base: dict[str, dict[int, float]] = {}
        self.calls: list[tuple] = []

    # JVLinkClient 互換
    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def reset(self) -> None:
        log.info("[mock] reset")

    def _maybe_fail(self, func: str) -> None:
        if self.fail_rate and self.rng.random() < self.fail_rate:
            raise JVLinkError(func, -502, "(mock) 通信エラーの模擬")

    def _race_ids(self) -> list[tuple[str, str, int]]:
        out = []
        for d in self.race_dates:
            for jyo in ("05", "08"):  # 東京・京都
                for r in range(1, self.races_per_venue + 1):
                    race_id = f"{d:%Y%m%d}{jyo}0402{r:02d}"
                    post = (datetime(2000, 1, 1, 9, 50) + timedelta(minutes=35 * (r - 1)
                            + (5 if jyo == "08" else 0))).strftime("%H:%M")
                    out.append((race_id, post, 10 + (r % 7)))
        return out

    def stored_records(self, dataspec: str, fromtime: str, option: int,
                       record_filter: Optional[set[str]] = None) -> list[str]:
        self.calls.append(("JVOpen", dataspec, fromtime, option))
        self._maybe_fail("JVOpen")
        return [build_ra(rid, f"モック{rid[-2:]}R", post, tosu) for rid, post, tosu in self._race_ids()]

    def rt_records(self, dataspec: str, key: str) -> list[str]:
        self.calls.append(("JVRTOpen", dataspec, key))
        self._maybe_fail(f"JVRTOpen({dataspec})")
        if dataspec == "0B15":
            return [build_ra(rid, f"モック{rid[-2:]}R", post, tosu)
                    for rid, post, tosu in self._race_ids() if rid.startswith(key)]
        tosu = next((t for rid, _, t in self._race_ids() if rid == key), 12)
        base = self._base.setdefault(key, {u: round(self.rng.uniform(1.5, 150), 1)
                                           for u in range(1, tosu + 1)})
        # ランダムウォークで少しずつ変化させる（一部の馬は据え置き → 差分保存の確認用）
        for u in base:
            if self.rng.random() < 0.6:
                base[u] = max(1.0, round(base[u] * self.rng.uniform(0.9, 1.1), 1))
        happyo = datetime.now().replace(second=0, microsecond=0)
        if dataspec in ("0B31", "0B41"):
            place = {u: (max(1.0, round(o / 4, 1)), max(1.1, round(o / 2.5, 1))) for u, o in base.items()}
            return [build_o1(key, happyo, dict(base), place)]
        if dataspec in ("0B32", "0B42"):
            q = {(a, b): round(base[a] * base[b] / 3, 1)
                 for a in base for b in base if a < b}
            return [build_o2(key, happyo, q)]
        return []
