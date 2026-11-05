"""
当日の収集ループ。

  - start_time まで待機
  - 壁時計の interval_minutes 刻み (例: 09:00, 09:10, …) で run_cycle を実行
  - 各レースの「発走 final_fetch_minutes_before 分前」にも追加で 1 回取得（締切直前のオッズ）
  - 当日の全レースの取得時間帯が終わったらループを抜けて終了
  - どんな例外もループ内で捕捉してログに残し、次のタイミングで再試行する
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from . import db
from .collector import OddsCollector, race_window
from .config import Settings

log = logging.getLogger(__name__)


def next_tick(now: datetime, interval_minutes: int) -> datetime:
    """now より後で、0 時から interval_minutes 刻みになる最初の時刻。"""
    base = now.replace(second=0, microsecond=0)
    minutes = base.hour * 60 + base.minute
    nxt = (minutes // interval_minutes + 1) * interval_minutes
    return base.replace(hour=0, minute=0) + timedelta(minutes=nxt)


def final_fetch_times(races, st: Settings) -> dict[datetime, set[str]]:
    """締切直前の追加取得時刻 → 対象 race_id 集合。"""
    out: dict[datetime, set[str]] = {}
    if st.final_fetch_minutes_before <= 0:
        return out
    for r in races:
        if not r["post_time"]:
            continue
        hh, mm = map(int, r["post_time"].split(":"))
        post = datetime.combine(date.fromisoformat(r["race_date"]), datetime.min.time()) \
            + timedelta(hours=hh, minutes=mm)
        t = post - timedelta(minutes=st.final_fetch_minutes_before)
        _, end = race_window(r, st)
        if t < end:
            out.setdefault(t, set()).add(r["race_id"])
    return out


def _sleep_until(target: datetime, now_fn, sleep_fn) -> None:
    while True:
        remain = (target - now_fn()).total_seconds()
        if remain <= 0:
            return
        sleep_fn(min(remain, 5.0))
        try:  # COM のメッセージを処理しておく（JV-Link の安定動作のため）
            import pythoncom  # type: ignore
            pythoncom.PumpWaitingMessages()
        except Exception:
            pass


def run_day(collector: OddsCollector, st: Settings,
            now_fn: Callable[[], datetime] = datetime.now,
            sleep_fn: Callable[[float], None] = time.sleep,
            max_cycles: Optional[int] = None) -> int:
    """当日の収集ループを実行し、実行したサイクル数を返す。"""
    conn = collector.conn
    today = now_fn().date()
    last_refresh: Optional[datetime] = None
    cycles = 0
    done_finals: set[datetime] = set()
    refresh_ok = False
    tick_at: Optional[datetime] = None   # 次に通常取得すべき時刻（None = 即時）

    while True:
        now = now_fn()
        try:
            # --- レース情報の取得・定期更新（発走時刻変更の反映） ---
            if last_refresh is None or now - last_refresh >= timedelta(minutes=st.race_refresh_minutes):
                try:
                    collector.refresh_races([today])
                    refresh_ok = True
                except Exception as e:
                    log.error("レース情報の更新に失敗（次回再試行）: %s", e)
                    refresh_ok = False
                last_refresh = now

            races = db.races_on(conn, today.isoformat())
            if not races:
                if not refresh_ok and now.time() < st.fallback_end_time:
                    # 取得失敗の可能性があるので、締め時刻までは間隔をあけて再試行
                    log.warning("本日 (%s) のレース情報がありません。%d 分後に再試行します",
                                today, st.interval_minutes)
                    last_refresh = None
                    _sleep_until(next_tick(now, st.interval_minutes), now_fn, sleep_fn)
                    continue
                log.info("本日 (%s) の開催レースはありません。終了します", today)
                return cycles

            last_end = max(race_window(r, st)[1] for r in races)
            if now >= last_end:
                log.info("本日の全レースの取得時間帯が終了しました。終了します (cycles=%d)", cycles)
                return cycles

            start = datetime.combine(today, st.start_time)
            if now < start:
                log.info("収集開始時刻 %s まで待機します", start.strftime("%H:%M"))
                _sleep_until(start, now_fn, sleep_fn)
                continue

            # --- 取得 ---
            finals = final_fetch_times(races, st)
            due_final = [t for t in finals if t <= now and t not in done_finals
                         and now - t < timedelta(minutes=st.interval_minutes)]
            if tick_at is None or now >= tick_at:
                tick_at = next_tick(now, st.interval_minutes)
                collector.run_cycle(now)
                done_finals.update(due_final)  # 通常取得と重なった締切直前取得は不要
            elif due_final:
                ids = set().union(*(finals[t] for t in due_final))
                log.info("締切直前の取得: %s", ", ".join(sorted(ids)))
                collector.run_cycle(now, race_ids=ids)
                done_finals.update(due_final)
            cycles += 1
        except Exception:
            # ここに来るのは想定外の例外（DB ロック・COM 生成失敗など）。終了せず次回へ
            log.exception("収集サイクルで予期しないエラー（次回再試行）")
            try:
                collector.client.reset()
            except Exception:
                pass
            cycles += 1

        if max_cycles is not None and cycles >= max_cycles:
            return cycles

        # --- 次の実行時刻まで待機 ---
        now = now_fn()
        candidates = [tick_at or next_tick(now, st.interval_minutes)]
        candidates += [t for t in final_fetch_times(db.races_on(conn, today.isoformat()), st)
                       if t > now and t not in done_finals]
        wake = min(candidates)
        log.debug("次回実行: %s", wake.strftime("%H:%M"))
        _sleep_until(wake, now_fn, sleep_fn)
