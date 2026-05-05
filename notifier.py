"""
LINE Messaging API 通知モジュール。
Push Message API を使い、スクリーニング結果をLINEに送信する。
"""
import logging
import os
from datetime import datetime

import requests

from screener import Candidate
from entry_judge import EntryCheckResult
from positions import ExitSignal

logger = logging.getLogger(__name__)

_LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
_MAX_MESSAGE_LEN = 4000  # 1リクエストあたりの最大文字数（API上限5000、余裕をもって4000）


def _split_message(text: str, max_len: int = _MAX_MESSAGE_LEN) -> list[str]:
    """テキストを max_len 文字以下のチャンクに分割する。"""
    chunks = []
    while len(text) > max_len:
        # 改行で区切れる最後の位置を探す
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def _build_message(candidates: list[Candidate]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")

    if not candidates:
        return f"【スクリーニング結果】{today}\n\n該当銘柄なし"

    lines = [f"【スクリーニング結果】{today}", ""]
    lines.append("■ パーフェクトオーダー + 20週線タッチ銘柄")
    lines.append("")

    for i, c in enumerate(candidates, 1):
        code = c.ticker.replace(".T", "")
        touch_label = f"{c.touch_count}回目"
        lines.append(
            f"{i}. {code} {c.name}\n"
            f"   株価: {c.price:,.0f}円  出来高: {c.volume:,.0f}\n"
            f"   タッチ: {touch_label}  日足押し目: -{c.pullback_pct * 100:.1f}%"
        )

    lines.append("")
    lines.append(f"合計 {len(candidates)} 銘柄")
    return "\n".join(lines)


def _ok(b: bool) -> str:
    return "✓" if b else "✗"


def _build_entry_message(results: list[EntryCheckResult]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    passing = [r for r in results if r.all_met]

    lines = [f"【エントリー判断】{today}", ""]

    if not passing:
        lines.append("本日の通過銘柄なし")
        if results:
            lines.append("")
            lines.append("─ 条件別チェック ─")
            for r in results:
                code = r.ticker.replace(".T", "")
                lines.append(
                    f"\n{code} {r.name}\n"
                    f"  日足60MA上向:{_ok(r.daily_ma60_up)} "
                    f"日足PO:{_ok(r.daily_po)} "
                    f"日足タッチ:{r.daily_touch_count}回/{_ok(r.daily_touch_valid)} "
                    f"本数:{r.bars_since_touch}本/{_ok(r.bars_ok)}\n"
                    f"  週足MA20上向:{_ok(r.weekly_ma20_up)} "
                    f"5週タッチ:{r.weekly_5w_touch_count}回/{_ok(r.weekly_5w_touch_valid)}\n"
                    f"  RR:{r.rr_ratio:.1f}/{_ok(r.rr_ok)}"
                )
    else:
        lines.append("■ エントリー候補")
        lines.append("")
        for r in passing:
            code = r.ticker.replace(".T", "")
            gap_str    = ("↑GU" if r.gap_up else "→") if r.gap_up is not None else "-"
            candle_str = ("陽線" if r.first_candle_bullish else "陰線") if r.first_candle_bullish is not None else "-"
            lines.append(
                f"{code} {r.name}\n"
                f"  エントリー: {r.entry_price:,.0f}円（20日線）\n"
                f"  損切り:    {r.stop_loss:,.0f}円（-{(1 - r.stop_loss/r.entry_price)*100:.1f}%）\n"
                f"  目標:      {r.target_price:,.0f}円  RR: 1:{r.rr_ratio:.1f}\n"
                f"  寄り付き:  {gap_str} / {candle_str}\n"
                f"  ─ 条件 ─\n"
                f"  日60↑:{_ok(r.daily_ma60_up)} 日PO:{_ok(r.daily_po)} "
                f"日タッチ:{r.daily_touch_count}回目 {r.bars_since_touch}本後\n"
                f"  週20↑:{_ok(r.weekly_ma20_up)} 週5w:{r.weekly_5w_touch_count}回目"
            )
            lines.append("")

    lines.append(f"通過: {len(passing)} / {len(results)} 銘柄")
    return "\n".join(lines)


def _build_exit_message(signals: list[ExitSignal]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"【利益確定アラート】{today}", ""]

    if not signals:
        lines.append("シグナルなし")
        return "\n".join(lines)

    lines.append("■ 売り検討銘柄")
    lines.append("")
    for s in signals:
        code = s.ticker.replace(".T", "")
        profit_str = f"+{s.profit_pct:.1f}%" if s.profit_pct >= 0 else f"{s.profit_pct:.1f}%"
        trigger_str = " / ".join(s.triggers)
        lines.append(
            f"{code} {s.name}\n"
            f"  現在値: {s.current_price:,.0f}円  損益: {profit_str}\n"
            f"  エントリー: {s.entry_price:,.0f}円\n"
            f"  5日線: {s.ma5:,.0f}  20日線: {s.ma20:,.0f}\n"
            f"  ▶ {trigger_str}"
        )
        lines.append("")

    lines.append(f"アラート {len(signals)} 銘柄")
    return "\n".join(lines)


def notify_exit(signals: list[ExitSignal]) -> None:
    """利益確定アラートをLINEに送信する。"""
    message = _build_exit_message(signals)
    print("\n" + "=" * 50)
    print(message)
    print("=" * 50 + "\n")
    _send(message)


def notify_entry(results: list[EntryCheckResult]) -> None:
    """エントリー判断結果をLINEに送信する。"""
    message = _build_entry_message(results)
    print("\n" + "=" * 50)
    print(message)
    print("=" * 50 + "\n")
    _send(message)


def notify(candidates: list[Candidate]) -> None:
    """スクリーニング結果をLINE Messaging API で送信する。"""
    token   = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")

    if not token or not user_id:
        logger.error(
            "LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が未設定です。"
            " .env ファイルを確認してください。"
        )
        return

    message = _build_message(candidates)
    # コンソールにも常に出力
    print("\n" + "=" * 50)
    print(message)
    print("=" * 50 + "\n")

    _send(message)


def _send(message: str) -> None:
    """LINE Messaging API にメッセージを送信する（内部共通関数）。"""
    token   = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")

    if not token or not user_id:
        logger.error("LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が未設定です。")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    for chunk in _split_message(message):
        payload = {
            "to": user_id,
            "messages": [{"type": "text", "text": chunk}],
        }
        try:
            resp = requests.post(_LINE_PUSH_URL, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("LINE送信成功（%d文字）", len(chunk))
        except requests.HTTPError:
            logger.error("LINE送信エラー (HTTP %s): %s", resp.status_code, resp.text)
        except Exception as e:
            logger.error("LINE送信例外: %s", e)
