"""
エントリポイント。
  通常起動: python main.py          → スケジューラ起動 + Webhookサーバー起動
  即時スクリーニング: python main.py --now
  即時エントリー判断: python main.py --entry
  即時ポジション監視: python main.py --monitor
"""
import logging
import os
import sys
import threading
import time
from datetime import datetime

import pytz
from dotenv import load_dotenv

from entry_judge import run_entry_checks, run_entry_checks_from_watchlist, save_candidates
from notifier import notify, notify_entry, notify_exit
from positions import check_exit_signals
from screener import screen
from universe import get_prime_universe

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

JST = pytz.timezone("Asia/Tokyo")

# 週次スクリーニング: 土曜 08:00 JST
_SCREEN_WEEKDAY = 5
_SCREEN_HOUR    = 8
_SCREEN_MINUTE  = 0

# エントリー判断: 平日（月〜金）09:10 JST
# （1分足 9:00 の足が確実に確定するのを待つため 9:05 から 9:10 に変更）
_ENTRY_WEEKDAY_RANGE = range(0, 5)
_ENTRY_HOUR          = 9
_ENTRY_MINUTE        = 10

# ポジション監視: 平日 15:45 JST（大引け後）
_MONITOR_HOUR   = 15
_MONITOR_MINUTE = 45


def run_screening() -> None:
    logger.info("======== スクリーニング開始 ========")
    try:
        from watchlist_manager import add_screened_candidates
        tickers, names = get_prime_universe()
        candidates = screen(tickers, names)
        notify(candidates)
        save_candidates(candidates)
        # ウォッチリストに追加（既存の watching/skipped 銘柄は上書きしない）
        add_screened_candidates(candidates)
    except Exception as e:
        logger.exception("スクリーニングエラー: %s", e)
    logger.info("======== スクリーニング完了 ========")


def run_entry() -> None:
    """
    watchlist.json の watching 銘柄を毎日チェックし、LINE 通知する。

    フロー:
      0. watchlist 空なら candidates.json から自動移行（Railway 再デプロイ対策）
      1. 前日 skipped → watching にリセット
      2. 14日超過エントリーを expired に
      3. watching 全銘柄のエントリー条件チェック
      4. 結果に応じて status 更新（passed / skipped / expired）
      5. LINE 通知（候補なしでも通知して動作確認できるようにする）
    """
    logger.info("======== エントリー判断開始 ========")
    try:
        from watchlist_manager import (
            reset_daily_skipped,
            expire_old_entries,
            mark_entry_status,
            get_watching_entries,
            migrate_from_candidates_json,
        )
        from entry_judge import CANDIDATES_FILE

        # ステップ0: watchlist が空なら candidates.json から自動移行
        # （Railway 再デプロイ後にファイルが消えた場合の復旧）
        if not get_watching_entries():
            n = migrate_from_candidates_json(CANDIDATES_FILE)
            if n:
                logger.info("watchlist を candidates.json から自動復元: %d 銘柄", n)
            else:
                logger.info("candidates.json も空 → 土曜スクリーニング待ち")

        # ステップ1: 前日見送りをリセット
        reset_daily_skipped()

        # ステップ2: 期限切れを処理
        expired = expire_old_entries()
        if expired:
            logger.info("期限切れ除外: %s", expired)

        # ステップ3: 監視中銘柄をチェック
        results = run_entry_checks_from_watchlist(check_opening=True)

        # ステップ4: 結果を watchlist に反映
        for r in results:
            code = r.ticker.replace(".T", "")
            if r.all_met:
                mark_entry_status(code, "passed")
            elif r.weekly_expired:
                mark_entry_status(code, "expired")
                logger.info("週足根拠崩れ → expired: %s %s", code, r.name)
            else:
                mark_entry_status(code, "skipped")

        # ステップ5: 通知（candidates もなければ "候補なし" を送り、システム稼働を確認できるようにする）
        notify_entry(results)

    except Exception as e:
        logger.exception("エントリー判断エラー: %s", e)
    logger.info("======== エントリー判断完了 ========")


def run_monitor() -> None:
    logger.info("======== ポジション監視開始 ========")
    try:
        signals = check_exit_signals()
        if signals:
            notify_exit(signals)
            logger.info("デスクロスアラート: %d 銘柄", len(signals))
        else:
            logger.info("デスクロス銘柄なし")
    except Exception as e:
        logger.exception("ポジション監視エラー: %s", e)
    logger.info("======== ポジション監視完了 ========")


def _now_jst() -> datetime:
    return datetime.now(JST)


def _scheduler_loop() -> None:
    logger.info(
        "スケジューラ起動。\n"
        "  スクリーニング: 毎週土曜 %02d:%02d JST\n"
        "  エントリー判断: 平日月〜金 %02d:%02d JST\n"
        "  ポジション監視: 平日月〜金 %02d:%02d JST",
        _SCREEN_HOUR, _SCREEN_MINUTE,
        _ENTRY_HOUR, _ENTRY_MINUTE,
        _MONITOR_HOUR, _MONITOR_MINUTE,
    )

    last_screen_date  = None
    last_entry_date   = None
    last_monitor_date = None

    while True:
        now = _now_jst()

        if (now.weekday() == _SCREEN_WEEKDAY
                and now.hour == _SCREEN_HOUR
                and now.minute == _SCREEN_MINUTE
                and last_screen_date != now.date()):
            last_screen_date = now.date()
            run_screening()

        if (now.weekday() in _ENTRY_WEEKDAY_RANGE
                and now.hour == _ENTRY_HOUR
                and now.minute == _ENTRY_MINUTE
                and last_entry_date != now.date()):
            last_entry_date = now.date()
            run_entry()

        if (now.weekday() in _ENTRY_WEEKDAY_RANGE
                and now.hour == _MONITOR_HOUR
                and now.minute == _MONITOR_MINUTE
                and last_monitor_date != now.date()):
            last_monitor_date = now.date()
            run_monitor()

        time.sleep(60)


def main() -> None:
    if "--now" in sys.argv:
        run_screening()
        return

    if "--entry" in sys.argv:
        run_entry()
        return

    if "--monitor" in sys.argv:
        run_monitor()
        return

    # スケジューラをバックグラウンドスレッドで起動
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()

    # FlaskサーバーをメインスレッドでPORTにバインド（Railway公開URL用）
    from webhook import app as flask_app
    port = int(os.getenv("PORT", 8080))
    logger.info("Webhookサーバー起動: port=%d", port)
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
