"""
LINE Webhookサーバー（Flask）。
ポジション登録・解除コマンドおよびリモートコントロールコマンドを受信する。

コマンド仕様:
  登録 7203 2850   → ポジション登録
  解除 7203        → ポジション解除
  一覧             → 登録中ポジション確認
  スクリーニング   → 週次スクリーニング即時実行
  エントリー       → エントリー判断即時実行
  監視             → ポジション監視即時実行
  お宝             → お宝スクリーニング即時実行
  ウォッチリスト   → 監視中銘柄一覧
  WL追加 7203      → ウォッチリストに手動追加
  WL削除 7203      → ウォッチリストから削除
  ヘルプ           → コマンド一覧
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import threading
from datetime import date

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
    "──── リモートコントロール ────\n"
    "スクリーニング        →  週次スクリーニング即時実行\n"
    "エントリー           →  エントリー判断即時実行\n"
    "監視                 →  ポジション監視即時実行\n"
    "お宝                 →  お宝スクリーニング即時実行\n"
    "ウォッチリスト        →  監視中銘柄一覧\n"
    "WL追加 銘柄コード     →  ウォッチリストに手動追加\n"
    "WL削除 銘柄コード     →  ウォッチリストから削除\n"
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


def _watchlist_text() -> str:
    """ウォッチリストの監視中銘柄を表示用文字列で返す。"""
    from watchlist_manager import load_watchlist
    data = load_watchlist()
    entries = data.get("watchlist", [])
    active = [e for e in entries if e.get("status") in ("watching", "skipped", "passed")]
    if not active:
        return "監視中銘柄なし"
    lines = [f"【ウォッチリスト】{len(active)}銘柄", ""]
    status_label = {"watching": "監視中", "skipped": "本日見送", "passed": "通過済"}
    for e in active:
        code = e["code"]
        name = e.get("name", "")
        status = status_label.get(e.get("status", ""), e.get("status", ""))
        screened = e.get("screened_date", "")
        lines.append(f"{code} {name}  [{status}]  登録: {screened}")
    return "\n".join(lines)


def _wl_add(code: str) -> str:
    """ウォッチリストに手動追加する。"""
    from watchlist_manager import add_entry, is_in_watchlist
    code = code.strip().upper()
    if is_in_watchlist(code):
        return f"{code} はすでにウォッチリストに存在します"
    entry = {
        "code": code,
        "ticker": f"{code}.T",
        "name": code,
        "screened_date": date.today().isoformat(),
        "status": "watching",
        "fetch_fail_count": 0,
        "price": 0.0,
        "touch_count": 1,
        "weekly_ma20": 0.0,
        "pullback_pct": 0.0,
        "lot_suggest": 0,
        "stop_loss": 0.0,
        "target": 0.0,
        "ma20w": 0.0,
    }
    add_entry(entry)
    return f"{code} をウォッチリストに追加しました"


def _wl_remove(code: str) -> str:
    """ウォッチリストから削除する。"""
    from watchlist_manager import remove_entry, is_in_watchlist
    code = code.strip().upper()
    if not is_in_watchlist(code):
        return f"{code} はウォッチリストに存在しません"
    remove_entry(code)
    return f"{code} をウォッチリストから削除しました"


def _run_in_background(fn, label: str, reply_token: str) -> None:
    """即時返信してからバックグラウンドで fn を実行する。"""
    _reply(reply_token, f"{label}を開始します...\n結果は別途通知します")
    threading.Thread(target=fn, daemon=True).start()


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

    elif cmd == "スクリーニング":
        from main import run_screening
        _run_in_background(run_screening, "スクリーニング", reply_token)

    elif cmd == "エントリー":
        from main import run_entry
        _run_in_background(run_entry, "エントリー判断", reply_token)

    elif cmd == "監視":
        from main import run_monitor
        _run_in_background(run_monitor, "ポジション監視", reply_token)

    elif cmd == "お宝":
        from main import run_treasure
        _run_in_background(run_treasure, "お宝スクリーニング", reply_token)

    elif cmd == "ウォッチリスト":
        _reply(reply_token, _watchlist_text())

    elif cmd == "WL追加" and len(parts) == 2:
        _reply(reply_token, _wl_add(parts[1]))

    elif cmd == "WL削除" and len(parts) == 2:
        _reply(reply_token, _wl_remove(parts[1]))

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
