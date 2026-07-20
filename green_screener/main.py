"""
green_screener エントリポイント。

    python green_screener/main.py                 通常実行（結果保存 + LINE通知）
    python green_screener/main.py --no-line       LINE送信せずコンソール出力のみ
    python green_screener/main.py --limit 50      先頭50銘柄だけ処理（動作確認用）
    python green_screener/main.py --ticker 7203   特定銘柄だけ判定
    python green_screener/main.py --clear-cache   キャッシュを削除してから実行

既存システム（プロジェクト直下の main.py）とは独立して動作する。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 単体スクリプトとして実行された場合でもパッケージ import を成立させる
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "green_screener"

from dotenv import load_dotenv

from . import config
from .notifier import notify
from .screener import analyze, screen
from .universe import get_universe
from .utils import clear_cache, logger, save_results, setup_logging


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ぐりーん式スイングトレード・スクリーナー")
    p.add_argument("--no-line", action="store_true", help="LINE送信せずコンソール出力のみ")
    p.add_argument("--limit", type=int, default=None, help="処理する銘柄数の上限（動作確認用）")
    p.add_argument("--ticker", type=str, default=None, help="特定銘柄のみ判定（例: 7203）")
    p.add_argument("--clear-cache", action="store_true", help="実行前にキャッシュを削除")
    p.add_argument("--no-save", action="store_true", help="results/ へ保存しない")
    return p.parse_args()


def run_single(code: str, to_line: bool) -> int:
    """特定銘柄だけを判定する（動作確認・デバッグ用）。"""
    ticker = code if code.endswith(".T") else f"{code}.T"
    logger.info("単体判定: %s", ticker)

    result = analyze(ticker, ticker)
    if result is None:
        logger.error("データを取得できませんでした: %s", ticker)
        return 1

    notify([result], to_line=to_line)
    return 0


def run_screening(limit: int | None, to_line: bool, save: bool) -> int:
    """ユニバースを取得してスクリーニングを実行する。"""
    logger.info("======== ぐりーん式スクリーニング開始 ========")

    tickers, names, sectors = get_universe()
    if not tickers:
        logger.error("対象銘柄が0件です。ユニバース取得に失敗した可能性があります")
        return 1

    if limit:
        tickers = tickers[:limit]
        logger.info("--limit 指定により %d 銘柄に絞り込みました", len(tickers))

    candidates = screen(tickers, names, sectors)

    if save and candidates:
        save_results([c.to_dict() for c in candidates])

    notify(candidates, to_line=to_line)

    logger.info("======== ぐりーん式スクリーニング完了 ========")
    return 0


def main() -> int:
    args = _parse_args()
    load_dotenv()
    setup_logging()

    if args.clear_cache:
        n = clear_cache()
        logger.info("キャッシュを削除しました: %d ファイル", n)

    to_line = not args.no_line

    try:
        if args.ticker:
            return run_single(args.ticker, to_line)
        return run_screening(args.limit, to_line, save=not args.no_save)
    except KeyboardInterrupt:
        logger.warning("中断されました")
        return 130
    except Exception as e:
        logger.exception("実行エラー: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
