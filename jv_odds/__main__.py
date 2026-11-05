"""
JV-Link 時系列オッズ収集システム CLI

  python -m jv_odds check-env            必要ライブラリ・JV-Link の確認
  python -m jv_odds init-db              SQLite に DDL を適用
  python -m jv_odds races                今週末の開催レースを取得して登録
  python -m jv_odds run                  当日ループ（朝～最終レース発走まで 10 分間隔で取得）
  python -m jv_odds collect              1 回だけ取得して終了（タスクスケジューラ用）
  python -m jv_odds backfill [--date D]  時系列オッズ (0B41/0B42) で過去分を補完
  python -m jv_odds status [--date D]    蓄積状況の表示

  共通オプション: --config PATH / --mock（JV-Link 無しで模擬データ動作）/ -v
"""
from __future__ import annotations

import argparse
import logging
import platform
import struct
import sys
from datetime import date, datetime

from . import db
from .collector import OddsCollector
from .config import load_settings
from .jvlink import JVLinkClient, JVLinkError, JVLinkUnavailable
from .logging_setup import setup_logging
from .scheduler import run_day

log = logging.getLogger("jv_odds")


def make_client(args, st):
    if args.mock:
        from .mock_jvlink import MockJVLinkClient
        log.info("モック JV-Link を使用します")
        return MockJVLinkClient(fail_rate=args.mock_fail_rate)
    return JVLinkClient(st.sid, st.progid, st.read_buffer, st.download_timeout)


def cmd_check_env(args, st) -> int:
    ok = True
    bits = struct.calcsize("P") * 8
    print(f"Python      : {sys.version.split()[0]} ({bits}bit) / {platform.system()} {platform.release()}")
    if platform.system() != "Windows":
        print("  [NG] JV-Link は Windows 専用です（--mock で動作確認のみ可能）")
        ok = False
    if bits != 32:
        print("  [WARN] JV-Link は 32bit COM です。32bit 版 Python を推奨します")
    import sqlite3
    print(f"sqlite3     : {sqlite3.sqlite_version} [OK]")
    try:
        import win32com.client  # type: ignore  # noqa: F401
        import pythoncom  # type: ignore  # noqa: F401
        print("pywin32     : [OK]")
    except ImportError:
        print("pywin32     : [NG] `pip install -r jv_odds/requirements.txt` を実行してください")
        ok = False
    if ok:
        client = JVLinkClient(st.sid, st.progid, st.read_buffer, st.download_timeout)
        try:
            client.connect()
            print(f"JV-Link     : {st.progid} JVInit 成功 [OK]")
        except (JVLinkUnavailable, JVLinkError) as e:
            print(f"JV-Link     : [NG] {e}")
            ok = False
        finally:
            client.reset()
    print(f"DB          : {st.db_path}")
    print(f"ログ        : {st.log_dir}")
    print("結果        :", "OK" if ok else "NG")
    return 0 if ok else 1


def cmd_init_db(args, st) -> int:
    conn = db.connect(st.db_path)
    db.init_db(conn)
    log.info("DB を初期化しました: %s", st.db_path)
    return 0


def _collector(args, st) -> OddsCollector:
    conn = db.connect(st.db_path)
    db.init_db(conn)
    return OddsCollector(make_client(args, st), conn, st)


def cmd_races(args, st) -> int:
    c = _collector(args, st)
    dates = [date.fromisoformat(args.date)] if args.date else None
    try:
        c.refresh_races(dates)
    except (JVLinkError, JVLinkUnavailable) as e:
        log.error("レース情報の取得に失敗しました: %s", e)
        return 1
    rows = c.conn.execute(
        "SELECT race_date, COUNT(*) n, MIN(post_time) first, MAX(post_time) last "
        "FROM races WHERE race_date >= ? GROUP BY race_date ORDER BY race_date",
        (date.today().isoformat(),)).fetchall()
    for r in rows:
        print(f"{r['race_date']}: {r['n']} レース  発走 {r['first']} ～ {r['last']}")
    return 0


def cmd_collect(args, st) -> int:
    c = _collector(args, st)
    try:
        res = c.run_cycle(force_all=args.all)
    except JVLinkUnavailable as e:
        log.error("%s", e)
        return 1
    finally:
        c.client.reset()
    # タスクスケジューラで「失敗」扱いにすると再試行設定が効くよう、ERROR は終了コード 2
    return 2 if res.status == "ERROR" else 0


def cmd_run(args, st) -> int:
    c = _collector(args, st)
    try:
        run_day(c, st)
    except KeyboardInterrupt:
        log.info("中断されました")
    finally:
        c.client.reset()
    return 0


def cmd_backfill(args, st) -> int:
    c = _collector(args, st)
    d = args.date or date.today().isoformat()
    races = db.races_on(c.conn, d)
    if not races:
        c.refresh_races([date.fromisoformat(d)])
        races = db.races_on(c.conn, d)
    total = 0
    for r in races:
        try:
            n = c.backfill_race(r["race_id"])
            total += n
            log.info("時系列補完 %s: %d 行", r["race_id"], n)
        except Exception as e:
            log.error("時系列補完失敗 %s: %s", r["race_id"], e)
    c.client.reset()
    log.info("時系列補完 完了: %d 行", total)
    return 0


def cmd_status(args, st) -> int:
    conn = db.connect(st.db_path)
    db.init_db(conn)
    d = args.date or date.today().isoformat()
    print(f"== {d} ==")
    for r in conn.execute(
        """SELECT r.race_id, r.jyo_cd, r.race_num, r.post_time, r.race_name,
                  (SELECT COUNT(*) FROM odds_snapshots s WHERE s.race_id=r.race_id) snaps,
                  (SELECT MAX(happyo_at) FROM odds_snapshots s WHERE s.race_id=r.race_id) last
           FROM races r WHERE race_date=? ORDER BY post_time, jyo_cd""", (d,)):
        print(f"{r['race_id']} 場{r['jyo_cd']} {r['race_num']:>2}R {r['post_time'] or '--:--'} "
              f"{r['race_name'][:16]:<16} snapshots={r['snaps']:>3} last={r['last'] or '-'}")
    print("-- 直近の実行 --")
    for r in conn.execute("SELECT * FROM fetch_runs ORDER BY run_id DESC LIMIT 10"):
        print(f"#{r['run_id']} {r['started_at']} {r['status']:<8} 対象{r['races_targeted']} "
              f"成功{r['races_ok']} 差分{r['rows_inserted']} {r['error_message'] or ''}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m jv_odds", description="JV-Link 時系列オッズ収集")
    p.add_argument("--config", help="設定ファイル (既定: jv_odds/config.ini)")
    p.add_argument("--mock", action="store_true", help="JV-Link の代わりにモックを使う")
    p.add_argument("--mock-fail-rate", type=float, default=0.0, help="モックのエラー発生率")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-env")
    sub.add_parser("init-db")
    sp = sub.add_parser("races"); sp.add_argument("--date")
    sp = sub.add_parser("collect"); sp.add_argument("--all", action="store_true",
                                                    help="時間帯に関係なく当日全レースを取得")
    sub.add_parser("run")
    sp = sub.add_parser("backfill"); sp.add_argument("--date")
    sp = sub.add_parser("status"); sp.add_argument("--date")
    args = p.parse_args(argv)

    st = load_settings(args.config)
    setup_logging(st.log_dir, args.verbose)
    handlers = {
        "check-env": cmd_check_env, "init-db": cmd_init_db, "races": cmd_races,
        "collect": cmd_collect, "run": cmd_run, "backfill": cmd_backfill, "status": cmd_status,
    }
    try:
        return handlers[args.cmd](args, st)
    except Exception:
        log.exception("予期しないエラーで終了します (%s)", datetime.now())
        return 1


if __name__ == "__main__":
    sys.exit(main())
