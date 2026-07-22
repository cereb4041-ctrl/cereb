"""
green_screener 共通ユーティリティ。

ログ設定・JST日付・キャッシュ付きデータ取得・結果保存を担当する。
既存システムのモジュールには依存しない。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz
import yfinance as yf

try:
    import jpholiday
    _HAS_JPHOLIDAY = True
except ImportError:      # ライブラリ未導入でも土日判定だけは効かせる
    _HAS_JPHOLIDAY = False

from . import config

JST = pytz.timezone("Asia/Tokyo")

logger = logging.getLogger("green_screener")


# ─────────────────────────────────────────────
# ログ
# ─────────────────────────────────────────────

def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """コンソールと logs/green_screener_YYYY-MM-DD.log の両方に出力する。"""
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if logger.handlers:      # 二重登録を防ぐ
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    logfile = config.LOGS_DIR / f"green_screener_{today_str()}.log"
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.propagate = False   # ルートロガーへの二重出力を防ぐ
    return logger


# ─────────────────────────────────────────────
# 日付（すべて JST 基準）
# ─────────────────────────────────────────────

def now_jst() -> datetime:
    return datetime.now(JST)


def today_str() -> str:
    return now_jst().strftime("%Y-%m-%d")


# ─────────────────────────────────────────────
# 営業日判定（東証の休場日）
# ─────────────────────────────────────────────

def is_market_holiday(d: date | None = None) -> tuple[bool, str]:
    """
    東証が休場かどうかを判定する。
    Returns: (休場か, 理由)  営業日なら (False, "")。

    休場条件:
      - 土曜・日曜
      - 日本の祝日（振替休日を含む。jpholiday 導入時のみ判定）
      - 年末年始（12/31〜1/3）は祝日でなくても東証は休場
    """
    if d is None:
        d = now_jst().date()

    if d.weekday() >= 5:
        return True, "土日"

    # 年末年始の取引所休場（大納会・大発会の慣例）
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3):
        return True, "年末年始休場"

    if _HAS_JPHOLIDAY:
        name = jpholiday.is_holiday_name(d)
        if name:
            return True, name

    return False, ""


def is_business_day(d: date | None = None) -> bool:
    """東証の営業日なら True。"""
    holiday, _ = is_market_holiday(d)
    return not holiday


# ─────────────────────────────────────────────
# キャッシュ付きデータ取得
# ─────────────────────────────────────────────

def _cache_path(ticker: str, period: str, interval: str) -> Path:
    safe = ticker.replace(".", "_")
    return config.CACHE_DIR / f"{safe}_{period}_{interval}.pkl"


def _cache_is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age < timedelta(hours=config.CACHE_TTL_HOURS)


def fetch_ohlc(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """
    yfinance から OHLC を取得する。
    CACHE_TTL_HOURS 以内のキャッシュがあれば再利用し、API呼び出しを減らす。
    取得できなければ空の DataFrame を返す。
    """
    cache_file = _cache_path(ticker, period, interval)

    if config.CACHE_ENABLED and _cache_is_fresh(cache_file):
        try:
            return pd.read_pickle(cache_file)
        except Exception as e:
            logger.debug("キャッシュ読込失敗 %s: %s", cache_file.name, e)

    df = pd.DataFrame()
    for attempt in range(config.MAX_RETRIES + 1):
        try:
            raw = yf.download(ticker, period=period, interval=interval,
                              auto_adjust=True, progress=False)
            if raw is not None and not raw.empty:
                df = normalize_columns(raw)
                break
        except Exception as e:
            logger.debug("%s 取得エラー (attempt %d): %s", ticker, attempt + 1, e)
        if attempt < config.MAX_RETRIES:
            time.sleep(config.RETRY_SLEEP)

    if config.CACHE_ENABLED and not df.empty:
        try:
            config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            df.to_pickle(cache_file)
        except Exception as e:
            logger.debug("キャッシュ保存失敗 %s: %s", cache_file.name, e)

    return df


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance が返す MultiIndex カラムを単層に均す。"""
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


def clear_cache() -> int:
    """キャッシュを全削除する。Returns: 削除したファイル数。"""
    if not config.CACHE_DIR.exists():
        return 0
    count = 0
    for f in config.CACHE_DIR.glob("*.pkl"):
        try:
            f.unlink()
            count += 1
        except Exception:
            pass
    return count


# ─────────────────────────────────────────────
# テクニカル指標
# ─────────────────────────────────────────────

def moving_averages(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(短期, 中期, 長期) の単純移動平均を返す。"""
    return (
        close.rolling(config.MA_SHORT).mean(),
        close.rolling(config.MA_MID).mean(),
        close.rolling(config.MA_LONG).mean(),
    )


def is_perfect_order(ma_s: pd.Series, ma_m: pd.Series, ma_l: pd.Series) -> bool:
    """最新バーでパーフェクトオーダー（短期 > 中期 > 長期）が成立しているか。"""
    try:
        s, m, l = float(ma_s.iloc[-1]), float(ma_m.iloc[-1]), float(ma_l.iloc[-1])
    except (IndexError, ValueError):
        return False
    if any(pd.isna(x) for x in (s, m, l)):
        return False
    return s > m > l


def is_rising(ma: pd.Series, bars: int) -> bool:
    """移動平均が bars 本前と比べて上向きか。"""
    if len(ma) <= bars:
        return False
    latest, past = ma.iloc[-1], ma.iloc[-1 - bars]
    if pd.isna(latest) or pd.isna(past):
        return False
    return float(latest) > float(past)


def merge_events(indices: list[int], gap: int) -> list[list[int]]:
    """近接するインデックスを1つのイベントにまとめる。"""
    if not indices:
        return []
    events: list[list[int]] = [[indices[0]]]
    for idx in indices[1:]:
        if idx - events[-1][-1] <= gap:
            events[-1].append(idx)
        else:
            events.append([idx])
    return events


def count_ma_touches(low: pd.Series, close: pd.Series,
                     ma_s: pd.Series, ma_m: pd.Series, ma_l: pd.Series,
                     tolerance: float, lookback: int,
                     gap: int, cutoff: int) -> int:
    """
    中期線へのタッチ回数を数える。

    タッチの条件（すべて満たすバーをタッチとみなす）:
      - そのバーでパーフェクトオーダーが成立している
      - 安値が中期線 × (1 + tolerance) 以内まで下げた
      - 終値は中期線より上で終えた（下抜けではなく反発）

    最後のタッチが cutoff より古い場合は 0 を返す。
    """
    n = len(close)
    touch_idx: list[int] = []

    for rel in range(-lookback, 0):
        i = n + rel
        if i < 0:
            continue
        if pd.isna(ma_s.iloc[i]) or pd.isna(ma_m.iloc[i]) or pd.isna(ma_l.iloc[i]):
            continue
        po_ok     = float(ma_s.iloc[i]) > float(ma_m.iloc[i]) > float(ma_l.iloc[i])
        touched   = float(low.iloc[i]) <= float(ma_m.iloc[i]) * (1 + tolerance)
        closed_up = float(close.iloc[i]) > float(ma_m.iloc[i])
        if po_ok and touched and closed_up:
            touch_idx.append(rel)

    events = merge_events(touch_idx, gap)
    if not events:
        return 0
    if events[-1][-1] < cutoff:
        return 0
    return len(events)


def pullback_pct(close: pd.Series, lookback: int) -> float | None:
    """直近 lookback 本の高値からの下落率を返す。算出できなければ None。"""
    if len(close) < lookback:
        return None
    recent_high = float(close.iloc[-lookback:].max())
    current     = float(close.iloc[-1])
    if recent_high <= 0:
        return None
    return (recent_high - current) / recent_high


# ─────────────────────────────────────────────
# 結果保存
# ─────────────────────────────────────────────

def save_results(results: list[dict]) -> list[Path]:
    """結果を results/ に CSV と JSON で保存する。Returns: 保存したパス一覧。"""
    if not results:
        return []

    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_jst().strftime("%Y%m%d_%H%M%S")
    saved: list[Path] = []

    if config.SAVE_CSV:
        csv_path = config.RESULTS_DIR / f"green_{stamp}.csv"
        try:
            pd.DataFrame(results).to_csv(csv_path, index=False, encoding="utf-8-sig")
            saved.append(csv_path)
        except Exception as e:
            logger.warning("CSV保存失敗: %s", e)

    if config.SAVE_JSON:
        json_path = config.RESULTS_DIR / f"green_{stamp}.json"
        try:
            payload = {"date": today_str(), "count": len(results), "results": results}
            json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved.append(json_path)
        except Exception as e:
            logger.warning("JSON保存失敗: %s", e)

    for p in saved:
        logger.info("結果を保存: %s", p)
    return saved
