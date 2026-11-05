"""
オッズ収集のコア処理。

  refresh_races()  : 今週の開催レース (RA) を取得して races テーブルへ登録
  collect_race()   : 1 レース分の最新オッズ (0B31: 単複枠 / 0B32: 馬連) を取得し差分保存
  backfill_race()  : 時系列オッズ (0B41 / 0B42) を取得し、過去の発表時刻分もまとめて保存
  run_cycle()      : 「今」取得対象になっているレースすべてを 1 回取得する（10 分ごとに呼ばれる）

通信エラーや JV-Link の切断が起きても例外を外へ投げず、ログと fetch_runs に記録して
次回のタイミングで再試行する。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Optional

from . import db
from .config import Settings
from .jvlink import JVLinkError, JVLinkUnavailable
from .records import PARSERS, parse_ra, record_spec

log = logging.getLogger(__name__)

# 速報オッズ（最新値）
REALTIME_SPECS = (("0B31", "O1"), ("0B32", "O2"))
# 時系列オッズ（発表時刻ごとの履歴）
TIMESERIES_SPECS = (("0B41", "O1"), ("0B42", "O2"))


@dataclass
class CycleResult:
    status: str = "OK"
    races_targeted: int = 0
    races_ok: int = 0
    rows_inserted: int = 0
    errors: list[str] = field(default_factory=list)


def race_window(race: sqlite3.Row, st: Settings) -> tuple[datetime, datetime]:
    """レースの取得対象時間帯 [開始, 終了) を返す。"""
    d = date.fromisoformat(race["race_date"])
    start = datetime.combine(d, st.start_time)
    if race["post_time"]:
        hh, mm = map(int, race["post_time"].split(":"))
        end = datetime.combine(d, dtime(hh, mm)) - timedelta(minutes=st.stop_before_post_minutes)
    else:
        end = datetime.combine(d, st.fallback_end_time)
    return start, end


def is_target(race: sqlite3.Row, now: datetime, st: Settings) -> bool:
    start, end = race_window(race, st)
    return start <= now < end


class OddsCollector:
    def __init__(self, client, conn: sqlite3.Connection, settings: Settings):
        self.client = client
        self.conn = conn
        self.st = settings
        self.consecutive_errors = 0

    # ---- JV-Link 呼び出しの共通リトライ --------------------------------
    def _call(self, desc: str, func, *args, **kwargs):
        """
        JV-Link 呼び出しを retry_count 回までその場で再試行する。
        再試行の前に JV-Link をリセットして再初期化（切断・状態不整合への対処）。
        最終的に失敗した場合は例外を送出する（呼び出し側で記録して次回へ回す）。
        """
        attempts = self.st.retry_count + 1
        for i in range(1, attempts + 1):
            try:
                result = func(*args, **kwargs)
                self.consecutive_errors = 0
                return result
            except JVLinkError as e:
                if not e.retryable:
                    log.error("%s: 再試行不可のエラー: %s", desc, e)
                    raise
                err = e
            except JVLinkUnavailable:
                raise
            except Exception as e:  # pywintypes.com_error（RPC 切断など）を含む
                err = e
            self.consecutive_errors += 1
            log.warning("%s: 失敗 (%d/%d): %s", desc, i, attempts, err)
            if self.consecutive_errors >= self.st.reinit_after_errors:
                self.client.reset()
            if i < attempts:
                time.sleep(self.st.retry_wait_seconds)
        raise err  # type: ignore[misc]

    # ---- レース一覧 ---------------------------------------------------
    def refresh_races(self, target_dates: Optional[list[date]] = None) -> int:
        """
        今週データ (JVOpen dataspec=RACE, option=2) から RA レコードを取り出して登録する。
        失敗した場合は速報レース情報 (JVRTOpen 0B15, キー=開催日) で日別に取得する。
        target_dates 未指定時は「今日以降」のレースをすべて登録する（=今週末分）。
        """
        today = date.today()
        fromtime = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d000000")
        races = []
        source = "RACE"
        try:
            recs = self._call("JVOpen(RACE 今週データ)", self.client.stored_records,
                              "RACE", fromtime, 2, {"RA"})
            races = [parse_ra(r) for r in recs]
        except JVLinkError as e:
            if not e.retryable:
                raise
            log.warning("今週データの取得に失敗。0B15 で日別取得を試みます: %s", e)

        if not races:
            source = "0B15"
            for d in target_dates or self._upcoming_weekend(today):
                try:
                    recs = self._call(f"JVRTOpen(0B15,{d:%Y%m%d})", self.client.rt_records,
                                      "0B15", d.strftime("%Y%m%d"))
                except JVLinkError as e:
                    if not e.retryable:
                        raise
                    log.warning("0B15 %s の取得に失敗: %s", d, e)
                    continue
                races += [parse_ra(r) for r in recs if record_spec(r) == "RA"]

        # データ区分 0 (削除) と中止を除き、対象日のレースだけ残す。同一レースは最後のものを採用
        uniq = {}
        for r in races:
            if r.data_kubun == "0":
                continue
            rd = date.fromisoformat(r.race_date)
            if target_dates is not None and rd not in target_dates:
                continue
            if target_dates is None and rd < today:
                continue
            uniq[r.race_id] = r
        n = db.upsert_races(self.conn, uniq.values(), source)
        log.info("レース情報を登録: %d レース (source=%s)", n, source)
        return n

    @staticmethod
    def _upcoming_weekend(today: date) -> list[date]:
        """今日から次の月曜日（祝日開催対策）までの日付。"""
        days = (0 - today.weekday()) % 7 or 7  # 次の月曜まで
        return [today + timedelta(days=i) for i in range(days + 1)]

    # ---- オッズ取得 ---------------------------------------------------
    def _fetch_and_save(self, race_id: str, specs) -> int:
        inserted = 0
        for spec, rtype in specs:
            fetched_at = db.now_str()
            recs = self._call(f"JVRTOpen({spec},{race_id})", self.client.rt_records, spec, race_id)
            for raw in recs:
                if record_spec(raw) != rtype:
                    continue
                rec = PARSERS[rtype](raw)
                if rec.race_id != race_id:
                    continue
                inserted += db.save_odds_record(self.conn, rec, spec, fetched_at)
        return inserted

    def collect_race(self, race_id: str) -> int:
        return self._fetch_and_save(race_id, REALTIME_SPECS)

    def backfill_race(self, race_id: str) -> int:
        return self._fetch_and_save(race_id, TIMESERIES_SPECS)

    # ---- 1 サイクル ---------------------------------------------------
    def run_cycle(self, now: Optional[datetime] = None, force_all: bool = False,
                  race_ids: Optional[set[str]] = None) -> CycleResult:
        """
        取得対象のレースを 1 回ずつ取得する。JV-Link 由来のエラーは外へ出さない。
        race_ids を指定した場合はその中で取得対象のレースだけを取得する（締切直前の追加取得用）。
        """
        now = now or datetime.now()
        res = CycleResult()
        try:
            races = db.races_on(self.conn, now.date().isoformat())
            if not races:
                self.refresh_races([now.date()])
                races = db.races_on(self.conn, now.date().isoformat())
            targets = [r for r in races
                       if (force_all or is_target(r, now, self.st))
                       and (race_ids is None or r["race_id"] in race_ids)]
        except JVLinkUnavailable:
            raise
        except Exception as e:
            log.exception("レース一覧の取得に失敗（次回再試行）")
            res.status = "ERROR"
            res.errors.append(f"race list: {e}")
            run_id = db.start_run(self.conn, 0)
            db.finish_run(self.conn, run_id, res.status, 0, 0, "; ".join(res.errors))
            return res

        res.races_targeted = len(targets)
        if not targets:
            res.status = "SKIPPED"
            log.info("取得対象レースなし (%s)", now.strftime("%H:%M"))
            return res

        run_id = db.start_run(self.conn, len(targets))
        for race in targets:
            rid = race["race_id"]
            try:
                n = self.collect_race(rid)
                res.races_ok += 1
                res.rows_inserted += n
                log.info("取得 %s %s %s 発走%s: 差分 %d 行", rid, race["jyo_cd"],
                         f"{race['race_num']}R", race["post_time"] or "--:--", n)
            except JVLinkUnavailable:
                db.finish_run(self.conn, run_id, "ERROR", res.races_ok, res.rows_inserted,
                              "JV-Link を利用できません")
                raise
            except Exception as e:
                log.error("レース %s のオッズ取得に失敗（次回再試行）: %s", rid, e)
                res.errors.append(f"{rid}: {e}")

        if res.errors:
            res.status = "PARTIAL" if res.races_ok else "ERROR"
        db.finish_run(self.conn, run_id, res.status, res.races_ok, res.rows_inserted,
                      "; ".join(res.errors) or None)
        log.info("サイクル完了: status=%s 対象=%d 成功=%d 差分行=%d",
                 res.status, res.races_targeted, res.races_ok, res.rows_inserted)
        return res
