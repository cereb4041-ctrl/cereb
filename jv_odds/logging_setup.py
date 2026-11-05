"""
ログ設定。

  コンソール              : INFO 以上（-v で DEBUG）
  logs/jv_odds.log        : INFO 以上、日次ローテーション 30 日
  logs/jv_odds_error.log  : WARNING 以上、日次ローテーション 90 日（障害調査用）
  logs/jv_odds_debug.log  : DEBUG 以上、日次ローテーション 14 日
                            JVInit/JVRTOpen/JVGets/JVClose の戻り値・所要時間・
                            再試行の回数と待ち時間など、実機の不具合調査に必要な詳細を残す
"""
from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DEBUG_FMT = "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s %(funcName)s:%(lineno)d [%(threadName)s] %(message)s"


def _file_handler(path: Path, level: int, fmt: str, backup: int) -> logging.Handler:
    h = TimedRotatingFileHandler(path, when="midnight", backupCount=backup, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
    return h


def setup_logging(log_dir: Path, verbose: bool = False, debug_file: bool = True) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_jv_odds_configured", False):
        return
    root.setLevel(logging.DEBUG)  # 振り分けは各ハンドラのレベルで行う

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter(FMT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(console)

    root.addHandler(_file_handler(log_dir / "jv_odds.log", logging.INFO, FMT, 30))
    root.addHandler(_file_handler(log_dir / "jv_odds_error.log", logging.WARNING, FMT, 90))
    if debug_file:
        dh = _file_handler(log_dir / "jv_odds_debug.log", logging.DEBUG, DEBUG_FMT, 14)
        dh.addFilter(logging.Filter("jv_odds"))  # 他ライブラリの DEBUG は出さない
        root.addHandler(dh)
    root._jv_odds_configured = True  # type: ignore[attr-defined]
