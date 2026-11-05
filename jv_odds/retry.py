"""
JV-Link 呼び出しの再試行（エクスポネンシャルバックオフ）。

  待ち時間 = min(base * 2^(n-1), max_wait) ± jitter   (例: 2s → 4s → 8s → 16s)

  - 再試行の前に必ず reset()（JVClose → COM 破棄 → 次回 JVInit からやり直し）を呼ぶ
  - 設定・引数エラー（JVLinkError.retryable == False）と JV-Link 未導入は再試行しない
  - 試行ごとに「どの段階 (JVInit/JVRTOpen/JVGets…) で何のコードが返ったか」をログに残す
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

from .jvlink import JVLinkError, JVLinkUnavailable, describe_exception

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class RetryPolicy:
    max_attempts: int = 5        # 初回を含む試行回数
    base_wait: float = 2.0       # 1 回目の再試行までの秒数
    max_wait: float = 30.0       # 1 回の待ちの上限
    jitter: float = 0.2          # 待ち時間に ±20% の揺らぎ（同時刻の集中を避ける）

    def wait_for(self, retry_no: int, code: Optional[int] = None) -> float:
        """retry_no 回目 (1 始まり) の再試行前の待ち秒数。"""
        w = min(self.base_wait * (2 ** (retry_no - 1)), self.max_wait)
        if code == -504:  # サーバーメンテナンス中は上限まで待つ
            w = self.max_wait
        if self.jitter and w > 0:
            w *= 1 + random.uniform(-self.jitter, self.jitter)
        return max(w, 0.0)


@dataclass
class AttemptLog:
    attempt: int
    ok: bool
    detail: str
    wait: float = 0.0


def call_with_retry(
    desc: str,
    func: Callable[[], T],
    policy: RetryPolicy,
    reset: Optional[Callable[[], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    history: Optional[list[AttemptLog]] = None,
) -> T:
    """func を policy に従って再試行する。最後まで失敗したら最後の例外を送出する。"""
    last: Optional[BaseException] = None
    for attempt in range(1, policy.max_attempts + 1):
        t0 = time.monotonic()
        try:
            result = func()
            if attempt > 1:
                log.info("%s: %d 回目で成功", desc, attempt)
            if history is not None:
                history.append(AttemptLog(attempt, True, "成功"))
            return result
        except JVLinkUnavailable:
            raise
        except JVLinkError as e:
            last = e
            detail = e.detail_text()
            if not e.retryable:
                log.error("%s: 再試行不可のエラー %s", desc, detail)
                if history is not None:
                    history.append(AttemptLog(attempt, False, detail))
                _safe_reset(reset, desc)
                raise
            code = e.code
        except Exception as e:  # pywintypes.com_error（RPC 切断・COM 異常）など
            last = e
            detail = describe_exception(e)
            code = None
        elapsed = time.monotonic() - t0

        _safe_reset(reset, desc)  # 失敗のたびに JV-Link の状態を完全にクリア
        if attempt >= policy.max_attempts:
            log.error("%s: 失敗 (%d/%d, %.1fs) 再試行上限に達しました: %s",
                      desc, attempt, policy.max_attempts, elapsed, detail)
            if history is not None:
                history.append(AttemptLog(attempt, False, detail))
            break
        wait = policy.wait_for(attempt, code)
        log.warning("%s: 失敗 (%d/%d, %.1fs): %s → %.1f 秒後に再試行",
                    desc, attempt, policy.max_attempts, elapsed, detail, wait)
        if history is not None:
            history.append(AttemptLog(attempt, False, detail, wait))
        sleep(wait)
    assert last is not None
    raise last


def _safe_reset(reset: Optional[Callable[[], None]], desc: str) -> None:
    if reset is None:
        return
    try:
        reset()
    except Exception as e:  # リセット自体の失敗で処理を止めない
        log.warning("%s: リセット中に例外 (無視): %s", desc, describe_exception(e))
