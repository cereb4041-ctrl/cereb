"""
エントリポイント。
  通常起動: python main.py         → 毎週土曜8時(JST)に自動実行
  即時実行: python main.py --now   → スクリーニングをその場で実行

タイムゾーン:
  schedule ライブラリはシステム時刻に依存するため、
  クラウドサーバー(UTC)でも確実に JST 08:00 に動くよう
  pytz で現在時刻を JST に変換してチェックする方式を採用。
"""
import logging
import sys
import time
from datetime import datetime

import pytz
from dotenv import load_dotenv

from notifier import notify
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

# 実行条件: 土曜日(weekday=5) の 8:00〜8:01 JST
_RUN_WEEKDAY = 5   # Saturday
_RUN_HOUR    = 8
_RUN_MINUTE  = 0


def run_all() -> None:
    logger.info("======== スクリーニング開始 ========")
    try:
        tickers, names = get_prime_universe()
        candidates = screen(tickers, names)
        notify(candidates)
    except Exception as e:
        logger.exception("スクリーニング中に予期せぬエラーが発生しました: %s", e)
    logger.info("======== スクリーニング完了 ========")


def _should_run_now() -> bool:
    now = datetime.now(JST)
    return (
        now.weekday() == _RUN_WEEKDAY
        and now.hour == _RUN_HOUR
        and now.minute == _RUN_MINUTE
    )


def main() -> None:
    if "--now" in sys.argv:
        run_all()
        return

    logger.info("スクリーナー起動。毎週土曜 %02d:%02d JST に実行します。", _RUN_HOUR, _RUN_MINUTE)

    last_run_date = None  # 同じ土曜日に2回動かないようにする

    while True:
        if _should_run_now():
            today = datetime.now(JST).date()
            if last_run_date != today:
                last_run_date = today
                run_all()
        time.sleep(60)


if __name__ == "__main__":
    main()
