"""
treasure_screener.py
お宝銘柄スクリーニング
条件:
  1. 株価横ばい  — 5日間の値動き(高値-安値)/終値 平均 < FLAT_THRESH
  2. 出来高急増  — 当日出来高 > 20日平均出来高 × VOL_MULT
金曜のみ追加:
  3. 候補銘柄リストを通知に含める（信用残は手動確認用）
"""

from __future__ import annotations

import logging

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── パラメータ ──────────────────────────────────────────
FLAT_THRESH = 0.03   # 5日平均ボラ 3%以下 = 横ばい
VOL_MULT    = 3.0    # 出来高が20日平均の3倍以上
MIN_PRICE   = 100    # 最低株価（円）
MAX_PRICE   = 1500   # 最高株価（円）　低位株に絞る
MAX_RESULTS = 20     # 通知する最大銘柄数
LOOKBACK    = 25     # yfinance取得日数（20日MA + バッファ）


def _fetch(ticker_symbol: str) -> pd.DataFrame | None:
    """yfinanceで日足データを取得。失敗したらNoneを返す。"""
    try:
        df = yf.download(
            ticker_symbol,
            period=f"{LOOKBACK}d",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        if df is None or df.empty:
            return None
        # yfinance 新バージョンは MultiIndex カラムを返す場合がある
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 21:
            return None
        return df
    except Exception as e:
        logger.debug("fetch error %s: %s", ticker_symbol, e)
        return None


def _is_treasure(df: pd.DataFrame) -> bool:
    """条件1 + 条件2 を判定。"""
    close  = df["Close"].astype(float)
    high   = df["High"].astype(float)
    low    = df["Low"].astype(float)
    volume = df["Volume"].astype(float)

    latest_close = float(close.iloc[-1])

    # 株価フィルタ
    if not (MIN_PRICE <= latest_close <= MAX_PRICE):
        return False

    # 条件1: 5日間の平均ボラティリティ
    avg_range5 = float(((high - low) / close).iloc[-5:].mean())
    if avg_range5 >= FLAT_THRESH:
        return False

    # 条件2: 当日出来高 vs 20日平均出来高
    vol_today = float(volume.iloc[-1])
    vol_ma20  = float(volume.iloc[-21:-1].mean())
    if vol_ma20 <= 0 or vol_today < vol_ma20 * VOL_MULT:
        return False

    return True


def run_treasure_screening(
    symbols: list[str],
    names: dict[str, str] | None = None,
    is_friday: bool = False,
) -> list[dict]:
    """
    スクリーニングを実行して結果リストを返す。

    Parameters
    ----------
    symbols   : 東証銘柄コードリスト（例: ["7203.T", "9984.T", ...]）
    names     : {ticker: name} の辞書（省略時は yf.Ticker.info から取得）
    is_friday : 金曜日のみ True（信用残確認促進メッセージを付加）

    Returns
    -------
    list of dict: [{"code": "7203.T", "name": "...", "close": 1234,
                    "vol_ratio": 4.2, "avg_range_pct": 1.8}, ...]
    """
    results = []

    for symbol in symbols:
        df = _fetch(symbol)
        if df is None:
            continue
        if not _is_treasure(df):
            continue

        close  = df["Close"].astype(float)
        high   = df["High"].astype(float)
        low    = df["Low"].astype(float)
        volume = df["Volume"].astype(float)

        latest_close = float(close.iloc[-1])
        vol_today    = float(volume.iloc[-1])
        vol_ma20     = float(volume.iloc[-21:-1].mean())
        vol_ratio    = vol_today / vol_ma20 if vol_ma20 > 0 else 0
        avg_range5   = float(((high - low) / close).iloc[-5:].mean()) * 100

        # 銘柄名: 渡された辞書 → yf.Ticker.info の順で取得
        if names and symbol in names:
            name = names[symbol]
        else:
            try:
                info = yf.Ticker(symbol).info
                name = info.get("longName") or info.get("shortName") or symbol
            except Exception:
                name = symbol

        results.append({
            "code":          symbol,
            "name":          name,
            "close":         latest_close,
            "vol_ratio":     round(vol_ratio, 1),
            "avg_range_pct": round(avg_range5, 2),
        })

        if len(results) >= MAX_RESULTS:
            break

    logger.info("treasure screening: %d hits / %d symbols", len(results), len(symbols))
    return results
