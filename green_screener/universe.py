"""
東証プライム銘柄ユニバースの取得（green_screener 独立版）。

JPX公開Excelから銘柄一覧を取得し、出来高・株価でふるいにかける。
銘柄一覧は日次でキャッシュし、同日中の再実行では再ダウンロードしない。
"""
from __future__ import annotations

import gc
import json
import time

import pandas as pd
import yfinance as yf

from . import config
from .utils import logger, today_str


def _universe_cache_path():
    return config.CACHE_DIR / f"universe_{today_str()}.json"


def fetch_prime_tickers() -> tuple[list[str], dict[str, str]]:
    """
    JPX の上場銘柄一覧からプライム銘柄を抽出する。
    Returns: (ティッカーリスト, {ticker: 銘柄名})
    """
    cache = _universe_cache_path()
    if config.CACHE_ENABLED and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            logger.info("銘柄一覧をキャッシュから読込: %d 銘柄", len(data["tickers"]))
            return data["tickers"], data["names"]
        except Exception as e:
            logger.debug("銘柄一覧キャッシュ読込失敗: %s", e)

    logger.info("JPX銘柄一覧を取得中: %s", config.JPX_XLS_URL)
    df = pd.read_excel(config.JPX_XLS_URL, dtype=str)

    # JPX はカラム名を変更することがあるため両方に対応する
    code_col = next((c for c in ("銘柄コード", "コード") if c in df.columns), None)
    required = {"市場・商品区分", "銘柄名"}
    missing  = required - set(df.columns)
    if code_col is None or missing:
        raise ValueError(
            f"JPX Excelのカラムが想定と異なります。"
            f" コードカラム={code_col} 不足={missing} 実際={df.columns.tolist()}"
        )

    prime = df[df["市場・商品区分"] == config.PRIME_MARKET_NAME].copy()
    prime["ticker"] = prime[code_col].str.strip().str.zfill(4) + ".T"

    tickers = prime["ticker"].tolist()
    names   = dict(zip(prime["ticker"], prime["銘柄名"]))
    logger.info("プライム銘柄数: %d", len(tickers))

    if config.CACHE_ENABLED:
        try:
            config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps({"tickers": tickers, "names": names}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug("銘柄一覧キャッシュ保存失敗: %s", e)

    return tickers, names


def filter_by_volume_and_price(tickers: list[str]) -> list[str]:
    """直近5営業日をバルク取得し、出来高・株価の条件で絞り込む。"""
    logger.info("出来高・株価フィルタ適用中（対象: %d 銘柄）...", len(tickers))
    passed: list[str] = []
    size = config.UNIVERSE_BATCH_SIZE

    for i in range(0, len(tickers), size):
        batch = tickers[i : i + size]
        try:
            raw = yf.download(
                batch, period="5d", interval="1d",
                auto_adjust=True, threads=True, progress=False, group_by="ticker",
            )
        except Exception as e:
            logger.warning("バッチ %d-%d 取得エラー: %s", i, i + size, e)
            time.sleep(config.RETRY_SLEEP)
            continue

        for ticker in batch:
            try:
                sub = raw if len(batch) == 1 else raw[ticker]
                close = sub["Close"].dropna()
                vol   = sub["Volume"].dropna()
                if close.empty or vol.empty:
                    continue
                price  = float(close.iloc[-1])
                volume = float(vol.iloc[-1])
                if (config.MIN_PRICE <= price <= config.MAX_PRICE
                        and volume >= config.MIN_VOLUME):
                    passed.append(ticker)
            except (KeyError, IndexError):
                continue

        # メモリ対策: バッチごとに大きな DataFrame を解放する
        del raw
        gc.collect()
        time.sleep(1.0)

    logger.info("フィルタ通過銘柄数: %d / %d", len(passed), len(tickers))
    return passed


def get_universe() -> tuple[list[str], dict[str, str]]:
    """スクリーニング対象のティッカーと銘柄名辞書を返す。"""
    tickers, names = fetch_prime_tickers()
    filtered = filter_by_volume_and_price(tickers)
    return filtered, {t: names.get(t, t) for t in filtered}
