"""
green_screener 専用の通知モジュール（独立実装）。

既存の notifier.py には依存しない。
LINE の認証情報が未設定でもコンソール出力にフォールバックするため、
ローカル検証時はそのまま動く。
"""
from __future__ import annotations

import os
import sys

import requests

from . import config
from .utils import logger, today_str


def _ok(flag: bool) -> str:
    return "✓" if flag else "✗"


def safe_print(text: str) -> None:
    """
    コンソールへ出力する。
    Windows の cp932 コンソールは ▶ や ★ を扱えず UnicodeEncodeError になるため、
    その場合は表示できない文字を代替に置き換えて出力する（LINE送信側は元のまま）。
    """
    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def split_message(text: str, max_len: int = config.MAX_MESSAGE_LEN) -> list[str]:
    """LINE の文字数上限に収まるよう、改行位置でテキストを分割する。"""
    chunks: list[str] = []
    while len(text) > max_len:
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def build_message(candidates: list) -> str:
    """スクリーニング結果を LINE 用のテキストに整形する。"""
    header = f"【ぐりーん式スクリーニング】{today_str()}"

    if not candidates:
        return f"{header}\n\n該当銘柄なし"

    lines = [header, ""]

    for c in candidates:
        code = c.ticker.replace(".T", "")
        pb = (
            f"-{c.daily_pullback_pct}%" if c.daily_pullback_pct is not None else "-"
        )
        lines.append(
            f"▶ {code} {c.name}【{c.sector}】\n"
            f"  {c.price:,.0f}円 / 出来高 {c.volume:,.0f}\n"
            f"  週足PO：{_ok(c.weekly_po)}  20週線上向：{_ok(c.weekly_ma20_up)}\n"
            f"  20週線タッチ：{c.weekly_touch_count}回/{_ok(c.weekly_touch_ok)}\n"
            f"  日足PO：{_ok(c.daily_po)}  60日線上向：{_ok(c.daily_ma60_up)}\n"
            f"  押し目：{pb}/{_ok(c.daily_pullback_ok)}\n"
            f"  スコア：{c.score}/{config.SCORE_MAX}  {c.verdict}"
        )
        lines.append("")

    lines.append(f"合計 {len(candidates)} 銘柄")
    return "\n".join(lines)


def send_line(message: str) -> bool:
    """
    LINE Messaging API へ Push 送信する。
    認証情報が未設定なら送信せず False を返す（コンソール出力は呼び出し側で実施）。
    """
    token   = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")

    if not token or not user_id:
        logger.warning(
            "LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID が未設定のため送信をスキップします"
        )
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    ok = True
    for chunk in split_message(message):
        payload = {"to": user_id, "messages": [{"type": "text", "text": chunk}]}
        try:
            resp = requests.post(
                config.LINE_PUSH_URL, headers=headers, json=payload, timeout=10
            )
            resp.raise_for_status()
            logger.info("LINE送信成功（%d文字）", len(chunk))
        except requests.HTTPError:
            logger.error("LINE送信エラー (HTTP %s): %s", resp.status_code, resp.text)
            ok = False
        except Exception as e:
            logger.error("LINE送信例外: %s", e)
            ok = False
    return ok


def notify(candidates: list, to_line: bool = True) -> None:
    """
    結果をコンソールに表示し、必要なら LINE へ送信する。
    コンソール出力は常に行うので、LINE 未設定でも結果は確認できる。
    """
    message = build_message(candidates)

    safe_print("\n" + "=" * 50)
    safe_print(message)
    safe_print("=" * 50 + "\n")

    if to_line and config.NOTIFY_ENABLED:
        send_line(message)
