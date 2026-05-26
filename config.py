# ユニバースフィルタ
MIN_VOLUME = 100_000      # 出来高10万株以上
MAX_PRICE  = 5_000        # 株価5000円以下

# 週足MA（単純移動平均）
MA_SHORT = 5
MA_MID   = 20
MA_LONG  = 60

# 20週線タッチ検出
TOUCH_TOLERANCE     = 0.02   # 安値が20週線 × (1 + 0.02) 以内ならタッチ判定
TOUCH_LOOKBACK      = 52     # 直近52週を走査
TOUCH_GAP_WEEKS     = 4      # この週数以内の連続タッチは同一イベント
RECENT_TOUCH_CUTOFF = -8     # 最後のタッチが直近8週以内でないと無効

# 日足押し目
PULLBACK_MIN      = 0.03     # 高値から3%以上押した（高値圏を除外）
PULLBACK_MAX      = 0.20     # 高値から20%超の下落は除外
PULLBACK_LOOKBACK = 20       # 直近20日の高値と比較

# yfinance レート制限対策
BATCH_SIZE    = 50
BATCH_SLEEP   = 2.0    # バッチ間待機（秒）
RETRY_SLEEP   = 5.0    # リトライ前待機（秒）
MAX_RETRIES   = 2

# JPX 上場銘柄一覧 Excel（プライム・スタンダード・グロース全銘柄）
JPX_XLS_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"

# 東証プライム市場区分の文字列（カラム '市場・商品区分' の値）
PRIME_MARKET_NAME = "プライム（内国株式）"

# スケジュール（毎週土曜 08:00 JST）
SCHEDULE_DAY  = "saturday"
SCHEDULE_TIME = "08:00"

# ─── 資金管理 ───────────────────────────────────────
CAPITAL       = 1_000_000   # 運用資金（円）
MAX_POSITIONS = 4           # 最大同時保有ポジション数

STOP_LOSS_PCT_PM = 0.06   # ポジション管理の損切り率 (-6%)
TARGET_PCT_PM    = 0.12   # 利確目標率 (+12%, RR 1:2)

MORNING_PROFIT_HALFSELL = 2.0    # 半決済推奨の含み益閾値 (%)
MORNING_ADD_LOT_LOSS    = -3.0   # 追加ロット推奨の含み損閾値 (%)
MORNING_HEDGE_LOSS      = -5.0   # ヘッジ売り推奨の含み損閾値 (%)
MORNING_STOPLOSS_LOSS   = -6.0   # 損切り推奨の含み損閾値 (%)

PORTFOLIO_FILE = "portfolio.json"
