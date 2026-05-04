"""
東証プライム銘柄ユニバース取得モジュール。
JPX公開Excelから銘柄リストを取得し、出来高・株価でフィルタリングする。
"""
import logging
import time

import pandas as pd
import yfinance as yf

import config

logger = logging.getLogger(__name__)


def fetch_prime_tickers() -> list[str]:
    """JPX公開ExcelからプライムTickerリストを取得する。"""
    logger.info("JPX銘柄一覧を取得中: %s", config.JPX_XLS_URL)
    try:
        df = pd.read_excel(config.JPX_XLS_URL, dtype=str)
    except Exception as e:
        logger.error("JPX Excel取得失敗: %s", e)
        raise

    # カラム名の確認（JPXはカラム名を変更することがある）
    # '銘柄コード' または 'コード' のどちらかに対応
    code_col = None
    for candidate in ("銘柄コード", "コード"):
        if candidate in df.columns:
            code_col = candidate
            break

    required = {"市場・商品区分", "銘柄名"}
    missing = required - set(df.columns)
    if missing or code_col is None:
        raise ValueError(
            f"JPX Excelのカラム名が想定と異なります。\n"
            f"  コードカラム検出: {code_col}\n"
            f"  不足カラム: {missing}\n"
            f"  実際のカラム: {df.columns.tolist()}"
        )

    prime = df[df["市場・商品区分"] == config.PRIME_MARKET_NAME].copy()
    prime["ticker"] = prime[code_col].str.strip().str.zfill(4) + ".T"

    tickers = prime["ticker"].tolist()
    names = dict(zip(prime["ticker"], prime["銘柄名"]))
    logger.info("プライム銘柄数: %d", len(tickers))
    return tickers, names


def filter_by_volume_and_price(tickers: list[str]) -> list[str]:
    """直近5営業日のデータをバッチ取得し、出来高・株価フィルタを適用する。"""
    logger.info("出来高・株価フィルタ適用中（対象: %d 銘柄）...", len(tickers))

    passed = []
    batch_size = 200  # バルクDL用は大きめバッチ

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        try:
            raw = yf.download(
                batch,
                period="5d",
                interval="1d",
                auto_adjust=True,
                threads=True,
                progress=False,
                group_by="ticker",
            )
        except Exception as e:
            logger.warning("バッチ %d-%d 取得エラー: %s", i, i + batch_size, e)
            time.sleep(config.RETRY_SLEEP)
            continue

        for ticker in batch:
            try:
                if len(batch) == 1:
                    close_series = raw["Close"].dropna()
                    vol_series   = raw["Volume"].dropna()
                else:
                    close_series = raw[ticker]["Close"].dropna()
                    vol_series   = raw[ticker]["Volume"].dropna()

                if close_series.empty or vol_series.empty:
                    continue

                price  = float(close_series.iloc[-1])
                volume = float(vol_series.iloc[-1])

                if price <= config.MAX_PRICE and volume >= config.MIN_VOLUME:
                    passed.append(ticker)
            except (KeyError, IndexError):
                continue

        time.sleep(1.0)  # バッチ間スリープ

    logger.info("フィルタ通過銘柄数: %d / %d", len(passed), len(tickers))
    return passed


def get_prime_universe() -> tuple[list[str], dict[str, str]]:
    """プライム銘柄を取得し、出来高・株価フィルタ後のリストと銘柄名辞書を返す。"""
    tickers, names = fetch_prime_tickers()
    filtered = filter_by_volume_and_price(tickers)
    filtered_names = {t: names.get(t, t) for t in filtered}
    return filtered, filtered_names
