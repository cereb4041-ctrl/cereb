# JV-Link 時系列オッズ収集システム (`jv_odds`)

JRA-VAN Data Lab. の **JV-Link** から、開催レースの **単勝・複勝・馬連オッズ** を
レース当日の朝から締切直前まで **10 分間隔** で取得し、**SQLite** に **差分** として蓄積します。

## 1. 必要環境・ライブラリ

| 項目 | 内容 |
|---|---|
| OS | Windows 10/11（JV-Link が Windows 専用 COM のため） |
| Python | 3.10 以上 **32bit 版**（JV-Link `JVDTLab.JVLink` は 32bit COM。64bit 版 Python からは生成できません） |
| JV-Link | JRA-VAN からインストール済み・利用キー設定済み |
| 外部ライブラリ | `pywin32`（COM 呼び出し）のみ。他は標準ライブラリ（`sqlite3` / `logging` / `configparser`） |

```bat
rem 32bit Python で依存ライブラリを導入（リポジトリのルートで実行）
py -3-32 -m pip install -r jv_odds\requirements.txt

rem 環境チェック（Python bit 数・pywin32・JVInit の成否）
py -3-32 -m jv_odds check-env
```

## 2. データベース設計

DDL は [`schema.sql`](schema.sql)。`python -m jv_odds init-db` で `data/jv_odds.db` に適用されます（他コマンドも起動時に自動適用）。

| テーブル | 主な列 | 説明 |
|---|---|---|
| `races` | race_id(16桁), race_date, jyo_cd, race_num, race_name, post_time | 収集対象レース |
| `odds_snapshots` | race_id, record_type(O1/O2), data_spec, happyo_at, fetched_at, data_kubun | 受信したオッズレコード。**(レース, 賭式, 発表時刻) で一意** なので同じ発表を再受信しても重複しない |
| `odds_win_place` | race_id, umaban, happyo_at, fetched_at, win_odds, win_ninki, place_odds_min/max, place_ninki, odds_status | 単勝・複勝。**前回値から変わった馬だけ** を保存 |
| `odds_quinella` | race_id, umaban1, umaban2, happyo_at, fetched_at, odds, ninki, odds_status | 馬連。**前回値から変わった組だけ** を保存 |
| `fetch_runs` | started_at, finished_at, status, races_targeted, races_ok, rows_inserted, error_message | 実行ログ（OK / PARTIAL / ERROR） |
| `v_latest_win_place` / `v_latest_quinella` | | 最新オッズのビュー |

- `race_id` = 年(4)+月日(4)+場コード(2)+回次(2)+日次(2)+R(2)。JVRTOpen のキーと同じ形式。
- `happyo_at` は JRA の発表時刻、`fetched_at` は本システムの取得時刻。
- `odds_status`: `NO_VOTE`(無投票) / `CANCEL_BEFORE`(発売前取消) / `CANCEL_AFTER`(発売後取消)。

ある時刻 T 時点の全馬のオッズは「各馬の `happyo_at <= T` で最大の行」で復元できます。

```sql
SELECT w.* FROM odds_win_place w
WHERE w.race_id = '2026110805050411'
  AND w.happyo_at = (SELECT MAX(happyo_at) FROM odds_win_place
                     WHERE race_id = w.race_id AND umaban = w.umaban
                       AND happyo_at <= '2026-11-08 15:00:00');
```

## 3. 使用する JV-Link データ

| 用途 | API | dataspec / キー | レコード |
|---|---|---|---|
| 今週の開催レース | `JVOpen` (option=2 今週データ) | `RACE` | RA |
| 同上（失敗時の代替） | `JVRTOpen` | `0B15` / `YYYYMMDD` | RA |
| 最新 単勝・複勝 | `JVRTOpen` | `0B31` / race_id | O1 |
| 最新 馬連 | `JVRTOpen` | `0B32` / race_id | O2 |
| 時系列の補完 (`backfill`) | `JVRTOpen` | `0B41` / `0B42` / race_id | O1 / O2 |

レコードは JV-Data 仕様の固定長（Shift-JIS バイト位置）で `records.py` が解析します。
RA の発走時刻などの位置は `records.RA_POS` にまとめてあるので、仕様書の版が変わった場合はここを調整してください。

## 4. 使い方

```bat
py -3-32 -m jv_odds races              rem 今週末の開催レースを登録・一覧表示
py -3-32 -m jv_odds run                rem 当日ループ（08:30～最終レース発走まで）
py -3-32 -m jv_odds collect            rem 1 回だけ取得（タスクスケジューラ用）
py -3-32 -m jv_odds collect --all      rem 時間帯に関係なく当日全レースを取得
py -3-32 -m jv_odds backfill --date 2026-11-08   rem 時系列オッズで取りこぼしを補完
py -3-32 -m jv_odds status             rem 蓄積状況と直近の実行結果
```

JV-Link が無い環境では `--mock` で模擬データによる動作確認ができます（例: `python -m jv_odds --mock collect --all`）。

### 取得タイミング（`run`）

1. 起動時に当日のレース情報を取得（以後 60 分ごとに再取得して発走時刻変更を反映）
2. `start_time`(08:30) まで待機
3. 壁時計の 10 分刻み（08:30, 08:40, …）で、**発走前のレースすべて** の最新オッズを取得
4. 各レースの **発走 3 分前** にも追加で 1 回取得（締切直前オッズ）
5. 当日の全レースが発走したら終了。開催の無い日は即終了

設定は `config.ini.example` を `config.ini` にコピーして変更します。

### 自動実行（タスクスケジューラ）

```powershell
# 方式 A: 毎日 08:20 に run を起動（推奨。開催が無い日は即終了するので祝日開催にも対応）
powershell -ExecutionPolicy Bypass -File jv_odds\scripts\register_task.ps1
# 方式 B: 08:30～17:00 に 10 分おきに collect を起動
powershell -ExecutionPolicy Bypass -File jv_odds\scripts\register_task.ps1 -Mode Interval
```

32bit Python のパスが `py -3-32` 以外の場合は環境変数 `PYTHON_EXE` を設定してください。
JV-Link はログオン中ユーザーのセッションで動かす必要があります。

## 5. エラーハンドリングとログ

- JV-Link の呼び出しはその場で `retry_count` 回（既定 2 回、15 秒間隔）再試行。再試行前に COM を破棄して **JVInit からやり直す**（切断・`-202` 等の状態不整合に対応）。
- それでも失敗したレースは `fetch_runs` に記録して **次の 10 分後に再試行**。プロセスは終了しません。
- 利用キー期限切れ (`-302`) 等の設定エラーは再試行せずエラーログに残します。
- ログ: `logs/jv_odds.log`（日次ローテーション 30 日）、警告以上は `logs/jv_odds_error.log`（90 日）。

## 6. テスト

```bash
python -m unittest discover -s jv_odds/tests -t .
```

レコード解析・差分保存・リトライ・スケジューラ（仮想時計）をモックで検証します。
