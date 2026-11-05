-- =====================================================================
-- JV-Link 時系列オッズ蓄積用 DDL (SQLite)
--
--  races               : 収集対象レース（開催日・発走時刻など）
--  odds_snapshots      : JV-Link から受信したオッズレコード 1 件 = 1 行
--                        (レース × 賭式 × 発表時刻 で一意。重複受信は無視)
--  odds_win_place      : 単勝・複勝オッズ（馬番単位、前回値から変化した馬のみ保存 = 差分）
--  odds_quinella       : 馬連オッズ（組番単位、前回値から変化した組のみ保存 = 差分）
--  fetch_runs          : 収集処理 1 回ごとの実行ログ（成否・件数・エラー内容）
--
--  v_latest_win_place  : 各馬の最新 単勝・複勝オッズ
--  v_latest_quinella   : 各組の最新 馬連オッズ
--
--  日時はすべて ISO8601 文字列 (YYYY-MM-DD HH:MM:SS, JST) で保持する。
-- =====================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- レース情報
--   race_id = 開催年(4)+月日(4)+場コード(2)+回次(2)+日次(2)+R番号(2) の 16 桁
--             (JV-Link の JVRTOpen に渡すキー形式と同一)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS races (
    race_id        TEXT PRIMARY KEY CHECK (length(race_id) = 16),
    race_date      TEXT NOT NULL,              -- YYYY-MM-DD
    jyo_cd         TEXT NOT NULL,              -- 競馬場コード (01=札幌 … 10=小倉)
    kaiji          INTEGER NOT NULL,           -- 開催回
    nichiji        INTEGER NOT NULL,           -- 開催日目
    race_num       INTEGER NOT NULL,           -- レース番号
    race_name      TEXT,                       -- 競走名本題
    post_time      TEXT,                       -- 発走時刻 HH:MM (不明時 NULL)
    toroku_tosu    INTEGER,                    -- 登録頭数
    syusso_tosu    INTEGER,                    -- 出走頭数
    source         TEXT,                       -- 取得元 dataspec (RACE / 0B15 など)
    updated_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_races_date ON races (race_date, post_time);

-- ---------------------------------------------------------------------
-- 受信レコード（スナップショット）ヘッダ
--   record_type : 'O1' = 単勝・複勝・枠連, 'O2' = 馬連
--   happyo_at   : JV-Data の「発表月日時分」から復元した発表日時
--                 （仕様上、発表月日時分は中間オッズ(データ区分1)のみ設定。未設定の最終・確定
--                   オッズは取得時刻を入れ happyo_estimated = 1 とする）
--   data_kubun  : 1=中間 2=前日売最終 3=最終 4=確定 5=確定(月曜) 9=レース中止 0=削除
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS odds_snapshots (
    snapshot_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id        TEXT NOT NULL,
    record_type    TEXT NOT NULL CHECK (record_type IN ('O1', 'O2')),
    data_spec      TEXT NOT NULL,              -- 0B31 / 0B32 / 0B41 / 0B42 など
    data_kubun     TEXT,
    happyo_at      TEXT NOT NULL,              -- 発表日時
    happyo_estimated INTEGER NOT NULL DEFAULT 0, -- 1 = 発表時刻が未設定で取得時刻で代用
    fetched_at     TEXT NOT NULL,              -- 取得日時 (このシステムが受信した時刻)
    toroku_tosu    INTEGER,
    syusso_tosu    INTEGER,
    sale_flags     TEXT,                       -- 発売フラグ (O1: 単勝/複勝/枠連, O2: 馬連)
    total_votes    TEXT,                       -- 票数合計 (O1: 単/複/枠 をカンマ区切り)
    changed_rows   INTEGER NOT NULL DEFAULT 0, -- このスナップショットで保存した差分行数
    UNIQUE (race_id, record_type, happyo_at)
);
CREATE INDEX IF NOT EXISTS idx_snap_race ON odds_snapshots (race_id, record_type, happyo_at);

-- ---------------------------------------------------------------------
-- 単勝・複勝オッズ（差分保存）
--   odds_status : NULL=正常, 'NO_VOTE'=無投票, 'CANCEL_BEFORE'=発売前取消,
--                 'CANCEL_AFTER'=発売後取消, 'NONE'=登録なし
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS odds_win_place (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id    INTEGER NOT NULL REFERENCES odds_snapshots (snapshot_id) ON DELETE CASCADE,
    race_id        TEXT NOT NULL,
    umaban         INTEGER NOT NULL,           -- 馬番
    happyo_at      TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,              -- 取得日時
    win_odds       REAL,                       -- 単勝オッズ
    win_ninki      INTEGER,                    -- 単勝人気
    place_odds_min REAL,                       -- 複勝オッズ(最低)
    place_odds_max REAL,                       -- 複勝オッズ(最高)
    place_ninki    INTEGER,                    -- 複勝人気
    odds_status    TEXT,
    UNIQUE (race_id, umaban, happyo_at)
);
CREATE INDEX IF NOT EXISTS idx_wp_race_time ON odds_win_place (race_id, umaban, happyo_at);

-- ---------------------------------------------------------------------
-- 馬連オッズ（差分保存）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS odds_quinella (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id    INTEGER NOT NULL REFERENCES odds_snapshots (snapshot_id) ON DELETE CASCADE,
    race_id        TEXT NOT NULL,
    umaban1        INTEGER NOT NULL,
    umaban2        INTEGER NOT NULL,
    happyo_at      TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    odds           REAL,                       -- 馬連オッズ
    ninki          INTEGER,                    -- 人気
    odds_status    TEXT,
    CHECK (umaban1 < umaban2),
    UNIQUE (race_id, umaban1, umaban2, happyo_at)
);
CREATE INDEX IF NOT EXISTS idx_q_race_time ON odds_quinella (race_id, umaban1, umaban2, happyo_at);

-- ---------------------------------------------------------------------
-- 収集処理の実行ログ
--   status : RUNNING / OK / PARTIAL / ERROR / SKIPPED
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fetch_runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'RUNNING',
    races_targeted INTEGER NOT NULL DEFAULT 0,
    races_ok       INTEGER NOT NULL DEFAULT 0,
    rows_inserted  INTEGER NOT NULL DEFAULT 0,
    error_message  TEXT
);

-- ---------------------------------------------------------------------
-- 最新オッズビュー
-- ---------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_latest_win_place AS
SELECT w.*
FROM odds_win_place AS w
WHERE w.happyo_at = (
    SELECT MAX(w2.happyo_at) FROM odds_win_place AS w2
    WHERE w2.race_id = w.race_id AND w2.umaban = w.umaban
);

CREATE VIEW IF NOT EXISTS v_latest_quinella AS
SELECT q.*
FROM odds_quinella AS q
WHERE q.happyo_at = (
    SELECT MAX(q2.happyo_at) FROM odds_quinella AS q2
    WHERE q2.race_id = q.race_id AND q2.umaban1 = q.umaban1 AND q2.umaban2 = q.umaban2
);
