"""
ポジション管理と利益確定シグナル監視モジュール。

利益確定トリガー:
  1. デスクロス    : 5日線が20日線を下抜け
  2. 高値更新停止  : 当日高値 < 前日高値
  3. 上昇9本       : 終値が9日連続で前日比プラス
  4. 前回高値接近  : 現在値が登録前スイングハイの99%以上
"""
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

POSITIONS_FILE = Path("positions.json")

# 上昇カウント判定本数
RISING_COUNT_TRIGGER = 9
# 前回高値の接近判定（エントリー前60日の高値に何%以内で発火）
PREV_HIGH_APPROACH_PCT = 0.01   # 1%以内


@dataclass
class Position:
    ticker: str
    name: str
    entry_price: float
    registered_at: str  # ISO date string


@dataclass
class ExitSignal:
    ticker: str
    name: str
    entry_price: float
    current_price: float
    profit_pct: float
    triggers: list[str]   # 発火したトリガー名のリスト
    ma5: float
    ma20: float


# ─────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────

def load_positions() -> list[Position]:
    if not POSITIONS_FILE.exists():
        return []
    data = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    return [Position(**p) for p in data]


def _save_positions(positions: list[Position]) -> None:
    POSITIONS_FILE.write_text(
        json.dumps([asdict(p) for p in positions], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add_position(ticker: str, entry_price: float) -> tuple[bool, str]:
    """ポジションを追加する。Returns (success, message)"""
    code = ticker.strip().upper()
    if not code.endswith(".T"):
        code += ".T"

    positions = load_positions()
    for p in positions:
        if p.ticker == code:
            return False, f"{code.replace('.T', '')} は既に登録済みです"

    name = code.replace(".T", "")
    try:
        info = yf.Ticker(code).info
        name = info.get("longName") or info.get("shortName") or name
    except Exception:
        pass

    pos = Position(
        ticker=code,
        name=name,
        entry_price=entry_price,
        registered_at=date.today().isoformat(),
    )
    positions.append(pos)
    _save_positions(positions)
    logger.info("ポジション登録: %s %s @ %.0f円", code, name, entry_price)
    return True, f"✓ {code.replace('.T', '')} {name}\nエントリー: {entry_price:,.0f}円 を登録しました"


def remove_position(ticker: str) -> tuple[bool, str]:
    """ポジションを削除する。Returns (success, message)"""
    code = ticker.strip().upper()
    if not code.endswith(".T"):
        code += ".T"

    positions = load_positions()
    before = len(positions)
    positions = [p for p in positions if p.ticker != code]
    if len(positions) == before:
        return False, f"{code.replace('.T', '')} は登録されていません"

    _save_positions(positions)
    logger.info("ポジション解除: %s", code)
    return True, f"✓ {code.replace('.T', '')} を解除しました"


def list_positions_text() -> str:
    positions = load_positions()
    if not positions:
        return "登録中のポジションはありません"

    lines = ["■ 登録中ポジション", ""]
    for i, p in enumerate(positions, 1):
        code = p.ticker.replace(".T", "")
        lines.append(f"{i}. {code} {p.name}")
        lines.append(f"   エントリー: {p.entry_price:,.0f}円  登録日: {p.registered_at}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# データ取得
# ─────────────────────────────────────────────

def _fetch_daily(ticker: str, period: str = "3mo") -> pd.DataFrame:
    for attempt in range(3):
        try:
            df = yf.download(ticker, period=period, interval="1d",
                             auto_adjust=True, progress=False)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return df
        except Exception as e:
            logger.debug("%s fetch error (attempt %d): %s", ticker, attempt + 1, e)
            time.sleep(3)
    return pd.DataFrame()


# ─────────────────────────────────────────────
# 各トリガー判定
# ─────────────────────────────────────────────

def _check_deathcross(close: pd.Series) -> bool:
    """5MA が 20MA を下抜けた（前日は上、当日は下）。"""
    ma5  = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    if pd.isna(ma5.iloc[-2]) or pd.isna(ma20.iloc[-2]):
        return False
    return (float(ma5.iloc[-2]) >= float(ma20.iloc[-2])
            and float(ma5.iloc[-1]) < float(ma20.iloc[-1]))


def _check_high_stopped(high: pd.Series) -> bool:
    """当日高値 < 前日高値（高値更新が止まった）。"""
    return float(high.iloc[-1]) < float(high.iloc[-2])


def _check_rising_count(close: pd.Series, count: int = RISING_COUNT_TRIGGER) -> bool:
    """終値が count 日連続で前日比プラス。"""
    if len(close) < count + 1:
        return False
    for i in range(-count, 0):
        if float(close.iloc[i]) <= float(close.iloc[i - 1]):
            return False
    return True


def _check_prev_high_approach(close: pd.Series, registered_at: str) -> tuple[bool, Optional[float]]:
    """
    登録日前60日のスイングハイに現在値が1%以内まで接近した。
    Returns (triggered, prev_high)
    """
    try:
        reg_date = date.fromisoformat(registered_at)
    except ValueError:
        return False, None

    # 登録日以前のデータを抽出
    df_index = close.index
    if hasattr(df_index[0], "date"):
        pre_entry = close[[d.date() < reg_date for d in df_index]]
    else:
        pre_entry = close[close.index < pd.Timestamp(reg_date)]

    if len(pre_entry) < 5:
        return False, None

    lookback = pre_entry.iloc[-60:]
    prev_high = float(lookback.max())
    current   = float(close.iloc[-1])

    triggered = current >= prev_high * (1 - PREV_HIGH_APPROACH_PCT)
    return triggered, prev_high


# ─────────────────────────────────────────────
# メイン検出
# ─────────────────────────────────────────────

def check_exit_signals() -> list[ExitSignal]:
    """全ポジションの利益確定シグナルを検出する。"""
    positions = load_positions()
    if not positions:
        return []

    signals = []
    for pos in positions:
        try:
            df = _fetch_daily(pos.ticker, period="6mo")
            if df.empty or len(df) < 22:
                logger.warning("データ不足: %s", pos.ticker)
                continue

            close = df["Close"].astype(float)
            high  = df["High"].astype(float)
            ma5   = close.rolling(5).mean()
            ma20  = close.rolling(20).mean()

            triggered: list[str] = []

            if _check_deathcross(close):
                triggered.append("5日線が20日線を下抜け")

            if _check_high_stopped(high):
                triggered.append("高値更新が止まった")

            if _check_rising_count(close):
                triggered.append(f"上昇{RISING_COUNT_TRIGGER}本目")

            prev_high_hit, prev_high = _check_prev_high_approach(close, pos.registered_at)
            if prev_high_hit and prev_high:
                triggered.append(f"前回高値 {prev_high:,.0f}円 に接近")

            if triggered:
                current_price = float(close.iloc[-1])
                profit_pct = (current_price - pos.entry_price) / pos.entry_price * 100
                signals.append(ExitSignal(
                    ticker=pos.ticker,
                    name=pos.name,
                    entry_price=pos.entry_price,
                    current_price=current_price,
                    profit_pct=profit_pct,
                    triggers=triggered,
                    ma5=float(ma5.iloc[-1]) if not pd.isna(ma5.iloc[-1]) else 0.0,
                    ma20=float(ma20.iloc[-1]) if not pd.isna(ma20.iloc[-1]) else 0.0,
                ))
                logger.info("シグナル検出: %s  トリガー: %s  損益: %+.1f%%",
                            pos.ticker, triggered, profit_pct)
        except Exception as e:
            logger.warning("チェックエラー %s: %s", pos.ticker, e)
        time.sleep(1.0)

    return signals
