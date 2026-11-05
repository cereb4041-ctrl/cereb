"""ログ設定: コンソール + 日次ローテーションのファイル出力。"""
from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_jv_odds_configured", False):
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter(FMT, datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    fh = TimedRotatingFileHandler(
        log_dir / "jv_odds.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)

    # エラー以上は別ファイルにも残す（障害調査用）
    eh = TimedRotatingFileHandler(
        log_dir / "jv_odds_error.log", when="midnight", backupCount=90, encoding="utf-8"
    )
    eh.setLevel(logging.WARNING)
    eh.setFormatter(formatter)
    root.addHandler(eh)
    root._jv_odds_configured = True  # type: ignore[attr-defined]
