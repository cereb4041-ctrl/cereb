"""
エントリー判断モジュール。
スクリーニング済み候補に対して詳細なエントリー条件をチェックする。
"""
import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz
import yfinance as yf

logger = logging.getLogger(__name__)

CANDIDATES_FILE = Path("candidates.json")
JST = pytz.timezone("Asia/Tokyo")

# 日足タッチ検出
DAILY_TOUCH_TOLERANCE = 0.015   # 20日線の1.5%以内
DAILY_TOUCH_LOOKBACK  = 30      # 直近30日を走査
DAILY_TOUCH_GAP       = 3       # 同一イベントとみなす最大間隔（日）
DAILY_TOUCH_CUTOFF    = -10     # 直近10日以内のタッチのみ有効
MAX_BARS_SINCE_TOUCH  = 4       # タッチ後4本以内

# 週足5週線タッチ
WEEKLY_5W_TOLERANCE   = 0.015
WEEKLY_5W_LOOKBACK    = 26
WEEKLY_5W_GAP         = 3
WEEKLY_5W_CUTOFF      = -8

# リスクリワード
STOP_LOSS_PCT = 0.025           # エントリー価格の2.5%下
RR_MIN        = 2.0


@dataclass
class EntryCheckResult:
    ticker: str
    name: str
    price: float
    # 日足
    daily_ma60_up: bool
    daily_po: bool
    daily_touch_count: int      # 0=なし, 1=1回目, 2=2回目
    daily_touch_valid: bool
    bars_since_touch: int
    bars_ok: bool
    # 週足
    weekly_ma20_up: bool
    weekly_5w_touch_count: int
    weekly_5w_touch_valid: bool
    # 寄り付き
    opening_checked: bool
    gap_up: Optional[bool]
    first_candle_bullish: Optional[bool]
    prev_close: Optional[float]    # 前日終値
    open_price: Optional[float]    # 本日寄り付き値
    # エントリー情報
    entry_price: float
    stop_loss: float
    target_price: float
    rr_ratio: float
    rr_ok: bool
    # 総合
    all_met: bool


# ─────────────────────────────────────────────
# 候補の保存・読み込み
# ─────────────────────────────────────────────

def save_candidates(candidates: list) -> None:
    """CandidateオブジェクトのリストをJSONに保存する。"""
    from dataclasses import asdict
    data = {
        "date": date.today().isoformat(),
        "candidates": [asdict(c) for c in candidates],
    }
    CANDIDATES_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("候補を保存: %d 銘柄 → %s", len(candidates), CANDIDATES_FILE)


def load_candidates() -> list[dict]:
    """保存済み候補リストを読み込む。"""
    if not CANDIDATES_FILE.exists():
        logger.info("候補ファイルなし: %s", CANDIDATES_FILE)
        return []
    data = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
    logger.info("候補読み込み: %d 銘柄（%s 時点）", len(data["candidates"]), data["date"])
    return data["candidates"]


# ─────────────────────────────────────────────
# 内部ユーティリティ
# ─────────────────────────────────────────────

def _merge_events(indices: list[int], gap: int) -> list[list[int]]:
    if not indices:
        return []
    events = [[indices[0]]]
    for idx in indices[1:]:
        if idx - events[-1][-1] <= gap:
            events[-1].append(idx)
        else:
            events.append([idx])
    return events


def _fix_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def _fetch(ticker: str, period: str, interval: str) -> pd.DataFrame:
    for attempt in range(3):
        try:
            df = yf.download(ticker, period=period, interval=interval,
                             auto_adjust=True, progress=False)
            if not df.empty:
                return _fix_columns(df)
        except Exception as e:
            logger.debug("%s fetch error (attempt %d): %s", ticker, attempt + 1, e)
            time.sleep(3)
    return pd.DataFrame()


# ─────────────────────────────────────────────
# 各条件チェック
# ─────────────────────────────────────────────

def _check_daily_ma20_touch(close: pd.Series, low: pd.Series,
                             ma5: pd.Series, ma20: pd.Series,
                             ma60: pd.Series) -> tuple[int, int]:
    """
    日足20日線タッチ検出。
    Returns (touch_count, bars_since_last_touch)
    """
    n = len(close)
    touch_idx = []
    for rel in range(-DAILY_TOUCH_LOOKBACK, 0):
        i = n + rel
        if i < 0 or pd.isna(ma20.iloc[i]) or pd.isna(ma60.iloc[i]):
            continue
        po_ok  = float(ma5.iloc[i]) > float(ma20.iloc[i]) > float(ma60.iloc[i])
        touched = float(low.iloc[i]) <= float(ma20.iloc[i]) * (1 + DAILY_TOUCH_TOLERANCE)
        up      = float(close.iloc[i]) > float(ma20.iloc[i])
        if po_ok and touched and up:
            touch_idx.append(rel)

    events = _merge_events(touch_idx, DAILY_TOUCH_GAP)
    if not (1 <= len(events) <= 2):
        return 0, 999
    if events[-1][-1] < DAILY_TOUCH_CUTOFF:
        return 0, 999

    last_touch_abs = n + events[-1][-1]
    bars_since = (n - 1) - last_touch_abs
    return len(events), bars_since


def _check_weekly_5w_touch(ma5: pd.Series, ma20: pd.Series) -> int:
    """
    週足5週線が20週線に触れた回数を検出。
    Returns touch_count (0, 1, 2)
    """
    n = len(ma5)
    touch_idx = []
    for rel in range(-WEEKLY_5W_LOOKBACK, 0):
        i = n + rel
        if i < 0 or pd.isna(ma5.iloc[i]) or pd.isna(ma20.iloc[i]):
            continue
        diff = abs(float(ma5.iloc[i]) - float(ma20.iloc[i])) / float(ma20.iloc[i])
        if diff <= WEEKLY_5W_TOLERANCE:
            touch_idx.append(rel)

    events = _merge_events(touch_idx, WEEKLY_5W_GAP)
    if not (1 <= len(events) <= 2):
        return 0
    if events[-1][-1] < WEEKLY_5W_CUTOFF:
        return 0
    return len(events)


def _fetch_opening(ticker: str) -> tuple[Optional[bool], Optional[bool], Optional[float], Optional[float]]:
    """
    当日の寄り付き判断（5分足）。
    JST 9:00 の足が陽線かつギャップアップか確認する。
    Returns (gap_up, first_candle_bullish, prev_close, open_price)
    """
    try:
        df5 = _fetch(ticker, period="2d", interval="5m")
        daily = _fetch(ticker, period="3d", interval="1d")
        if df5.empty or daily.empty or len(daily) < 2:
            return None, None, None, None

        prev_close = float(daily["Close"].dropna().iloc[-2])

        # タイムゾーン変換
        if df5.index.tzinfo is None:
            df5.index = df5.index.tz_localize("UTC")
        df5.index = df5.index.tz_convert(JST)

        today = pd.Timestamp.now(tz=JST).date()
        today_bars = df5[df5.index.date == today]
        if today_bars.empty:
            return None, None, None, None

        first = today_bars.iloc[0]
        open_price           = float(first["Open"])
        gap_up               = open_price > prev_close
        first_candle_bullish = float(first["Close"]) > open_price
        return gap_up, first_candle_bullish, prev_close, open_price

    except Exception as e:
        logger.debug("Opening fetch error %s: %s", ticker, e)
        return None, None, None, None


# ─────────────────────────────────────────────
# メイン判定
# ─────────────────────────────────────────────

def check_entry(candidate: dict, check_opening: bool = True) -> EntryCheckResult:
    """1銘柄のエントリー条件を全てチェックする。"""
    ticker = candidate["ticker"]
    name   = candidate.get("name", ticker)
    price  = candidate.get("price", 0.0)

    # ── 日足 ─────────────────────────────────
    daily = _fetch(ticker, period="6mo", interval="1d")
    if daily.empty or len(daily) < 65:
        # データ不足は全条件 False
        return _no_data_result(ticker, name, price, check_opening)

    dc   = daily["Close"].astype(float)
    dl   = daily["Low"].astype(float)
    dma5  = dc.rolling(5).mean()
    dma20 = dc.rolling(20).mean()
    dma60 = dc.rolling(60).mean()

    daily_ma60_up = float(dma60.iloc[-1]) > float(dma60.iloc[-5])
    daily_po      = float(dma5.iloc[-1]) > float(dma20.iloc[-1]) > float(dma60.iloc[-1])
    d_touch_count, bars_since = _check_daily_ma20_touch(dc, dl, dma5, dma20, dma60)
    daily_touch_valid = (1 <= d_touch_count <= 2)
    bars_ok           = (bars_since <= MAX_BARS_SINCE_TOUCH)

    # ── 週足 ─────────────────────────────────
    weekly = _fetch(ticker, period="2y", interval="1wk")
    weekly_ma20_up      = False
    weekly_5w_count     = 0
    weekly_5w_valid     = False
    if not weekly.empty and len(weekly) >= 25:
        wc    = weekly["Close"].astype(float)
        wma5  = wc.rolling(5).mean()
        wma20 = wc.rolling(20).mean()
        weekly_ma20_up  = float(wma20.iloc[-1]) > float(wma20.iloc[-5])
        weekly_5w_count = _check_weekly_5w_touch(wma5, wma20)
        weekly_5w_valid = (1 <= weekly_5w_count <= 2)

    # ── エントリー価格 / 損切り / RR ─────────
    entry_price  = round(float(dma20.iloc[-1]), 1)
    stop_loss    = round(entry_price * (1 - STOP_LOSS_PCT), 1)
    target_price = round(float(dc.iloc[-60:].max()), 1)
    risk         = entry_price - stop_loss
    reward       = target_price - entry_price
    rr_ratio     = round(reward / risk, 2) if risk > 0 else 0.0
    rr_ok        = rr_ratio >= RR_MIN

    # ── 寄り付き ──────────────────────────────
    gap_up = first_candle_bullish = None
    prev_close = open_price = None
    opening_checked = False
    if check_opening:
        gap_up, first_candle_bullish, prev_close, open_price = _fetch_opening(ticker)
        opening_checked = gap_up is not None

    # ── 総合判断 ─────────────────────────────
    basic = (daily_ma60_up and daily_po and daily_touch_valid and bars_ok
             and weekly_ma20_up and weekly_5w_valid and rr_ok)
    opening_pass = (not opening_checked) or (gap_up is True and first_candle_bullish is True)
    all_met = basic and opening_pass

    return EntryCheckResult(
        ticker=ticker, name=name, price=price,
        daily_ma60_up=daily_ma60_up,
        daily_po=daily_po,
        daily_touch_count=d_touch_count,
        daily_touch_valid=daily_touch_valid,
        bars_since_touch=bars_since,
        bars_ok=bars_ok,
        weekly_ma20_up=weekly_ma20_up,
        weekly_5w_touch_count=weekly_5w_count,
        weekly_5w_touch_valid=weekly_5w_valid,
        opening_checked=opening_checked,
        gap_up=gap_up,
        first_candle_bullish=first_candle_bullish,
        prev_close=prev_close,
        open_price=open_price,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target_price=target_price,
        rr_ratio=rr_ratio,
        rr_ok=rr_ok,
        all_met=all_met,
    )


def _no_data_result(ticker, name, price, check_opening) -> EntryCheckResult:
    return EntryCheckResult(
        ticker=ticker, name=name, price=price,
        daily_ma60_up=False, daily_po=False,
        daily_touch_count=0, daily_touch_valid=False,
        bars_since_touch=999, bars_ok=False,
        weekly_ma20_up=False, weekly_5w_touch_count=0, weekly_5w_touch_valid=False,
        opening_checked=False, gap_up=None, first_candle_bullish=None,
        prev_close=None, open_price=None,
        entry_price=0.0, stop_loss=0.0, target_price=0.0, rr_ratio=0.0,
        rr_ok=False, all_met=False,
    )


def run_entry_checks(check_opening: bool = True) -> list[EntryCheckResult]:
    """保存済み候補に対してエントリーチェックを実行する。"""
    candidates = load_candidates()
    if not candidates:
        logger.info("エントリーチェック: 候補なし")
        return []

    results = []
    for idx, c in enumerate(candidates, 1):
        logger.info("エントリーチェック %d/%d: %s", idx, len(candidates), c["ticker"])
        try:
            result = check_entry(c, check_opening=check_opening)
            results.append(result)
        except Exception as e:
            logger.warning("エントリーチェックエラー %s: %s", c["ticker"], e)
        time.sleep(1.0)

    passed = sum(1 for r in results if r.all_met)
    logger.info("エントリー条件通過: %d / %d", passed, len(results))
    return results
