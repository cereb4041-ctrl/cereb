"""
ポジション管理と利益確定シグナル監視モジュール。
"""
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

POSITIONS_FILE = Path("positions.json")


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

    # 銘柄名を取得（失敗してもコードで代替）
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
    """登録中ポジション一覧を文字列で返す。"""
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
# シグナル検出
# ─────────────────────────────────────────────

def _fetch_daily(ticker: str) -> pd.DataFrame:
    for attempt in range(3):
        try:
            df = yf.download(ticker, period="3mo", interval="1d",
                             auto_adjust=True, progress=False)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return df
        except Exception as e:
            logger.debug("%s fetch error (attempt %d): %s", ticker, attempt + 1, e)
            time.sleep(3)
    return pd.DataFrame()


def check_exit_signals() -> list[ExitSignal]:
    """全ポジションのデスクロス（5MA が 20MA を下抜け）を検出する。"""
    positions = load_positions()
    if not positions:
        return []

    signals = []
    for pos in positions:
        try:
            df = _fetch_daily(pos.ticker)
            if df.empty or len(df) < 22:
                logger.warning("データ不足: %s", pos.ticker)
                continue

            close = df["Close"].astype(float)
            ma5  = close.rolling(5).mean()
            ma20 = close.rolling(20).mean()

            # デスクロス: 前日は MA5 >= MA20、当日は MA5 < MA20
            prev_above = float(ma5.iloc[-2]) >= float(ma20.iloc[-2])
            curr_below = float(ma5.iloc[-1]) < float(ma20.iloc[-1])

            if prev_above and curr_below:
                current_price = float(close.iloc[-1])
                profit_pct = (current_price - pos.entry_price) / pos.entry_price * 100
                signals.append(ExitSignal(
                    ticker=pos.ticker,
                    name=pos.name,
                    entry_price=pos.entry_price,
                    current_price=current_price,
                    profit_pct=profit_pct,
                    ma5=float(ma5.iloc[-1]),
                    ma20=float(ma20.iloc[-1]),
                ))
                logger.info("デスクロス検出: %s  損益: %+.1f%%", pos.ticker, profit_pct)
        except Exception as e:
            logger.warning("チェックエラー %s: %s", pos.ticker, e)
        time.sleep(1.0)

    return signals
