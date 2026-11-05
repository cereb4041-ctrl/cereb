"""
JV-Data レコードのパーサ（COM に依存しない純粋な処理）。

対応レコード:
  RA : レース詳細           (発走時刻・競走名の取得に使用)
  O1 : オッズ(単勝・複勝・枠連)  レコード長 962 byte
  O2 : オッズ(馬連)            レコード長 2042 byte

JV-Data の位置・桁数は Shift-JIS のバイト単位で定義されているため、
JVRead が返す文字列は必ず cp932 にエンコードしてからスライスする。
位置は JV-Data 仕様書の表記に合わせて 1 始まりで記述している。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

ENCODING = "cp932"

# オッズ値の特殊表記 → odds_status
STATUS_NO_VOTE = "NO_VOTE"              # 0000… 無投票
STATUS_CANCEL_BEFORE = "CANCEL_BEFORE"  # ---- 発売前取消
STATUS_CANCEL_AFTER = "CANCEL_AFTER"    # **** 発売後取消
STATUS_NONE = "NONE"                    # 空白  登録なし


def to_bytes(record) -> bytes:
    if isinstance(record, bytes):
        return record
    return record.encode(ENCODING, errors="replace")


def _f(b: bytes, pos: int, length: int) -> str:
    """1 始まりの位置 pos から length バイトを文字列として取り出す。"""
    return b[pos - 1:pos - 1 + length].decode(ENCODING, errors="replace")


def _int(s: str) -> Optional[int]:
    s = s.strip()
    return int(s) if s.isdigit() else None


def parse_odds_value(s: str, decimals: int = 1) -> tuple[Optional[float], Optional[str]]:
    """'0123' → (12.3, None)。特殊値は (None, status)。"""
    if not s.strip():
        return None, STATUS_NONE
    if set(s) == {"-"}:
        return None, STATUS_CANCEL_BEFORE
    if set(s) == {"*"}:
        return None, STATUS_CANCEL_AFTER
    if not s.isdigit():
        return None, STATUS_NONE
    v = int(s)
    if v == 0:
        return None, STATUS_NO_VOTE
    return v / (10 ** decimals), None


def race_id_of(b: bytes) -> str:
    """共通ヘッダ (位置12～27) から 16 桁のレース ID を作る。"""
    return _f(b, 12, 16)


def happyo_datetime(year: str, mmddhhmm: str) -> Optional[str]:
    """発表月日時分 (MMDDhhmm) に開催年を補って ISO 文字列にする。"""
    try:
        dt = datetime.strptime(year + mmddhhmm, "%Y%m%d%H%M")
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------
# RA: レース詳細
# ---------------------------------------------------------------------
@dataclass
class RaceInfo:
    race_id: str
    race_date: str
    jyo_cd: str
    kaiji: int
    nichiji: int
    race_num: int
    race_name: str
    post_time: Optional[str]
    toroku_tosu: Optional[int]
    syusso_tosu: Optional[int]
    data_kubun: str


# RA レコードの主要項目位置 (JV-Data 仕様書 4.x)
RA_POS = {
    "data_kubun": (3, 1),
    "year": (12, 4),
    "monthday": (16, 4),
    "jyo_cd": (20, 2),
    "kaiji": (22, 2),
    "nichiji": (24, 2),
    "race_num": (26, 2),
    "race_name": (33, 60),
    "post_time": (874, 4),
    "toroku_tosu": (882, 2),
    "syusso_tosu": (884, 2),
}


def parse_ra(record) -> RaceInfo:
    b = to_bytes(record)
    g = {k: _f(b, p, n) for k, (p, n) in RA_POS.items()}
    pt = g["post_time"].strip()
    post_time = f"{pt[:2]}:{pt[2:]}" if len(pt) == 4 and pt.isdigit() and pt != "0000" else None
    return RaceInfo(
        race_id=race_id_of(b),
        race_date=f"{g['year']}-{g['monthday'][:2]}-{g['monthday'][2:]}",
        jyo_cd=g["jyo_cd"],
        kaiji=_int(g["kaiji"]) or 0,
        nichiji=_int(g["nichiji"]) or 0,
        race_num=_int(g["race_num"]) or 0,
        race_name=g["race_name"].replace("　", " ").strip(),
        post_time=post_time,
        toroku_tosu=_int(g["toroku_tosu"]),
        syusso_tosu=_int(g["syusso_tosu"]),
        data_kubun=g["data_kubun"],
    )


# ---------------------------------------------------------------------
# O1 / O2: オッズ
# ---------------------------------------------------------------------
@dataclass
class WinPlaceOdds:
    umaban: int
    win_odds: Optional[float]
    win_ninki: Optional[int]
    place_odds_min: Optional[float]
    place_odds_max: Optional[float]
    place_ninki: Optional[int]
    odds_status: Optional[str]


@dataclass
class QuinellaOdds:
    umaban1: int
    umaban2: int
    odds: Optional[float]
    ninki: Optional[int]
    odds_status: Optional[str]


@dataclass
class OddsRecord:
    record_type: str          # 'O1' or 'O2'
    race_id: str
    data_kubun: str
    happyo_at: Optional[str]
    toroku_tosu: Optional[int]
    syusso_tosu: Optional[int]
    sale_flags: str
    total_votes: str
    win_place: list[WinPlaceOdds] = field(default_factory=list)
    quinella: list[QuinellaOdds] = field(default_factory=list)


def _header(b: bytes) -> dict:
    year = _f(b, 12, 4)
    return {
        "race_id": race_id_of(b),
        "data_kubun": _f(b, 3, 1),
        "happyo_at": happyo_datetime(year, _f(b, 28, 8)),
        "toroku_tosu": _int(_f(b, 36, 2)),
        "syusso_tosu": _int(_f(b, 38, 2)),
    }


def parse_o1(record) -> OddsRecord:
    """O1: 単勝(28頭×8byte, 位置44) / 複勝(28頭×12byte, 位置268)。枠連は保存対象外。"""
    b = to_bytes(record)
    h = _header(b)
    win: dict[int, tuple] = {}
    for i in range(28):
        p = 44 + i * 8
        uma = _int(_f(b, p, 2))
        if not uma:
            continue
        odds, st = parse_odds_value(_f(b, p + 2, 4))
        win[uma] = (odds, _int(_f(b, p + 6, 2)), st)

    place: dict[int, tuple] = {}
    for i in range(28):
        p = 268 + i * 12
        uma = _int(_f(b, p, 2))
        if not uma:
            continue
        lo, st_lo = parse_odds_value(_f(b, p + 2, 4))
        hi, _ = parse_odds_value(_f(b, p + 6, 4))
        place[uma] = (lo, hi, _int(_f(b, p + 10, 2)), st_lo)

    rows = []
    for uma in sorted(set(win) | set(place)):
        w_odds, w_ninki, w_st = win.get(uma, (None, None, STATUS_NONE))
        p_lo, p_hi, p_ninki, p_st = place.get(uma, (None, None, None, STATUS_NONE))
        if w_st == STATUS_NONE and p_st == STATUS_NONE:
            continue  # 登録なし
        # 状態は単勝を優先し、単勝が正常で複勝のみ異常な場合は複勝の状態を採用
        if w_st not in (None, STATUS_NONE):
            status = w_st
        elif p_lo is None and p_st != STATUS_NONE:
            status = p_st
        else:
            status = None
        rows.append(WinPlaceOdds(uma, w_odds, w_ninki, p_lo, p_hi, p_ninki, status))

    return OddsRecord(
        record_type="O1",
        sale_flags=_f(b, 40, 3),
        total_votes=",".join(_f(b, p, 11).strip() for p in (928, 939, 950)),
        win_place=rows,
        **h,
    )


def parse_o2(record) -> OddsRecord:
    """O2: 馬連 153組×13byte (組番4, オッズ6, 人気3), 位置41。"""
    b = to_bytes(record)
    h = _header(b)
    rows = []
    for i in range(153):
        p = 41 + i * 13
        kumi = _f(b, p, 4)
        u1, u2 = _int(kumi[:2]), _int(kumi[2:])
        if not u1 or not u2:
            continue
        odds, st = parse_odds_value(_f(b, p + 4, 6))
        if st == STATUS_NONE:
            continue
        rows.append(QuinellaOdds(min(u1, u2), max(u1, u2), odds, _int(_f(b, p + 10, 3)), st))
    return OddsRecord(
        record_type="O2",
        sale_flags=_f(b, 40, 1),
        total_votes=_f(b, 2030, 11).strip(),
        quinella=rows,
        **h,
    )


PARSERS = {"O1": parse_o1, "O2": parse_o2}


def record_spec(record) -> str:
    return record[:2] if isinstance(record, str) else record[:2].decode("ascii", "replace")
