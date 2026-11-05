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

rem 環境チェック（Python bit 数・pywin32・COM 登録・JVInit・JVRTOpen のエラー処理）
py -3-32 -m jv_odds check-env
rem 実在レースで 0B31/0B32 の取得・解析まで確認（発売中のレースキーを指定）
py -3-32 -m jv_odds check-env --race-key 2026110805050411
```

`check-env` が確認する内容:

| 項目 | 内容 | 期待結果 |
|---|---|---|
| Python / pywin32 | bit 数・OS・pywin32 の有無 | 32bit / Windows |
| COM 登録 | ProgID → CLSID → InprocServer32（32bit/64bit ビュー） | 実行中の Python と同じ bit 数で登録あり |
| JVInit | sid で初期化 | 0 |
| JVRTOpen 不正キー | `0B31` + `0000` | -114 を捕捉 |
| JVRTOpen 不正 dataspec | `0B99` | -111 を捕捉 |
| JVRTOpen キー形式 16桁/12桁 | 提供期間外のダミーレース | -1 (該当なし) = キー形式は受理 |
| 連続呼び出し | 同じ呼び出しを 2 回 | -202 にならない (JVClose 済み) |
| 利用キー認証 | -301/-302/-303 の検出 | 発生しない |
| 実取得 (`--race-key`) | 0B31/0B32 を取得・解析 | O1=962 / O2=2042 byte、明細あり |

`--race-key` を省略した場合、DB に当日のレースがあればその最初のレースで実取得まで確認します。

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

- 速報オッズのキーは **レース単位** `YYYYMMDDJJKKHHRR`（16桁）または `YYYYMMDDJJRR`（12桁）。
  `0B11`(速報馬体重) / `0B12`(速報レース情報・成績確定後) はオッズではありません。
- `JVRTOpen` 前に `jvlink.validate_rt_key()` で dataspec とキー形式を検査し、不正なら JV-Link を呼ばずに -114 / -111 とします。
- レコードは **JVGets** で Shift-JIS の bytes のまま受け取り、JV-Data 仕様の固定長（バイト位置）で `records.py` が解析します。
- `JVRTOpen`/`JVOpen` が -1（該当なし）を返しても必ず `JVClose` します（呼ばないと次回が -202 になる）。

### JV-Data 仕様書 4.9.0.1 との照合結果

公式 SDK 同梱の `JV-Data仕様書_4.9.0.1.xlsx` から機械生成されたレイアウト定義と照合しました
（位置は 1 始まり。`tests/test_spec_compliance.py` で固定）。

| レコード | 項目 | 位置 | バイト | 本実装 |
|---|---|---|---|---|
| RA (1272) | 競走名本題 | 33 | 60 | 一致 |
| RA | 発走時刻 (hhmm) | 874 | 4 | 一致 |
| RA | 登録頭数 / 出走頭数 | 882 / 884 | 2 / 2 | 一致 |
| O1 (962) | 発表月日時分 (MMDDhhmm) | 28 | 8 | 一致 |
| O1 | 発売フラグ 単/複/枠 | 40 | 3 | 一致 |
| O1 | 単勝 (馬番2+オッズ4+人気2)×28 | 44 | 8 | 一致 |
| O1 | 複勝 (馬番2+最低4+最高4+人気2)×28 | 268 | 12 | 一致 |
| O1 | 枠連 (組番2+オッズ5+人気2)×36 | 604 | 9 | 一致（保存対象外） |
| O2 (2042) | 馬連 (組番4+オッズ6+人気3)×153 | 41 | 13 | 一致 |

賭式とレコードの対応は **O1=単勝・複勝・枠連 / O2=馬連 / O3=ワイド / O4=馬単 / O5=3連複 / O6=3連単** です。

仕様書の注記に合わせて次の点を扱っています。

- **発表月日時分は中間オッズ（データ区分 1）のみ設定**。最終・確定オッズは取得時刻を `happyo_at` とし `happyo_estimated = 1`。
- 発表月日時分は年を持たないため、1 月開催で 12 月の発表は前年扱い。
- 地方 (A)・海外 (B)・削除 (0) の RA、JRA 以外の場コードは収集対象外。
- 仕様外の長さのレコードは警告ログを出して読み飛ばし。

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

### 再試行（エクスポネンシャルバックオフ）

`retry.call_with_retry` で、JV-Link の呼び出しごとに次のように再試行します。

| 試行 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| 失敗後の待ち | 2 秒 | 4 秒 | 8 秒 | 16 秒 | （終了） |

- 待ち時間は `min(retry_wait_seconds × 2^(n-1), retry_max_wait_seconds)` に ±20% の揺らぎを加えたもの。`-504`（メンテナンス中）は上限（30 秒）まで待ちます。
- 再試行の対象は、`-503`（ファイルが見つからない）、`-502`（ダウンロード失敗）、`-4xx`（サーバー・内部エラー）、`-201/-202/-203`（状態不整合）、`pywintypes.com_error`（RPC 切断など）です。
- `-114`（キー不正）、`-301〜-303`（認証・利用キー）などの設定エラーは再試行せず、すぐにエラーログへ残します。
- **サーキットブレーカ**: 1 サイクルの中で 1 レースが再試行を使い切ると、残りのレースは 1 回ずつだけ試します（障害時に 1 サイクルが 10 分を超えないようにするため）。どこかで成功すれば元に戻り、次のサイクルは通常の再試行から始めます。
- それでも失敗したレースは `fetch_runs.error_message` に段階とコードを記録し、**次の 10 分後に再試行**します。プロセスは終了しません。

### JVClose とリセット

- `JVRTOpen`/`JVOpen` の戻り値が -1 でも、例外が起きても、必ず `JVClose` します（`finally`）。
- 失敗のたびに `reset()` を呼び、**JVCancel（ダウンロード中のとき）→ JVClose → COM オブジェクト解放 → CoUninitialize** まで行います。次回は CoInitialize → Dispatch → JVInit からやり直します。
- `JVGets` が `-3`（ダウンロード中）を返し続ける場合は、`download_timeout` 秒で打ち切ります（無限待ちの防止）。
- `-402/-403`（破損ファイル）は `JVFiledelete` してから再試行します。

### ログ

| ファイル | レベル | 保存 | 内容 |
|---|---|---|---|
| `logs/jv_odds.log` | INFO 以上 | 30 日 | 取得結果・サイクル概要 |
| `logs/jv_odds_error.log` | WARNING 以上 | 90 日 | 失敗・再試行 |
| `logs/jv_odds_debug.log` | DEBUG 以上 | 14 日 | JV-Link 呼び出しの詳細（`debug_log = false` で無効） |

エラーは **どの段階で何のコードが返ったか** を次の形式で出します。

```
JVGets ループ内でエラー: stage=JVGets code=-503(ファイルが見つからない) dataspec=0B31 key=2026110805050411 records_read=0 file=…
JVRTOpen(0B31,2026110805050411): 失敗 (2/5, 0.3s): stage=JVGets code=-503(…) → 4.1 秒後に再試行
```

debug ログには、`JVInit / JVRTOpen / JVOpen / JVStatus / JVGets(EOF・ファイル切替・-3 待機) / JVClose / JVCancel` の戻り値と所要時間（ms）を記録します。COM 例外は HRESULT（例: `0x800706BA` = RPC サーバー利用不可）付きで出します。

`check-env --race-key` の実取得も同じ再試行で行い、再試行が起きた場合は各試行の段階・コード・待ち時間を表示します。

## 6. テスト

```bash
python -m unittest discover -s jv_odds/tests -t .
```

レコード解析・仕様書との位置照合・差分保存・キー検査・JVClose/JVGets の扱い・環境チェック・
バックオフ・JVClose/リセット・段階別エラーログ・サーキットブレーカ・
リトライ・スケジューラ（仮想時計）をモック / 偽 COM で検証します（55 件）。
