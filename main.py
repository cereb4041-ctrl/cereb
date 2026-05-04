"""
エントリポイント。
  通常起動: python main.py          → 毎週土曜8時スクリーニング + 平日9:15エントリー判断
  即時スクリーニング: python main.py --now
  即時エントリー判断: python main.py --entry
"""
import logging
import sys
import time
from datetime import datetime

import pytz
from dotenv import load_dotenv

from entry_judge import run_entry_checks, save_candidates
from notifier import notify, notify_entry
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

# エントリー判断: 平日（月〜金）09:15 JST
_ENTRY_WEEKDAY_RANGE = range(0, 5)   # 0=月, 4=金
_ENTRY_HOUR          = 9
_ENTRY_MINUTE        = 15


def run_screening() -> None:
    """週次スクリーニングを実行し、結果を保存してLINEに通知する。"""
    logger.info("======== スクリーニング開始 ========")
    try:
        tickers, names = get_prime_universe()
        candidates = screen(tickers, names)
        notify(candidates)
        save_candidates(candidates)   # エントリー判断用に保存
    except Exception as e:
        logger.exception("スクリーニングエラー: %s", e)
    logger.info("======== スクリーニング完了 ========")


def run_entry() -> None:
    """エントリー判断を実行してLINEに通知する。"""
    logger.info("======== エントリー判断開始 ========")
    try:
        results = run_entry_checks(check_opening=True)
        notify_entry(results)
    except Exception as e:
        logger.exception("エントリー判断エラー: %s", e)
    logger.info("======== エントリー判断完了 ========")


def _now_jst() -> datetime:
    return datetime.now(JST)


def main() -> None:
    if "--now" in sys.argv:
        run_screening()
        return

    if "--entry" in sys.argv:
        run_entry()
        return

    logger.info(
        "スクリーナー起動。\n"
        "  スクリーニング: 毎週土曜 %02d:%02d JST\n"
        "  エントリー判断: 平日月〜金 %02d:%02d JST",
        _SCREEN_HOUR, _SCREEN_MINUTE,
        _ENTRY_HOUR, _ENTRY_MINUTE,
    )

    last_screen_date = None
    last_entry_date  = None

    while True:
        now = _now_jst()

        # 週次スクリーニング（土曜 08:00）
        if (now.weekday() == _SCREEN_WEEKDAY
                and now.hour == _SCREEN_HOUR
                and now.minute == _SCREEN_MINUTE
                and last_screen_date != now.date()):
            last_screen_date = now.date()
            run_screening()

        # エントリー判断（平日 09:15）
        if (now.weekday() in _ENTRY_WEEKDAY_RANGE
                and now.hour == _ENTRY_HOUR
                and now.minute == _ENTRY_MINUTE
                and last_entry_date != now.date()):
            last_entry_date = now.date()
            run_entry()

        time.sleep(60)


if __name__ == "__main__":
    main()
