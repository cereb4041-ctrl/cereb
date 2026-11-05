"""
JV-Data レコードのパーサ（COM に依存しない純粋な処理）。

準拠: JV-Data 仕様書 Ver.4.9.0.1「フォーマット」シート
  RA : レース詳細           1272 byte  (発走時刻・競走名・頭数の取得に使用)
  O1 : オッズ1（単複枠）      962 byte  単勝・複勝・枠連
  O2 : オッズ2（馬連）       2042 byte  馬連
  ※ O3=ワイド / O4=馬単 / O5=3連複 / O6=3連単。枠連は O1 に含まれる。

JV-Data の位置・桁数は Shift-JIS のバイト単位で定義されているため、レコードは
JVGets が返す bytes のまま扱う（str で受けた場合は cp932 にエンコードしてから切り出す）。
位置は仕様書の表記に合わせて 1 始まりで記述している。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple, Optional

ENCODING = "cp932"
SPEC_VERSION = "4.9.0.1"

# オッズ値の特殊表記 → odds_status
STATUS_NO_VOTE = "NO_VOTE"              # 0000… 無投票
STATUS_CANCEL_BEFORE = "CANCEL_BEFORE"  # ---- 発売前取消
STATUS_CANCEL_AFTER = "CANCEL_AFTER"    # **** 発売後取消
STATUS_NONE = "NONE"                    # 空白  登録なし


# ---------------------------------------------------------------------
# レイアウト定義（JV-Data 仕様書 4.9.0.1）
# ---------------------------------------------------------------------
class Field(NamedTuple):
    pos: int    # 1 始まりのバイト位置
    size: int


class Block(NamedTuple):
    """繰返し項目。pos から size バイトの要素が repeat 回並ぶ。"""
    pos: int
    size: int
    repeat: int

    def at(self, i: int) -> int:
        return self.pos + i * self.size

    @property
    def end(self) -> int:  # 次の項目の開始位置
        return self.pos + self.size * self.repeat


RECORD_LENGTH = {"RA": 1272, "O1": 962, "O2": 2042}

# 共通ヘッダ（RA / O1 / O2 で同一）
H_RECORD_SPEC = Field(1, 2)     # レコード種別ID
H_DATA_KUBUN = Field(3, 1)      # データ区分
H_MAKE_DATE = Field(4, 8)       # データ作成年月日
H_YEAR = Field(12, 4)           # 開催年        ┐
H_MONTHDAY = Field(16, 4)       # 開催月日      │
H_JYO_CD = Field(20, 2)         # 競馬場コード  │ キー (= レースID 16 桁)
H_KAIJI = Field(22, 2)          # 開催回        │
H_NICHIJI = Field(24, 2)        # 開催日目      │
H_RACE_NUM = Field(26, 2)       # レース番号    ┘
RACE_KEY = Field(12, 16)

# RA: レース詳細
RA_POS = {
    "data_kubun": H_DATA_KUBUN,
    "year": H_YEAR,
    "monthday": H_MONTHDAY,
    "jyo_cd": H_JYO_CD,
    "kaiji": H_KAIJI,
    "nichiji": H_NICHIJI,
    "race_num": H_RACE_NUM,
    "race_name": Field(33, 60),       # 項番12 競走名本題（全角30文字）
    "post_time": Field(874, 4),       # 項番44 発走時刻 hhmm
    "post_time_before": Field(878, 4),  # 項番45 変更前発走時刻
    "toroku_tosu": Field(882, 2),     # 項番46 登録頭数
    "syusso_tosu": Field(884, 2),     # 項番47 出走頭数
}

# O1 / O2 共通（ヘッダの後）
O_HAPPYO = Field(28, 8)           # 項番10 発表月日時分 MMDDhhmm（中間オッズのみ設定）
O_TOROKU_TOSU = Field(36, 2)      # 項番11 登録頭数
O_SYUSSO_TOSU = Field(38, 2)      # 項番12 出走頭数

# O1: オッズ1（単複枠）
O1_SALE_FLAGS = Field(40, 3)      # 項番13-15 発売フラグ 単勝/複勝/枠連 (0:なし 1:前取消 3:後取消 7:あり)
O1_FUKU_CHAKU = Field(43, 1)      # 項番16 複勝着払キー
O1_WIN = Block(44, 8, 28)         # 項番17 単勝: 馬番2 + オッズ4 + 人気順2
O1_PLACE = Block(268, 12, 28)     # 項番18 複勝: 馬番2 + 最低オッズ4 + 最高オッズ4 + 人気順2
O1_WAKUREN = Block(604, 9, 36)    # 項番19 枠連: 組番2 + オッズ5 + 人気順2（本システムでは保存しない）
O1_VOTES = (Field(928, 11), Field(939, 11), Field(950, 11))  # 項番20-22 単/複/枠 票数合計

# O2: オッズ2（馬連）
O2_SALE_FLAG = Field(40, 1)       # 項番13 発売フラグ 馬連
O2_QUINELLA = Block(41, 13, 153)  # 項番14 馬連: 組番4 + オッズ6 + 人気順3
O2_VOTES = Field(2030, 11)        # 項番15 馬連票数合計


def verify_layouts() -> None:
    """繰返し項目が隙間なく並び、レコード長 (末尾 CRLF 2byte 含む) と一致することを検査する。"""
    assert O1_WIN.end == O1_PLACE.pos
    assert O1_PLACE.end == O1_WAKUREN.pos
    assert O1_WAKUREN.end == O1_VOTES[0].pos
    assert O1_VOTES[2].pos + O1_VOTES[2].size + 2 == RECORD_LENGTH["O1"] + 1
    assert O2_QUINELLA.end == O2_VOTES.pos
    assert O2_VOTES.pos + O2_VOTES.size + 2 == RECORD_LENGTH["O2"] + 1


verify_layouts()


# ---------------------------------------------------------------------
# 基本関数
# ---------------------------------------------------------------------
def to_bytes(record) -> bytes:
    if isinstance(record, (bytes, bytearray, memoryview)):
        return bytes(record)
    return record.encode(ENCODING, errors="replace")


def _f(b: bytes, f: Field | int, length: Optional[int] = None) -> str:
    """1 始まりの位置からバイト列を取り出して文字列にする。"""
    pos, size = (f.pos, f.size) if isinstance(f, Field) else (f, length)
    return b[pos - 1:pos - 1 + size].decode(ENCODING, errors="replace")


def _int(s: str) -> Optional[int]:
    s = s.strip()
    return int(s) if s.isdigit() else None


def record_spec(record) -> str:
    """レコード種別ID (先頭 2 バイト)。"""
    head = record[:2]
    return head.decode("ascii", "replace") if isinstance(head, (bytes, bytearray)) else head


def check_length(b: bytes, spec: str) -> bool:
    """レコード長が仕様どおりか（末尾 CRLF の有無の揺れは許容）。"""
    expected = RECORD_LENGTH[spec]
    return len(b) in (expected, expected - 2)


class RecordLengthError(ValueError):
    pass


def _require_length(b: bytes, spec: str) -> None:
    if not check_length(b, spec):
        raise RecordLengthError(
            f"{spec} レコード長が仕様 ({RECORD_LENGTH[spec]} byte) と一致しません: {len(b)} byte")


def parse_odds_value(s: str, decimals: int = 1) -> tuple[Optional[float], Optional[str]]:
    """'0123' → (12.3, None)。特殊値は (None, status)。上限値 ('9999' 等) は上限値のまま返す。"""
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


def happyo_datetime(year: str, monthday: str, mmddhhmm: str) -> Optional[str]:
    """
    発表月日時分 (MMDDhhmm) を開催年で補って ISO 文字列にする。
      - 仕様上「中間オッズのみ設定」のため、最終/確定などで未設定 (0 / 空白) の場合は None
      - 年は持たないので、1 月開催で発表月が 12 月なら前年とする（前日売の年またぎ）
    """
    s = mmddhhmm.strip()
    if len(s) != 8 or not s.isdigit() or s == "00000000":
        return None
    try:
        y = int(year)
        if monthday[:2] == "01" and s[:2] == "12":
            y -= 1
        dt = datetime.strptime(f"{y:04d}{s}", "%Y%m%d%H%M")
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


def _hhmm(s: str) -> Optional[str]:
    s = s.strip()
    if len(s) != 4 or not s.isdigit() or s == "0000":
        return None
    hh, mm = int(s[:2]), int(s[2:])
    if hh > 23 or mm > 59:
        return None
    return f"{s[:2]}:{s[2:]}"


def parse_ra(record) -> RaceInfo:
    b = to_bytes(record)
    _require_length(b, "RA")
    g = {k: _f(b, f) for k, f in RA_POS.items()}
    return RaceInfo(
        race_id=_f(b, RACE_KEY),
        race_date=f"{g['year']}-{g['monthday'][:2]}-{g['monthday'][2:]}",
        jyo_cd=g["jyo_cd"],
        kaiji=_int(g["kaiji"]) or 0,
        nichiji=_int(g["nichiji"]) or 0,
        race_num=_int(g["race_num"]) or 0,
        race_name=g["race_name"].replace("　", " ").strip(),
        post_time=_hhmm(g["post_time"]),
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
    data_kubun: str           # 1:中間 2:前日売最終 3:最終 4:確定 5:確定(月曜) 9:中止 0:削除
    happyo_at: Optional[str]  # 中間オッズ以外は None（仕様上未設定）
    toroku_tosu: Optional[int]
    syusso_tosu: Optional[int]
    sale_flags: str
    total_votes: str
    win_place: list[WinPlaceOdds] = field(default_factory=list)
    quinella: list[QuinellaOdds] = field(default_factory=list)


def _header(b: bytes) -> dict:
    return {
        "race_id": _f(b, RACE_KEY),
        "data_kubun": _f(b, H_DATA_KUBUN),
        "happyo_at": happyo_datetime(_f(b, H_YEAR), _f(b, H_MONTHDAY), _f(b, O_HAPPYO)),
        "toroku_tosu": _int(_f(b, O_TOROKU_TOSU)),
        "syusso_tosu": _int(_f(b, O_SYUSSO_TOSU)),
    }


def parse_o1(record) -> OddsRecord:
    """O1: 単勝・複勝を馬番単位で取り出す（枠連は保存対象外）。"""
    b = to_bytes(record)
    _require_length(b, "O1")
    h = _header(b)

    win: dict[int, tuple] = {}
    for i in range(O1_WIN.repeat):
        p = O1_WIN.at(i)
        uma = _int(_f(b, p, 2))
        if not uma:
            continue
        odds, st = parse_odds_value(_f(b, p + 2, 4))
        win[uma] = (odds, _int(_f(b, p + 6, 2)), st)

    place: dict[int, tuple] = {}
    for i in range(O1_PLACE.repeat):
        p = O1_PLACE.at(i)
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
        sale_flags=_f(b, O1_SALE_FLAGS),
        total_votes=",".join(_f(b, f).strip() for f in O1_VOTES),
        win_place=rows,
        **h,
    )


def parse_o2(record) -> OddsRecord:
    """O2: 馬連を組番単位で取り出す。"""
    b = to_bytes(record)
    _require_length(b, "O2")
    h = _header(b)
    rows = []
    for i in range(O2_QUINELLA.repeat):
        p = O2_QUINELLA.at(i)
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
        sale_flags=_f(b, O2_SALE_FLAG),
        total_votes=_f(b, O2_VOTES).strip(),
        quinella=rows,
        **h,
    )


PARSERS = {"O1": parse_o1, "O2": parse_o2}
