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
import time as _time

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── スクリーニングパラメータ ────────────────────────────
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


# ══════════════════════════════════════════════════════════
#  チャート診断（ぐりーん式）
# ══════════════════════════════════════════════════════════

# 5週線タッチ判定パラメータ
_5W_TOLERANCE = 0.015   # 5週線が20週線の ±1.5% 以内 = タッチ
_5W_LOOKBACK  = 26      # 直近26週を走査
_5W_GAP       = 3       # 同一タッチイベントとみなす最大バー間隔
_5W_CUTOFF    = -8      # 直近8週以内のタッチのみ有効

# 押し目・基本条件パラメータ
_PULLBACK_MIN     = 0.03     # 直近高値から -3% 以上
_PULLBACK_MAX     = 0.08     # 直近高値から -8% 以内
_PULLBACK_LOOK    = 20       # 直近N日の高値と比較
_MIN_VOLUME_CHART = 100_000  # 出来高10万株以上
_MAX_PRICE_CHART  = 5_000    # 株価5000円以下


def _merge_events(indices: list[int], gap: int) -> list[list[int]]:
    """連続するインデックスを同一イベントにまとめる。"""
    if not indices:
        return []
    events: list[list[int]] = [[indices[0]]]
    for idx in indices[1:]:
        if idx - events[-1][-1] <= gap:
            events[-1].append(idx)
        else:
            events.append([idx])
    return events


def _check_5w_touch(wma5: pd.Series, wma20: pd.Series) -> int:
    """
    週足5週線が20週線にタッチした回数（1〜2回のみ有効）を返す。
    有効でなければ 0 を返す。
    """
    n = len(wma5)
    touch_idx: list[int] = []
    for rel in range(-_5W_LOOKBACK, 0):
        i = n + rel
        if i < 0 or pd.isna(wma5.iloc[i]) or pd.isna(wma20.iloc[i]):
            continue
        diff = abs(float(wma5.iloc[i]) - float(wma20.iloc[i])) / float(wma20.iloc[i])
        if diff <= _5W_TOLERANCE:
            touch_idx.append(rel)
    events = _merge_events(touch_idx, _5W_GAP)
    if not (1 <= len(events) <= 2):
        return 0
    if events[-1][-1] < _5W_CUTOFF:
        return 0
    return len(events)


def check_chart_conditions(code: str) -> dict:
    """
    お宝候補銘柄のぐりーん式チャート診断（8条件）。

    Returns
    -------
    dict:
        weekly_po, weekly_ma20_up, weekly_5w_count, weekly_5w_ok,
        daily_ma60_up, daily_po, pullback_pct, pullback_ok,
        volume_ok, price_ok, score (0-8), verdict
    """
    result: dict = {
        "code":          code,
        "name":          code,
        # 週足
        "weekly_po":      False,
        "weekly_ma20_up": False,
        "weekly_5w_count": 0,
        "weekly_5w_ok":   False,
        # 日足
        "daily_ma60_up":  False,
        "daily_po":       False,
        "pullback_pct":   None,
        "pullback_ok":    False,
        # 基本条件
        "volume_ok":      False,
        "price_ok":       False,
        # 総合
        "score":   0,
        "verdict": "★ 見送り",
    }

    try:
        # ── 週足 (2年分 ≈ 104週) ────────────────────────
        weekly = yf.download(code, period="2y", interval="1wk",
                             auto_adjust=True, progress=False)
        if isinstance(weekly.columns, pd.MultiIndex):
            weekly.columns = weekly.columns.get_level_values(0)

        if not weekly.empty and len(weekly) >= 65:
            wc    = weekly["Close"].astype(float)
            wma5  = wc.rolling(5).mean()
            wma20 = wc.rolling(20).mean()
            wma60 = wc.rolling(60).mean()

            # 1. 週足PO: 5wMA > 20wMA > 60wMA
            v5, v20, v60 = float(wma5.iloc[-1]), float(wma20.iloc[-1]), float(wma60.iloc[-1])
            if not any(pd.isna(x) for x in (v5, v20, v60)):
                result["weekly_po"] = (v5 > v20 > v60)

            # 2. 20週線が右肩上がり（直近5週前比）
            if len(wma20.dropna()) >= 6:
                result["weekly_ma20_up"] = float(wma20.iloc[-1]) > float(wma20.iloc[-6])

            # 3. 5週線が20週線に1〜2回タッチ
            touch_count = _check_5w_touch(wma5, wma20)
            result["weekly_5w_count"] = touch_count
            result["weekly_5w_ok"]    = (1 <= touch_count <= 2)

        # ── 日足 (6ヶ月分 ≈ 130日) ──────────────────────
        daily = yf.download(code, period="6mo", interval="1d",
                            auto_adjust=True, progress=False)
        if isinstance(daily.columns, pd.MultiIndex):
            daily.columns = daily.columns.get_level_values(0)

        if not daily.empty and len(daily) >= 65:
            dc    = daily["Close"].astype(float)
            dv    = daily["Volume"].astype(float)
            dma5  = dc.rolling(5).mean()
            dma20 = dc.rolling(20).mean()
            dma60 = dc.rolling(60).mean()

            # 4. 60日線が上向き（直近5日前比）
            if len(dma60.dropna()) >= 6:
                result["daily_ma60_up"] = float(dma60.iloc[-1]) > float(dma60.iloc[-6])

            # 5. 日足PO: 5dMA > 20dMA > 60dMA
            d5, d20, d60 = float(dma5.iloc[-1]), float(dma20.iloc[-1]), float(dma60.iloc[-1])
            if not any(pd.isna(x) for x in (d5, d20, d60)):
                result["daily_po"] = (d5 > d20 > d60)

            # 6. 押し目形成中（直近20日高値から -3%〜-8%）
            recent_high = float(dc.iloc[-_PULLBACK_LOOK:].max())
            current     = float(dc.iloc[-1])
            if recent_high > 0:
                pullback = (recent_high - current) / recent_high
                result["pullback_pct"] = round(pullback * 100, 1)
                result["pullback_ok"]  = (_PULLBACK_MIN <= pullback <= _PULLBACK_MAX)

            # 7. 出来高10万株以上
            result["volume_ok"] = float(dv.iloc[-1]) >= _MIN_VOLUME_CHART

            # 8. 株価5000円以下
            result["price_ok"] = current <= _MAX_PRICE_CHART

        # ── スコア集計 ───────────────────────────────────
        score = sum([
            result["weekly_po"],
            result["weekly_ma20_up"],
            result["weekly_5w_ok"],
            result["daily_ma60_up"],
            result["daily_po"],
            result["pullback_ok"],
            result["volume_ok"],
            result["price_ok"],
        ])
        result["score"] = score
        result["verdict"] = (
            "★★★ 要注目"   if score >= 6 else
            "★★ 経過観察"  if score >= 4 else
            "★ 見送り"
        )

    except Exception as e:
        logger.debug("chart check error %s: %s", code, e)

    return result


def run_chart_checks(treasure_results: list[dict]) -> list[dict]:
    """
    お宝スクリーニング通過銘柄に対してチャート診断を実行し、
    スコアの高い順にソートして返す。
    """
    chart_results: list[dict] = []
    for r in treasure_results:
        logger.info("チャート診断: %s %s", r["code"], r.get("name", ""))
        diag = check_chart_conditions(r["code"])
        diag["name"] = r.get("name", r["code"])
        chart_results.append(diag)
        _time.sleep(1.0)

    chart_results.sort(key=lambda x: x["score"], reverse=True)
    return chart_results
