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


def _fmt_price(v) -> str:
    """価格を文字列化。Noneなら '-'。"""
    return f"{v:,.0f}円" if v is not None else "-"


def _rejection_reasons(r: EntryCheckResult) -> list[str]:
    """見送り理由を日本語リストで返す。"""
    reasons = []
    if not r.daily_ma60_up:
        reasons.append("60日線が下向き")
    if not r.daily_po:
        reasons.append("日足PO未成立")
    if not r.daily_touch_valid:
        reasons.append(f"日足タッチ{r.daily_touch_count}回（1〜2回外）")
    if not r.bars_ok:
        if r.bars_since_touch == 0:
            reasons.append("20日線タッチ未検出")
        else:
            reasons.append(f"タッチから{r.bars_since_touch}本目（4本超）")
    if not r.weekly_ma20_up:
        reasons.append("週足20週線が下向き")
    if not r.weekly_5w_touch_valid:
        reasons.append(f"週足5wタッチ{r.weekly_5w_touch_count}回（1〜2回外）")
    if not r.rr_ok:
        reasons.append(f"RR {r.rr_ratio:.1f}（2.0未満）")
    # 週足根拠崩れは最優先で表示（他の理由は不要）
    if r.weekly_expired:
        reasons.append("20週線を割った（監視終了）")
        return reasons
    if not r.opening_checked:
        reasons.append("寄り付きデータ取得失敗")
    else:
        if r.gap_up is not True:
            reasons.append("GD寄り" if r.gap_up is False else "フラット寄り")
        if r.first_candle_bullish is False:
            reasons.append("寄り付き陰線")
    return reasons


def _opening_line(r: EntryCheckResult) -> str:
    """寄り付き情報を表示用文字列で返す。"""
    if not r.opening_checked:
        return "寄り付き: 取得失敗"

    gap_str    = "↑GU" if r.gap_up is True else ("↓GD" if r.gap_up is False else "→フラット")
    candle_str = "陽線" if r.first_candle_bullish else "陰線"
    prev_str   = _fmt_price(r.prev_close)
    open_str   = _fmt_price(r.open_price)
    entry_ok   = (r.gap_up is True and r.first_candle_bullish is True)
    verdict    = "エントリー推奨" if entry_ok else "見送り"
    return (
        f"前日終値: {prev_str}  寄り付き: {open_str}\n"
        f"  （{gap_str} / {candle_str}）→ {verdict}"
    )


def _build_entry_message(results: list[EntryCheckResult]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")

    # watchlist が空（candidates.json もない）場合
    if not results:
        return (
            f"【エントリー判断】{today}\n\n"
            "監視候補なし\n"
            "（次回土曜スクリーニングまで待機中）"
        )

    passing = [r for r in results if r.all_met]

    lines = [f"【エントリー判断】{today}", ""]

    # ── エントリー候補（通過銘柄）──────────────
    if passing:
        lines.append("■ エントリー候補")
        lines.append("")
        for r in passing:
            code = r.ticker.replace(".T", "")
            lines.append(
                f"◎ {code} {r.name}\n"
                f"  エントリー: {r.entry_price:,.0f}円（20日線）\n"
                f"  損切り:    {r.stop_loss:,.0f}円"
                f"（-{(1 - r.stop_loss/r.entry_price)*100:.1f}%）\n"
                f"  目標:      {r.target_price:,.0f}円  RR: 1:{r.rr_ratio:.1f}\n"
                f"  {_opening_line(r)}\n"
                f"  条件: 日60↑{_ok(r.daily_ma60_up)} PO{_ok(r.daily_po)} "
                f"タッチ{r.daily_touch_count}回目/{r.bars_since_touch}本目  "
                f"週20↑{_ok(r.weekly_ma20_up)} 5w{r.weekly_5w_touch_count}回"
            )
            lines.append("")

    # ── 見送り銘柄 ────────────────────────────
    skipped = [r for r in results if not r.all_met]
    if skipped:
        lines.append("─ 見送り銘柄 ─")
        for r in skipped:
            code = r.ticker.replace(".T", "")
            reasons = _rejection_reasons(r)
            reason_str = "・".join(reasons) if reasons else "不明"
            lines.append(
                f"\n✕ {code} {r.name}\n"
                f"  {_opening_line(r)}\n"
                f"  5分足: {'陽線' if r.first_candle_bullish else '陰線' if r.first_candle_bullish is not None else '-'}\n"
                f"  見送り理由: {reason_str}"
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


def notify_morning_report(report_text: str) -> None:
    """朝の建玉レポートを LINE に送信する。"""
    print("\n" + "=" * 50)
    print(report_text)
    print("=" * 50 + "\n")
    _send(report_text)


def notify_watchlist_registered(entries: list[dict]) -> None:
    """土曜スクリーニング後：watchlist 自動登録結果を LINE に送信する。"""
    today = datetime.now().strftime("%Y-%m-%d")

    if not entries:
        message = f"【ウォッチリスト登録】{today}\n\n登録銘柄なし（エントリー条件通過ゼロ）"
    else:
        lines = [f"【ウォッチリスト登録】{today}", ""]
        for e in entries:
            lines.append(
                f"{e['code']} {e['name']}\n"
                f"  株価: {e['stock_price']:,}円  {e['touch_count']}回目タッチ\n"
                f"  推奨: {e['lot_suggest']}株  SL: {e['stop_loss']:,.0f}  TP: {e['target']:,.0f}"
            )
        lines.append("")
        lines.append(f"合計 {len(entries)} 銘柄をウォッチリストに登録")
        message = "\n".join(lines)

    print("\n" + "=" * 50)
    print(message)
    print("=" * 50 + "\n")
    _send(message)


def notify_entry_recommendation(entries: list[dict]) -> None:
    """月曜寄り付き確認後：エントリー推奨を LINE に送信する。"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"【本日エントリー推奨】{today}", ""]

    for e in entries:
        lines.append(
            f"本日エントリー推奨：{e['name']} {e['lots']}株 @{e['open_price']:,.0f}円\n"
            f"  損切り: {e['stop_loss']:,.0f}円 / 目標: {e['target']:,.0f}円"
        )
        lines.append("")

    lines.append("※ 発注は手動で行ってください")
    message = "\n".join(lines)

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


def notify_treasure_chart(chart_results: list[dict]) -> None:
    """お宝×チャート診断結果をLINEに送信。"""
    today = datetime.now().strftime("%Y-%m-%d")

    if not chart_results:
        _send(f"【お宝×チャート診断】{today}\n\n診断対象なし")
        return

    def ok(b: bool) -> str:
        return "✓" if b else "✗"

    lines = [f"【お宝×チャート診断】{today}", ""]

    for r in chart_results:
        code = r["code"].replace(".T", "")
        touch_str   = f"{r['weekly_5w_count']}回目/{ok(r['weekly_5w_ok'])}"
        pullback_str = (
            f"-{r['pullback_pct']}%/{ok(r['pullback_ok'])}"
            if r["pullback_pct"] is not None
            else f"-/{ok(r['pullback_ok'])}"
        )
        lines.append(
            f"► {code} {r['name']}\n"
            f"  週足PO：{ok(r['weekly_po'])}  20週線上向：{ok(r['weekly_ma20_up'])}\n"
            f"  5wタッチ：{touch_str}\n"
            f"  日足60↑：{ok(r['daily_ma60_up'])}  日足PO：{ok(r['daily_po'])}\n"
            f"  押し目：{pullback_str}\n"
            f"  出来高10万↑：{ok(r['volume_ok'])}  株価5000以下：{ok(r['price_ok'])}\n"
            f"  スコア：{r['score']}/8  {r['verdict']}"
        )
        lines.append("")

    message = "\n".join(lines)
    print("\n" + "=" * 50)
    print(message)
    print("=" * 50 + "\n")
    _send(message)


def notify_treasure(results: list[dict], is_friday: bool = False) -> None:
    """
    お宝銘柄スクリーニング結果をLINEに送信。

    Parameters
    ----------
    results   : treasure_screener.run_treasure_screening() の戻り値
    is_friday : 金曜日なら信用残確認の促しを追記
    """
    if not results:
        msg = "【お宝候補】\n該当銘柄なし"
        print("\n" + "=" * 50)
        print(msg)
        print("=" * 50 + "\n")
        _send(msg)
        return

    lines = [f"【お宝候補】{len(results)}銘柄\n"]

    for r in results:
        code = r["code"].replace(".T", "")
        lines.append(
            f"▶ {code} {r['name']}\n"
            f"  終値 {r['close']:,.0f}円\n"
            f"  出来高 {r['vol_ratio']}倍 / ボラ {r['avg_range_pct']}%\n"
        )

    if is_friday:
        lines.append(
            "\n📋 金曜チェック推奨:\n"
            "上記銘柄の信用売り残をkabuplusで確認\n"
            "→ https://kabuplus.com/stock/\n"
            "売り残急増 + 上記2条件 = 踏み上げ候補"
        )

    message = "\n".join(lines)
    print("\n" + "=" * 50)
    print(message)
    print("=" * 50 + "\n")
    _send(message)
