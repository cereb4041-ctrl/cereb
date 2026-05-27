"""
ウォッチリスト管理モジュール。
watchlist.json の CRUD 操作と portfolio.json への書き込みを担当する。

watchlist エントリーの status:
  watching : 監視中（毎日 9:10 にエントリー判断を実施）
  passed   : 本日通過（エントリー推奨 → 翌日以降は判断対象外）
  skipped  : 本日見送り（翌朝 watching に戻して再判断）
  expired  : 監視終了（スクリーニングから14日超過 or 20週線割れ）
"""
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

WATCHLIST_FILE = Path("watchlist.json")
PORTFOLIO_FILE = Path("portfolio.json")

WATCHLIST_EXPIRY_DAYS = 14   # スクリーニングから何日で監視終了するか


# ─────────────────────────────────────────────
# watchlist.json CRUD
# ─────────────────────────────────────────────

def load_watchlist() -> dict:
    if not WATCHLIST_FILE.exists():
        return {"watchlist": []}
    return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))


def save_watchlist(data: dict) -> None:
    WATCHLIST_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_in_watchlist(code: str) -> bool:
    data = load_watchlist()
    return any(e["code"] == code for e in data["watchlist"])


def add_entry(entry: dict) -> None:
    """エントリーを追加する。同一 code が存在する場合は上書きする。"""
    data = load_watchlist()
    code = entry["code"]
    data["watchlist"] = [e for e in data["watchlist"] if e["code"] != code]
    data["watchlist"].append(entry)
    save_watchlist(data)
    logger.info("ウォッチリスト登録: %s %s", code, entry.get("name", ""))


def remove_entry(code: str) -> None:
    """指定 code のエントリーを削除する。"""
    data = load_watchlist()
    before = len(data["watchlist"])
    data["watchlist"] = [e for e in data["watchlist"] if e["code"] != code]
    save_watchlist(data)
    if len(data["watchlist"]) < before:
        logger.info("ウォッチリスト削除: %s", code)


def get_watching_entries() -> list[dict]:
    """status == 'watching' のエントリーのみ返す。"""
    data = load_watchlist()
    return [e for e in data["watchlist"] if e.get("status") == "watching"]


def add_screened_candidates(candidates: list) -> None:
    """
    土曜スクリーニング結果を watchlist に追加する。
    すでに watching / skipped 中の銘柄は上書きしない（既存の監視を継続）。
    """
    data = load_watchlist()
    active_codes = {
        e["code"] for e in data["watchlist"]
        if e.get("status") in ("watching", "skipped")
    }

    today = date.today().isoformat()
    added = 0
    for c in candidates:
        code = c.ticker.replace(".T", "")
        if code in active_codes:
            logger.debug("ウォッチリスト: %s は既に監視中のためスキップ", code)
            continue
        entry = {
            "code": code,
            "ticker": c.ticker,
            "name": c.name,
            "screened_date": today,
            "status": "watching",
            "price": c.price,
            "touch_count": c.touch_count,
            "weekly_ma20": round(c.weekly_ma20, 1),
            "pullback_pct": round(c.pullback_pct, 4),
            # portfolio.json 書き込み用（check_entry 後に更新される）
            "lot_suggest": 0,
            "stop_loss": 0.0,
            "target": 0.0,
            "ma20w": round(c.weekly_ma20, 1),
        }
        data["watchlist"].append(entry)
        added += 1
        logger.info("ウォッチリスト追加: %s %s", code, c.name)

    save_watchlist(data)
    logger.info("スクリーニング結果 %d 銘柄をウォッチリストに追加（既存スキップ含む）", added)


def reset_daily_skipped() -> None:
    """
    毎朝エントリー判断の前に呼び出す。
    前日 skipped になった銘柄を watching に戻して再判断対象にする。
    """
    data = load_watchlist()
    count = 0
    for e in data["watchlist"]:
        if e.get("status") == "skipped":
            e["status"] = "watching"
            count += 1
    if count:
        save_watchlist(data)
        logger.info("skipped → watching リセット: %d 銘柄", count)
    else:
        logger.debug("リセット対象の skipped 銘柄なし")


def expire_old_entries() -> list[str]:
    """
    screened_date から WATCHLIST_EXPIRY_DAYS 日を超えた watching 銘柄を expired にする。
    Returns: 期限切れになったコードのリスト
    """
    data = load_watchlist()
    today = date.today()
    expired_codes: list[str] = []

    for e in data["watchlist"]:
        if e.get("status") != "watching":
            continue
        try:
            screened = date.fromisoformat(e["screened_date"])
            elapsed = (today - screened).days
            if elapsed > WATCHLIST_EXPIRY_DAYS:
                e["status"] = "expired"
                expired_codes.append(e["code"])
                logger.info(
                    "期限切れ: %s %s（%s から %d日経過）",
                    e["code"], e.get("name", ""), e["screened_date"], elapsed,
                )
        except (ValueError, KeyError):
            pass

    if expired_codes:
        save_watchlist(data)
    return expired_codes


def mark_entry_status(code: str, status: str) -> None:
    """watchlist エントリーの status を更新する。"""
    data = load_watchlist()
    for e in data["watchlist"]:
        if e["code"] == code:
            old = e.get("status", "?")
            e["status"] = status
            save_watchlist(data)
            logger.debug("watchlist status: %s  %s → %s", code, old, status)
            return
    logger.warning("watchlist に %s が見つかりません（mark_entry_status）", code)


def migrate_from_candidates_json(candidates_file: Path) -> int:
    """
    Railway 再デプロイ後などで watchlist が空になった場合に、
    candidates.json が存在すれば watchlist へ自動移行する。

    Returns: 追加した銘柄数
    """
    if not candidates_file.exists():
        logger.info("candidates.json が見つかりません（移行スキップ）")
        return 0

    try:
        raw = json.loads(candidates_file.read_text(encoding="utf-8"))
        candidates_data = raw.get("candidates", [])
        if not candidates_data:
            logger.info("candidates.json に銘柄なし（移行スキップ）")
            return 0

        screened_date = raw.get("date", date.today().isoformat())
        wl = load_watchlist()
        active_codes = {
            e["code"] for e in wl["watchlist"]
            if e.get("status") in ("watching", "skipped")
        }

        added = 0
        for c in candidates_data:
            code = c["ticker"].replace(".T", "")
            if code in active_codes:
                continue
            entry = {
                "code": code,
                "ticker": c["ticker"],
                "name": c.get("name", code),
                "screened_date": screened_date,
                "status": "watching",
                "price": c.get("price", 0.0),
                "touch_count": c.get("touch_count", 1),
                "weekly_ma20": c.get("weekly_ma20", 0.0),
                "pullback_pct": c.get("pullback_pct", 0.0),
                "lot_suggest": 0,
                "stop_loss": 0.0,
                "target": 0.0,
                "ma20w": c.get("weekly_ma20", 0.0),
            }
            wl["watchlist"].append(entry)
            added += 1

        if added:
            save_watchlist(wl)
            logger.info(
                "candidates.json（%s）→ watchlist 自動移行: %d 銘柄",
                screened_date, added,
            )
        return added

    except Exception as e:
        logger.warning("candidates.json 移行エラー: %s", e)
        return 0


# ─────────────────────────────────────────────
# portfolio.json 書き込み
# ─────────────────────────────────────────────

def add_to_portfolio(
    watchlist_entry: dict,
    open_price: float,
) -> None:
    """
    ウォッチリストエントリーを portfolio.json の positions に追記する。
    entry_price は寄り付き始値（open_price）を使用する。
    """
    ticker = f"{watchlist_entry['code']}.T"

    position = {
        "ticker": ticker,
        "name": watchlist_entry["name"],
        "entry_price": round(open_price, 1),
        "lots": watchlist_entry["lot_suggest"],
        "lot_type": "打診",
        "stop_loss": watchlist_entry["stop_loss"],
        "target": watchlist_entry["target"],
        "entry_date": date.today().isoformat(),
        "status": "active",
        "weekly_ma20": watchlist_entry["ma20w"],
        "prev_high": None,
        "recent_highs": [],
    }

    if PORTFOLIO_FILE.exists():
        data = json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
    else:
        data = {"positions": []}

    data["positions"].append(position)
    data["updated_at"] = datetime.now().isoformat()

    PORTFOLIO_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "portfolio.json に追記: %s %s @ %.0f円 %d株",
        ticker, watchlist_entry["name"], open_price, watchlist_entry["lot_suggest"],
    )
