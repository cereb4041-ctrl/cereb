"""
テクニカル分析スクリーニングモジュール。
パーフェクトオーダー / 20週線タッチ / 日足押し目 の3条件を判定する。
"""
import logging
import time
from dataclasses import dataclass, field

import pandas as pd
import yfinance as yf

import config

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    ticker: str
    name: str
    price: float
    volume: float
    touch_count: int          # 1 or 2
    pullback_pct: float       # 日足の押し幅（%）
    weekly_ma5: float
    weekly_ma20: float
    weekly_ma60: float


def _fetch_with_retry(ticker: str, period: str, interval: str) -> pd.DataFrame:
    for attempt in range(config.MAX_RETRIES + 1):
        try:
            df = yf.download(
                ticker,
                period=period,
                interval=interval,
                auto_adjust=True,
                progress=False,
            )
            if not df.empty:
                return df
        except Exception as e:
            logger.debug("%s 取得エラー (attempt %d): %s", ticker, attempt + 1, e)
        if attempt < config.MAX_RETRIES:
            time.sleep(config.RETRY_SLEEP)
    return pd.DataFrame()


def _merge_touch_events(touch_indices: list[int], gap: int) -> list[list[int]]:
    """連続するタッチ週インデックスを同一イベントにまとめる。"""
    if not touch_indices:
        return []
    events = [[touch_indices[0]]]
    for idx in touch_indices[1:]:
        if idx - events[-1][-1] <= gap:
            events[-1].append(idx)
        else:
            events.append([idx])
    return events


def _check_weekly(ticker: str) -> tuple[bool, int, float, float, float] | None:
    """
    週足チェック。
    Returns (perfect_order, touch_count, ma5, ma20, ma60) または None（スキップ）。
    """
    df = _fetch_with_retry(ticker, period="2y", interval="1wk")
    if df.empty or len(df) < 65:
        return None

    # MultiIndex対応: yfinance 0.2.x は (Attribute, Ticker) の MultiIndex になる場合あり
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    close = df["Close"].astype(float)
    low   = df["Low"].astype(float)

    ma5  = close.rolling(config.MA_SHORT).mean()
    ma20 = close.rolling(config.MA_MID).mean()
    ma60 = close.rolling(config.MA_LONG).mean()

    # パーフェクトオーダー（最新週）
    if not (ma5.iloc[-1] > ma20.iloc[-1] > ma60.iloc[-1]):
        return None

    # 20週線タッチ検出（直近TOUCH_LOOKBACK週を走査）
    touch_indices: list[int] = []
    n = len(df)
    for rel in range(-config.TOUCH_LOOKBACK, 0):
        abs_i = n + rel
        if abs_i < 0 or pd.isna(ma5.iloc[abs_i]) or pd.isna(ma60.iloc[abs_i]):
            continue
        po_ok     = ma5.iloc[abs_i] > ma20.iloc[abs_i] > ma60.iloc[abs_i]
        touched   = low.iloc[abs_i] <= ma20.iloc[abs_i] * (1 + config.TOUCH_TOLERANCE)
        closed_up = close.iloc[abs_i] > ma20.iloc[abs_i]
        if po_ok and touched and closed_up:
            touch_indices.append(rel)

    events = _merge_touch_events(touch_indices, config.TOUCH_GAP_WEEKS)

    # タッチ回数が1〜2、かつ最後のタッチが直近8週以内
    if not (1 <= len(events) <= 2):
        return None
    if events[-1][-1] < config.RECENT_TOUCH_CUTOFF:
        return None

    return (
        True,
        len(events),
        float(ma5.iloc[-1]),
        float(ma20.iloc[-1]),
        float(ma60.iloc[-1]),
    )


def _check_daily(ticker: str) -> tuple[bool, float] | None:
    """
    日足チェック。
    Returns (passed, pullback_pct) または None（データ不足）。
    """
    df = _fetch_with_retry(ticker, period="3mo", interval="1d")
    if df.empty or len(df) < 25:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    close    = df["Close"].astype(float)
    daily_ma20 = close.rolling(20).mean()

    if pd.isna(daily_ma20.iloc[-1]):
        return None

    recent_high  = close.iloc[-config.PULLBACK_LOOKBACK:].max()
    latest_close = float(close.iloc[-1])
    pullback_pct = (float(recent_high) - latest_close) / float(recent_high)

    above_ma   = latest_close > float(daily_ma20.iloc[-1])
    pull_range = config.PULLBACK_MIN <= pullback_pct <= config.PULLBACK_MAX
    ma_up      = float(daily_ma20.iloc[-1]) > float(daily_ma20.iloc[-5])

    return (above_ma and pull_range and ma_up), pullback_pct


def screen(tickers: list[str], names: dict[str, str]) -> list[Candidate]:
    """
    全ティッカーをスクリーニングし、全条件を満たした銘柄リストを返す。
    tickers: フィルタ済みティッカーリスト
    names:   {ticker: 銘柄名} 辞書
    """
    candidates: list[Candidate] = []
    total = len(tickers)

    for idx, ticker in enumerate(tickers, 1):
        if idx % config.BATCH_SIZE == 0:
            logger.info("進捗: %d / %d", idx, total)
            time.sleep(config.BATCH_SLEEP)

        # 週足チェック
        weekly_result = _check_weekly(ticker)
        if weekly_result is None:
            continue
        _, touch_count, ma5, ma20, ma60 = weekly_result

        # 日足チェック
        daily_result = _check_daily(ticker)
        if daily_result is None:
            continue
        passed, pullback_pct = daily_result
        if not passed:
            continue

        # 最新株価・出来高（週足の最終値を流用）
        df_tmp = _fetch_with_retry(ticker, period="5d", interval="1d")
        if df_tmp.empty:
            continue
        if isinstance(df_tmp.columns, pd.MultiIndex):
            df_tmp.columns = df_tmp.columns.get_level_values(0)
        price  = float(df_tmp["Close"].dropna().iloc[-1])
        volume = float(df_tmp["Volume"].dropna().iloc[-1])

        candidates.append(
            Candidate(
                ticker=ticker,
                name=names.get(ticker, ticker),
                price=price,
                volume=volume,
                touch_count=touch_count,
                pullback_pct=pullback_pct,
                weekly_ma5=ma5,
                weekly_ma20=ma20,
                weekly_ma60=ma60,
            )
        )
        logger.info("候補追加: %s (%s)  タッチ: %d回目", ticker, names.get(ticker, ""), touch_count)

    logger.info("スクリーニング完了: %d 銘柄 → %d 候補", total, len(candidates))
    return candidates
