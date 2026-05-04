"""
LINE Messaging API 通知モジュール。
Push Message API を使い、スクリーニング結果をLINEに送信する。
"""
import logging
import os
from datetime import datetime

import requests

from screener import Candidate

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
        except requests.HTTPError as e:
            logger.error("LINE送信エラー (HTTP %s): %s", resp.status_code, resp.text)
        except Exception as e:
            logger.error("LINE送信例外: %s", e)
