"""SQLite への保存処理（DDL 適用・差分保存・実行ログ）。"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .records import OddsRecord, RaceInfo

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: Path | str) -> sqlite3.Connection:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(db_path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")  # 収集中も別プロセスから参照可能にする
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """既存 DB に後から追加した列を足す（CREATE TABLE IF NOT EXISTS では追加されないため）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(odds_snapshots)")}
    if "happyo_estimated" not in cols:
        conn.execute("ALTER TABLE odds_snapshots ADD COLUMN happyo_estimated INTEGER NOT NULL DEFAULT 0")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------
# races
# ---------------------------------------------------------------------
def upsert_races(conn: sqlite3.Connection, races: Iterable[RaceInfo], source: str) -> int:
    n = 0
    with transaction(conn):
        for r in races:
            conn.execute(
                """
                INSERT INTO races (race_id, race_date, jyo_cd, kaiji, nichiji, race_num,
                                   race_name, post_time, toroku_tosu, syusso_tosu, source, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(race_id) DO UPDATE SET
                    race_name   = COALESCE(NULLIF(excluded.race_name, ''), races.race_name),
                    post_time   = COALESCE(excluded.post_time, races.post_time),
                    toroku_tosu = COALESCE(excluded.toroku_tosu, races.toroku_tosu),
                    syusso_tosu = COALESCE(excluded.syusso_tosu, races.syusso_tosu),
                    source      = excluded.source,
                    updated_at  = excluded.updated_at
                """,
                (r.race_id, r.race_date, r.jyo_cd, r.kaiji, r.nichiji, r.race_num,
                 r.race_name, r.post_time, r.toroku_tosu, r.syusso_tosu, source, now_str()),
            )
            n += 1
    return n


def races_on(conn: sqlite3.Connection, race_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM races WHERE race_date = ? ORDER BY post_time, jyo_cd, race_num",
        (race_date,),
    ).fetchall()


# ---------------------------------------------------------------------
# odds（差分保存）
# ---------------------------------------------------------------------
def _latest_win_place(conn, race_id: str, before: str) -> dict[int, tuple]:
    rows = conn.execute(
        """
        SELECT umaban, win_odds, place_odds_min, place_odds_max, odds_status
        FROM odds_win_place AS w
        WHERE race_id = ? AND happyo_at = (
            SELECT MAX(happyo_at) FROM odds_win_place
            WHERE race_id = w.race_id AND umaban = w.umaban AND happyo_at < ?)
        """,
        (race_id, before),
    ).fetchall()
    return {r["umaban"]: (r["win_odds"], r["place_odds_min"], r["place_odds_max"], r["odds_status"])
            for r in rows}


def _latest_quinella(conn, race_id: str, before: str) -> dict[tuple[int, int], tuple]:
    rows = conn.execute(
        """
        SELECT umaban1, umaban2, odds, odds_status
        FROM odds_quinella AS q
        WHERE race_id = ? AND happyo_at = (
            SELECT MAX(happyo_at) FROM odds_quinella
            WHERE race_id = q.race_id AND umaban1 = q.umaban1 AND umaban2 = q.umaban2
              AND happyo_at < ?)
        """,
        (race_id, before),
    ).fetchall()
    return {(r["umaban1"], r["umaban2"]): (r["odds"], r["odds_status"]) for r in rows}


def _changed_rows(conn, rec: OddsRecord, happyo_at: str) -> list[tuple]:
    """直前の値から変化した馬・組番の INSERT 用タプル（snapshot_id 抜き）を返す。"""
    out = []
    if rec.record_type == "O1":
        prev = _latest_win_place(conn, rec.race_id, happyo_at)
        for o in rec.win_place:
            if prev.get(o.umaban) == (o.win_odds, o.place_odds_min, o.place_odds_max, o.odds_status):
                continue
            out.append((o.umaban, o.win_odds, o.win_ninki, o.place_odds_min, o.place_odds_max,
                        o.place_ninki, o.odds_status))
    elif rec.record_type == "O2":
        prev_q = _latest_quinella(conn, rec.race_id, happyo_at)
        for q in rec.quinella:
            if prev_q.get((q.umaban1, q.umaban2)) == (q.odds, q.odds_status):
                continue
            out.append((q.umaban1, q.umaban2, q.odds, q.ninki, q.odds_status))
    return out


def save_odds_record(
    conn: sqlite3.Connection,
    rec: OddsRecord,
    data_spec: str,
    fetched_at: Optional[str] = None,
) -> int:
    """
    オッズレコード 1 件を保存し、保存した差分行数を返す。
      - 同じ (レース, 賭式, 発表時刻) を既に受信済みなら何もしない (0 を返す)
      - 直前の値から変化した馬・組番のみ明細テーブルへ INSERT する
      - 発表月日時分は仕様上「中間オッズ (データ区分 1) のみ設定」。最終・確定オッズなど
        未設定のレコードは取得時刻 (分単位) を happyo_at とし happyo_estimated=1 とする。
        直前のスナップショットと同じデータ区分で値も変わっていなければ保存しない。
      - データ区分 0（該当レコード削除）は保存しない
    """
    if rec.data_kubun == "0":
        log.info("削除レコード (データ区分0) をスキップ: race_id=%s type=%s", rec.race_id, rec.record_type)
        return 0
    fetched_at = fetched_at or now_str()
    estimated = rec.happyo_at is None
    happyo_at = rec.happyo_at or (fetched_at[:16] + ":00")

    with transaction(conn):
        changes = _changed_rows(conn, rec, happyo_at)
        if estimated and not changes:
            last = conn.execute(
                """SELECT data_kubun FROM odds_snapshots WHERE race_id=? AND record_type=?
                   ORDER BY happyo_at DESC LIMIT 1""", (rec.race_id, rec.record_type)).fetchone()
            if last and last["data_kubun"] == rec.data_kubun:
                return 0  # 同じ最終/確定オッズの再受信
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO odds_snapshots
                (race_id, record_type, data_spec, data_kubun, happyo_at, happyo_estimated,
                 fetched_at, toroku_tosu, syusso_tosu, sale_flags, total_votes, changed_rows)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (rec.race_id, rec.record_type, data_spec, rec.data_kubun, happyo_at, int(estimated),
             fetched_at, rec.toroku_tosu, rec.syusso_tosu, rec.sale_flags, rec.total_votes,
             len(changes)),
        )
        if cur.rowcount == 0:
            return 0  # 受信済みの発表時刻
        sid = cur.lastrowid
        if rec.record_type == "O1":
            conn.executemany(
                """INSERT OR IGNORE INTO odds_win_place
                       (snapshot_id, race_id, happyo_at, fetched_at, umaban, win_odds, win_ninki,
                        place_odds_min, place_odds_max, place_ninki, odds_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [(sid, rec.race_id, happyo_at, fetched_at, *c) for c in changes])
        else:
            conn.executemany(
                """INSERT OR IGNORE INTO odds_quinella
                       (snapshot_id, race_id, happyo_at, fetched_at, umaban1, umaban2,
                        odds, ninki, odds_status)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(sid, rec.race_id, happyo_at, fetched_at, *c) for c in changes])
    return len(changes)


def last_happyo_at(conn: sqlite3.Connection, race_id: str) -> Optional[str]:
    row = conn.execute("SELECT MAX(happyo_at) FROM odds_snapshots WHERE race_id = ?",
                       (race_id,)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------
# fetch_runs
# ---------------------------------------------------------------------
def start_run(conn: sqlite3.Connection, races_targeted: int) -> int:
    with transaction(conn):
        cur = conn.execute("INSERT INTO fetch_runs (started_at, races_targeted) VALUES (?, ?)",
                           (now_str(), races_targeted))
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, races_ok: int,
               rows_inserted: int, error_message: Optional[str] = None) -> None:
    with transaction(conn):
        conn.execute(
            """UPDATE fetch_runs SET finished_at=?, status=?, races_ok=?, rows_inserted=?,
                      error_message=? WHERE run_id=?""",
            (now_str(), status, races_ok, rows_inserted,
             (error_message or None) and error_message[:2000], run_id),
        )
