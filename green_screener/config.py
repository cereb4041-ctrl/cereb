"""
green_screener 専用の設定。

既存システムの config.py とは独立している。
ここの値を変えても既存システムの挙動には一切影響しない。
"""
from pathlib import Path

# ── ディレクトリ（プロジェクトルート基準） ──────────────
ROOT_DIR    = Path(__file__).resolve().parent.parent
LOGS_DIR    = ROOT_DIR / "logs"
RESULTS_DIR = ROOT_DIR / "results"
CACHE_DIR   = ROOT_DIR / "cache"

# ── ユニバースフィルタ ──────────────────────────────
MIN_VOLUME = 100_000      # 出来高10万株以上
MAX_PRICE  = 5_000        # 株価5000円以下
MIN_PRICE  = 100          # 極端な低位株を除外

# 東証プライム上場銘柄一覧（JPX公開Excel）
JPX_XLS_URL       = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
PRIME_MARKET_NAME = "プライム（内国株式）"

# ── 移動平均（週足・日足で共通の本数） ──────────────
MA_SHORT = 5
MA_MID   = 20
MA_LONG  = 60

# ── 週足条件 ────────────────────────────────────────
W_TOUCH_TOLERANCE = 0.02   # 安値が20週線 × (1+0.02) 以内でタッチ判定
W_TOUCH_LOOKBACK  = 52     # 直近52週を走査
W_TOUCH_GAP       = 4      # この週数以内の連続タッチは同一イベント
W_TOUCH_CUTOFF    = -8     # 最後のタッチが直近8週以内でないと無効
W_TOUCH_MIN       = 1      # 有効なタッチ回数の下限
W_TOUCH_MAX       = 2      # 有効なタッチ回数の上限
W_MA20_SLOPE_BARS = 5      # 20週線の傾きを何週前と比較するか

# ── 日足条件 ────────────────────────────────────────
D_PULLBACK_MIN      = 0.03   # 直近高値から3%以上押した
D_PULLBACK_MAX      = 0.08   # 8%を超える下落は押し目とみなさない
D_PULLBACK_LOOKBACK = 20     # 直近20日の高値と比較
D_MA60_SLOPE_BARS   = 5      # 60日線の傾きを何日前と比較するか

# ── スコアリング（8条件） ───────────────────────────
SCORE_MAX          = 8
SCORE_STRONG       = 6       # 6以上 → ★★★ 要注目
SCORE_WATCH        = 4       # 4-5   → ★★  経過観察
MIN_SCORE_TO_REPORT = 7      # これ未満は結果に含めない
                             # 4だと全体の6割が該当して絞り込みにならないため7に設定

# ── セクター分散 ────────────────────────────────────
# 同一業種の銘柄が上位を占めると分散が効かないため、
# スコア順に採用しつつ1業種あたりの採用数に上限を設ける。
SECTOR_DIVERSIFY   = True    # False で分散フィルタを無効化（スコア順のまま）
MAX_PER_SECTOR     = 3       # 1業種あたりの最大採用数
SECTOR_COLUMN      = "33業種区分"   # JPX Excel の業種カラム名
SECTOR_UNKNOWN     = "不明"          # 業種を取得できなかった場合の表示

# ── データ取得 ──────────────────────────────────────
WEEKLY_PERIOD = "2y"
DAILY_PERIOD  = "6mo"
MIN_WEEKLY_BARS = 65     # 60週線を引くのに必要
MIN_DAILY_BARS  = 65     # 60日線を引くのに必要

UNIVERSE_BATCH_SIZE = 100    # バルクDLのバッチサイズ（メモリ対策で小さめ）
BATCH_SIZE          = 50     # スクリーニングの進捗ログ間隔
BATCH_SLEEP         = 2.0    # バッチ間の待機（秒）
FETCH_SLEEP         = 0.3    # 銘柄ごとの待機（秒）
MAX_RETRIES         = 2
RETRY_SLEEP         = 5.0

# ── キャッシュ ──────────────────────────────────────
CACHE_ENABLED     = True
CACHE_TTL_HOURS   = 12       # この時間以内のキャッシュは再利用する

# ── 出力 ────────────────────────────────────────────
MAX_RESULTS   = 30           # 結果・通知に載せる最大件数
SAVE_CSV      = True
SAVE_JSON     = True

# ── 通知 ────────────────────────────────────────────
NOTIFY_ENABLED = True        # False ならコンソール出力のみ
LINE_PUSH_URL  = "https://api.line.me/v2/bot/message/push"
MAX_MESSAGE_LEN = 4000       # LINE APIの上限5000に対し余裕をもたせる
