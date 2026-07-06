"""
ペーパートレード（仮想売買）モジュール。

実際には発注せず、エントリー判断通過 → 仮想建玉、
毎日15:45の監視で損切り・利確・デッドクロスを判定して仮想決済する。
自動売買ロジックの検証用の雛形。

発注抽象化:
  _execute_order() が唯一の「発注」箇所。
  実発注に切り替える場合はここに証券会社APIを差し込む。

注意:
  Railway のファイルシステムは再デプロイで消えるため、
  paper_trades.json も消える（検証記録はLINE通知に残る）。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytz
import yfinance as yf

import config

logger = logging.getLogger(__name__)

_JST = pytz.timezone("Asia/Tokyo")

PAPER_FILE = Path("paper_trades.json")

# ── 安全装置（実発注に切り替えた際もここが効く） ──────────
MAX_OPEN_POSITIONS = config.MAX_POSITIONS      # 最大同時保有（4）
BUDGET_PER_TRADE   = config.CAPITAL // config.MAX_POSITIONS  # 1銘柄あたり予算
MAX_ENTRIES_PER_DAY = 2                        # 1日の新規建玉上限
UNIT = 100                                     # 単元株数

DRY_RUN = True   # True=ペーパートレード / False=実発注（未実装）


# ─────────────────────────────────────────────
# 永続化
# ─────────────────────────────────────────────

def _load() -> dict:
    if not PAPER_FILE.exists():
        return {"trades": [], "stats": {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}}
    return json.loads(PAPER_FILE.read_text(encoding="utf-8"))


def _save(data: dict) -> None:
    PAPER_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _today() -> str:
    return datetime.now(_JST).date().isoformat()


def _open_trades(data: dict) -> list[dict]:
    return [t for t in data["trades"] if t["status"] == "open"]


# ─────────────────────────────────────────────
# 発注抽象化層（実発注はここを差し替える）
# ─────────────────────────────────────────────

def _execute_order(side: str, ticker: str, qty: int, price: float) -> bool:
    """
    発注の唯一の入口。DRY_RUN=True の間は記録のみで常に成功を返す。
    実発注対応時は、ここに kabuステーションAPI 等の呼び出しを実装する。
    """
    if DRY_RUN:
        logger.info("[PAPER] %s %s %d株 @ %.1f円", side, ticker, qty, price)
        return True
    raise NotImplementedError("実発注は未実装です（DRY_RUN=False は使用不可）")


# ─────────────────────────────────────────────
# 建玉オープン（9:15 エントリー判断通過時に呼ぶ）
# ─────────────────────────────────────────────

def open_paper_positions(passed_results: list) -> list[dict]:
    """
    エントリー条件通過銘柄（EntryCheckResult のリスト）を仮想建玉にする。
    Returns: 新規オープンした建玉のリスト
    """
    data = _load()
    opened: list[dict] = []
    today = _today()

    open_codes = {t["ticker"] for t in _open_trades(data)}
    today_entries = sum(
        1 for t in data["trades"] if t.get("entry_date") == today
    )

    for r in passed_results:
        # ── 安全装置 ──────────────────────────
        if len(_open_trades(data)) + len(opened) >= MAX_OPEN_POSITIONS:
            logger.info("[PAPER] 最大保有数(%d)到達のためスキップ: %s", MAX_OPEN_POSITIONS, r.ticker)
            continue
        if today_entries + len(opened) >= MAX_ENTRIES_PER_DAY:
            logger.info("[PAPER] 本日の新規上限(%d)到達のためスキップ: %s", MAX_ENTRIES_PER_DAY, r.ticker)
            continue
        if r.ticker in open_codes:
            logger.info("[PAPER] 既に保有中のためスキップ: %s", r.ticker)
            continue

        # エントリー価格: 寄り付き値があればそれ、なければ20日線価格
        entry_price = r.open_price if r.open_price else r.entry_price
        if not entry_price or entry_price <= 0:
            continue

        # 数量: 予算内で買える最大単元（最低1単元）
        qty = int(BUDGET_PER_TRADE // (entry_price * UNIT)) * UNIT
        if qty == 0:
            qty = UNIT  # ペーパーなので予算超過でも1単元で記録して検証を続ける

        if not _execute_order("BUY", r.ticker, qty, entry_price):
            continue

        trade = {
            "ticker": r.ticker,
            "name": r.name,
            "entry_date": today,
            "entry_price": round(float(entry_price), 1),
            "qty": qty,
            "stop_loss": r.stop_loss,
            "target": r.target_price,
            "status": "open",
            "exit_date": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl": None,
        }
        data["trades"].append(trade)
        opened.append(trade)
        logger.info("[PAPER] 建玉オープン: %s %s %d株 @ %.1f", r.ticker, r.name, qty, entry_price)

    if opened:
        _save(data)
    return opened


# ─────────────────────────────────────────────
# 決済判定（15:45 に呼ぶ）
# ─────────────────────────────────────────────

def _check_exit(trade: dict) -> tuple[str, float] | None:
    """
    1建玉の決済判定。
    Returns (exit_reason, exit_price) または None（保有継続）。
    判定順序は保守的に 損切り → 利確 → デッドクロス。
    """
    ticker = trade["ticker"]
    df = yf.download(ticker, period="3mo", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or df.empty or len(df) < 21:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    low   = float(df["Low"].iloc[-1])
    high  = float(df["High"].iloc[-1])
    close = df["Close"].astype(float)

    # 1. 損切り: 当日安値がストップに到達
    if low <= trade["stop_loss"]:
        return "損切り", trade["stop_loss"]

    # 2. 利確: 当日高値が目標に到達
    if trade["target"] and high >= trade["target"]:
        return "利確目標到達", trade["target"]

    # 3. デッドクロス: 5日線が20日線を下抜け（当日クロス）
    ma5  = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    if (not pd.isna(ma5.iloc[-2]) and not pd.isna(ma20.iloc[-2])
            and float(ma5.iloc[-2]) >= float(ma20.iloc[-2])
            and float(ma5.iloc[-1]) < float(ma20.iloc[-1])):
        return "デッドクロス", float(close.iloc[-1])

    return None


def check_paper_exits() -> list[dict]:
    """
    全オープン建玉の決済判定を行い、決済したものを返す。
    """
    data = _load()
    closed: list[dict] = []

    for trade in _open_trades(data):
        try:
            result = _check_exit(trade)
        except Exception as e:
            logger.warning("[PAPER] 決済判定エラー %s: %s", trade["ticker"], e)
            continue

        if result is None:
            continue

        reason, exit_price = result
        if not _execute_order("SELL", trade["ticker"], trade["qty"], exit_price):
            continue

        pnl = round((exit_price - trade["entry_price"]) * trade["qty"], 0)
        trade.update({
            "status": "closed",
            "exit_date": _today(),
            "exit_price": round(exit_price, 1),
            "exit_reason": reason,
            "pnl": pnl,
        })
        data["stats"]["total"] += 1
        data["stats"]["pnl"] = round(data["stats"]["pnl"] + pnl, 0)
        if pnl >= 0:
            data["stats"]["wins"] += 1
        else:
            data["stats"]["losses"] += 1
        closed.append(trade)
        logger.info("[PAPER] 決済: %s %s → %+,.0f円", trade["ticker"], reason, pnl)
        time.sleep(1.0)

    if closed:
        _save(data)
    return closed


# ─────────────────────────────────────────────
# レポート生成
# ─────────────────────────────────────────────

def build_paper_report(opened: list[dict] | None = None,
                       closed: list[dict] | None = None) -> str | None:
    """
    ペーパートレードの状況レポートを作る。
    建玉・決済・保有のいずれもなければ None（通知不要）。
    """
    data = _load()
    open_trades = _open_trades(data)

    if not opened and not closed and not open_trades:
        return None

    lines = [f"【ペーパートレード】{_today()}", ""]

    if opened:
        lines.append("■ 新規建玉（仮想）")
        for t in opened:
            lines.append(
                f"◎ {t['ticker'].replace('.T', '')} {t['name']}\n"
                f"  {t['qty']}株 @ {t['entry_price']:,.0f}円\n"
                f"  損切り {t['stop_loss']:,.0f} / 目標 {t['target']:,.0f}"
            )
        lines.append("")

    if closed:
        lines.append("■ 決済（仮想）")
        for t in closed:
            sign = "+" if t["pnl"] >= 0 else ""
            lines.append(
                f"✕ {t['ticker'].replace('.T', '')} {t['name']}\n"
                f"  {t['exit_reason']} @ {t['exit_price']:,.0f}円\n"
                f"  損益: {sign}{t['pnl']:,.0f}円"
            )
        lines.append("")

    if open_trades:
        lines.append(f"■ 保有中: {len(open_trades)} 銘柄")
        for t in open_trades:
            lines.append(
                f"  {t['ticker'].replace('.T', '')} {t['name']} "
                f"{t['qty']}株 @ {t['entry_price']:,.0f}円（{t['entry_date']}〜）"
            )
        lines.append("")

    s = data["stats"]
    if s["total"] > 0:
        win_rate = s["wins"] / s["total"] * 100
        sign = "+" if s["pnl"] >= 0 else ""
        lines.append(
            f"─ 累計成績 ─\n"
            f"{s['total']}戦 {s['wins']}勝{s['losses']}敗（勝率 {win_rate:.0f}%）\n"
            f"累計損益: {sign}{s['pnl']:,.0f}円"
        )

    lines.append("\n※ 仮想売買です。実際の発注は行われていません")
    return "\n".join(lines)
