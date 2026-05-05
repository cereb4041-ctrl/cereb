"""
LINE Webhookサーバー（Flask）。
ポジション登録・解除コマンドを受信してpositions.jsonを更新する。

コマンド仕様:
  登録 7203 2850   → ポジション登録
  解除 7203        → ポジション解除
  一覧             → 登録中ポジション確認
  ヘルプ           → コマンド一覧
"""
import base64
import hashlib
import hmac
import json
import logging
import os

import requests
from flask import Flask, abort, request

from positions import add_position, list_positions_text, remove_position

logger = logging.getLogger(__name__)

app = Flask(__name__)

_LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

_HELP_TEXT = (
    "コマンド一覧:\n"
    "登録 銘柄コード 価格  →  ポジション登録\n"
    "解除 銘柄コード       →  ポジション解除\n"
    "一覧                 →  登録ポジション確認\n"
    "ヘルプ               →  このメッセージ\n\n"
    "例: 登録 7203 2850"
)


def _verify_signature(body: bytes, signature: str) -> bool:
    secret = os.getenv("LINE_CHANNEL_SECRET", "")
    if not secret:
        logger.warning("LINE_CHANNEL_SECRET 未設定: 署名検証をスキップします")
        return True
    h = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(h).decode("utf-8") == signature


def _reply(reply_token: str, text: str) -> None:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token or not reply_token:
        return
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    try:
        resp = requests.post(
            _LINE_REPLY_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Webhook reply error: %s", e)


def _handle_text(text: str, reply_token: str) -> None:
    parts = text.strip().split()
    if not parts:
        return

    cmd = parts[0]

    if cmd == "登録" and len(parts) == 3:
        try:
            price = float(parts[2].replace(",", ""))
            _, msg = add_position(parts[1], price)
        except ValueError:
            msg = "フォーマット: 登録 銘柄コード エントリー価格\n例: 登録 7203 2850"
        _reply(reply_token, msg)

    elif cmd == "解除" and len(parts) == 2:
        _, msg = remove_position(parts[1])
        _reply(reply_token, msg)

    elif cmd == "一覧":
        _reply(reply_token, list_positions_text())

    elif cmd == "ヘルプ":
        _reply(reply_token, _HELP_TEXT)


@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_data()
    sig  = request.headers.get("X-Line-Signature", "")

    if not _verify_signature(body, sig):
        logger.warning("LINE署名検証失敗")
        abort(400)

    try:
        events = json.loads(body).get("events", [])
    except Exception:
        abort(400)

    for event in events:
        if event.get("type") != "message":
            continue
        msg = event.get("message", {})
        if msg.get("type") != "text":
            continue
        _handle_text(msg.get("text", ""), event.get("replyToken", ""))

    return "OK", 200


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200
