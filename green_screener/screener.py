"""
ぐりーん式スイングトレードのスクリーニング判定（green_screener 独立版）。

8条件でスコアリングする:
  週足 (3)  1. パーフェクトオーダー
            2. 20週線が上向き
            3. 20週線タッチ 1〜2回（直近8週以内）
  日足 (3)  4. パーフェクトオーダー
            5. 60日線が上向き
            6. 押し目 -3%〜-8%
  基本 (2)  7. 出来高が基準以上
            8. 株価が上限以下

スコア6以上 = ★★★ 要注目 / 4-5 = ★★ 経過観察 / 3以下 = ★ 見送り
"""
from __future__ import annotations

import gc
import time
from dataclasses import asdict, dataclass, field

import pandas as pd

from . import config
from .utils import (
    count_ma_touches,
    fetch_ohlc,
    is_perfect_order,
    is_rising,
    logger,
    moving_averages,
    pullback_pct,
)


@dataclass
class GreenCandidate:
    ticker: str
    name: str
    sector: str
    price: float
    volume: float
    # 週足
    weekly_po: bool
    weekly_ma20_up: bool
    weekly_touch_count: int
    weekly_touch_ok: bool
    # 日足
    daily_po: bool
    daily_ma60_up: bool
    daily_pullback_pct: float | None
    daily_pullback_ok: bool
    # 基本
    volume_ok: bool
    price_ok: bool
    # 総合
    score: int
    verdict: str

    def to_dict(self) -> dict:
        return asdict(self)


def _verdict(score: int) -> str:
    if score >= config.SCORE_STRONG:
        return "★★★ 要注目"
    if score >= config.SCORE_WATCH:
        return "★★ 経過観察"
    return "★ 見送り"


def _analyze_weekly(ticker: str) -> dict | None:
    """週足の3条件を判定する。データ不足なら None。"""
    df = fetch_ohlc(ticker, config.WEEKLY_PERIOD, "1wk")
    if df.empty or len(df) < config.MIN_WEEKLY_BARS:
        return None

    close = df["Close"].astype(float)
    low   = df["Low"].astype(float)
    ma5, ma20, ma60 = moving_averages(close)

    touch_count = count_ma_touches(
        low, close, ma5, ma20, ma60,
        tolerance=config.W_TOUCH_TOLERANCE,
        lookback=config.W_TOUCH_LOOKBACK,
        gap=config.W_TOUCH_GAP,
        cutoff=config.W_TOUCH_CUTOFF,
    )

    return {
        "weekly_po":          is_perfect_order(ma5, ma20, ma60),
        "weekly_ma20_up":     is_rising(ma20, config.W_MA20_SLOPE_BARS),
        "weekly_touch_count": touch_count,
        "weekly_touch_ok":    config.W_TOUCH_MIN <= touch_count <= config.W_TOUCH_MAX,
    }


def _analyze_daily(ticker: str) -> dict | None:
    """日足の3条件と基本2条件の材料を判定する。データ不足なら None。"""
    df = fetch_ohlc(ticker, config.DAILY_PERIOD, "1d")
    if df.empty or len(df) < config.MIN_DAILY_BARS:
        return None

    close  = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    ma5, ma20, ma60 = moving_averages(close)

    pb = pullback_pct(close, config.D_PULLBACK_LOOKBACK)
    pb_ok = (
        pb is not None
        and config.D_PULLBACK_MIN <= pb <= config.D_PULLBACK_MAX
    )

    price      = float(close.iloc[-1])
    latest_vol = float(volume.iloc[-1])

    return {
        "price":              price,
        "volume":             latest_vol,
        "daily_po":           is_perfect_order(ma5, ma20, ma60),
        "daily_ma60_up":      is_rising(ma60, config.D_MA60_SLOPE_BARS),
        "daily_pullback_pct": round(pb * 100, 2) if pb is not None else None,
        "daily_pullback_ok":  pb_ok,
        "volume_ok":          latest_vol >= config.MIN_VOLUME,
        "price_ok":           config.MIN_PRICE <= price <= config.MAX_PRICE,
    }


def analyze(ticker: str, name: str,
            sector: str = config.SECTOR_UNKNOWN) -> GreenCandidate | None:
    """1銘柄を8条件で評価する。データ不足なら None。"""
    weekly = _analyze_weekly(ticker)
    if weekly is None:
        return None

    daily = _analyze_daily(ticker)
    if daily is None:
        return None

    score = sum([
        weekly["weekly_po"],
        weekly["weekly_ma20_up"],
        weekly["weekly_touch_ok"],
        daily["daily_po"],
        daily["daily_ma60_up"],
        daily["daily_pullback_ok"],
        daily["volume_ok"],
        daily["price_ok"],
    ])

    return GreenCandidate(
        ticker=ticker,
        name=name,
        sector=sector,
        price=daily["price"],
        volume=daily["volume"],
        weekly_po=weekly["weekly_po"],
        weekly_ma20_up=weekly["weekly_ma20_up"],
        weekly_touch_count=weekly["weekly_touch_count"],
        weekly_touch_ok=weekly["weekly_touch_ok"],
        daily_po=daily["daily_po"],
        daily_ma60_up=daily["daily_ma60_up"],
        daily_pullback_pct=daily["daily_pullback_pct"],
        daily_pullback_ok=daily["daily_pullback_ok"],
        volume_ok=daily["volume_ok"],
        price_ok=daily["price_ok"],
        score=score,
        verdict=_verdict(score),
    )


def diversify_by_sector(candidates: list[GreenCandidate],
                        max_per_sector: int | None = None) -> list[GreenCandidate]:
    """
    同一業種の採用数に上限を設ける。

    入力はスコア降順に並んでいる前提で、上から順に採用し、
    その業種が上限に達していたら見送る（スコアの高い銘柄が優先される）。
    """
    limit = max_per_sector if max_per_sector is not None else config.MAX_PER_SECTOR

    kept: list[GreenCandidate] = []
    counts: dict[str, int] = {}
    dropped: dict[str, int] = {}

    for c in candidates:
        sector = c.sector or config.SECTOR_UNKNOWN
        if counts.get(sector, 0) < limit:
            kept.append(c)
            counts[sector] = counts.get(sector, 0) + 1
        else:
            dropped[sector] = dropped.get(sector, 0) + 1

    logger.info(
        "セクター分散フィルタ: %d → %d 銘柄（1業種あたり最大%d）",
        len(candidates), len(kept), limit,
    )
    if dropped:
        detail = "、".join(
            f"{s} -{n}" for s, n in sorted(dropped.items(), key=lambda x: -x[1])
        )
        logger.info("  上限超過で除外: %s", detail)

    return kept


def screen(tickers: list[str], names: dict[str, str],
           sectors: dict[str, str] | None = None) -> list[GreenCandidate]:
    """
    全銘柄をスクリーニングし、MIN_SCORE_TO_REPORT 以上の候補を
    スコア降順で返す（最大 MAX_RESULTS 件）。

    SECTOR_DIVERSIFY が True の場合、件数を絞る前にセクター分散フィルタを適用する。
    """
    total = len(tickers)
    sectors = sectors or {}
    candidates: list[GreenCandidate] = []

    for idx, ticker in enumerate(tickers, 1):
        if idx % config.BATCH_SIZE == 0:
            logger.info("進捗: %d / %d（候補 %d）", idx, total, len(candidates))
            time.sleep(config.BATCH_SLEEP)
            gc.collect()   # メモリ対策

        try:
            result = analyze(
                ticker,
                names.get(ticker, ticker),
                sectors.get(ticker, config.SECTOR_UNKNOWN),
            )
        except Exception as e:
            logger.debug("判定エラー %s: %s", ticker, e)
            continue

        if result is None:
            continue

        if result.score >= config.MIN_SCORE_TO_REPORT:
            candidates.append(result)
            logger.info(
                "候補: %s %s [%s]  スコア %d/%d  %s",
                ticker, result.name, result.sector,
                result.score, config.SCORE_MAX, result.verdict,
            )

        time.sleep(config.FETCH_SLEEP)

    candidates.sort(key=lambda c: (c.score, c.volume), reverse=True)
    logger.info("スクリーニング完了: %d 銘柄 → %d 候補", total, len(candidates))

    # 件数を絞る前に分散させる（絞ってから分散すると候補が枯れるため）
    if config.SECTOR_DIVERSIFY:
        candidates = diversify_by_sector(candidates)
    else:
        logger.info("セクター分散フィルタ: 無効（SECTOR_DIVERSIFY=False）")

    return candidates[: config.MAX_RESULTS]
