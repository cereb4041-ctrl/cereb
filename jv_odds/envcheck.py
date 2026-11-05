"""
実機環境チェック (python -m jv_odds check-env)。

  1. Python (bit 数・OS) と pywin32
  2. レジストリ上の JV-Link COM 登録 (ProgID → CLSID → InprocServer32, 32/64bit ビュー)
  3. COM 生成と JVInit
  4. JVRTOpen のプローブ（毎回 JVClose する）
       a. 不正キー            → -114 (key 不正) を捕捉できるか
       b. 不正 dataspec       → -111 を捕捉できるか
       c. 16 桁ダミーキー      → -1 (該当なし) または 0 になり、キー形式として受理されるか
       d. 12 桁ダミーキー      → 同上（YYYYMMDDJJRR 形式の受理確認）
       e. 連続呼び出し         → -202 (JVClose 漏れ) にならないか
       f. 認証エラー (-301/-302/-303) の検出
  5. 実レースキー指定時 (--race-key) は 0B31/0B32 を実際に取得し、レコード長と内容を検査

プローブは COM オブジェクトを引数に取るため、偽 COM でテストできる。
"""
from __future__ import annotations

import platform
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .jvlink import ERROR_MESSAGES, JVLinkError, describe_exception, validate_rt_key

OK, WARN, NG = "OK", "WARN", "NG"

DUMMY_RACE_KEY_16 = "2000010105010101"   # 2000/01/01 東京 1回1日 1R（提供期間外 → -1 想定）
DUMMY_RACE_KEY_12 = "200001010501"       # 同 12 桁形式 YYYYMMDDJJRR
AUTH_CODES = {-301, -302, -303}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> Check:
        c = Check(name, status, detail)
        self.checks.append(c)
        return c

    @property
    def ok(self) -> bool:
        return all(c.status != NG for c in self.checks)

    def render(self) -> str:
        lines = [f"[{c.status:<4}] {c.name:<28} {c.detail}" for c in self.checks]
        lines.append(f"結果: {'OK' if self.ok else 'NG'}")
        return "\n".join(lines)


def _desc(code: int) -> str:
    return f"{code} ({ERROR_MESSAGES.get(code, '不明')})"


def check_python(report: Report) -> bool:
    bits = struct.calcsize("P") * 8
    is_win = platform.system() == "Windows"
    report.add("Python", OK if bits == 32 else WARN,
               f"{sys.version.split()[0]} {bits}bit / {platform.system()} {platform.release()}"
               + ("" if bits == 32 else "  ※JV-Link は 32bit COM。64bit 版では通常生成できません"))
    if not is_win:
        report.add("OS", NG, "JV-Link は Windows 専用です（--mock でのみ動作確認可能）")
    try:
        import pythoncom  # type: ignore  # noqa: F401
        import win32com.client  # type: ignore  # noqa: F401
        report.add("pywin32", OK)
        return is_win
    except ImportError:
        report.add("pywin32", NG, "`pip install -r jv_odds/requirements.txt` を実行してください")
        return False


def check_registry(report: Report, progid: str) -> None:
    """ProgID の登録と、DLL が 32bit/64bit どちらのビューに登録されているかを確認する。"""
    try:
        import winreg  # type: ignore
    except ImportError:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\CLSID") as k:
            clsid = winreg.QueryValue(k, None)
    except OSError:
        report.add("COM 登録 (ProgID)", NG, f"{progid} が未登録です。JV-Link を再インストールしてください")
        return
    found = []
    for label, view in (("32bit", winreg.KEY_WOW64_32KEY), ("64bit", winreg.KEY_WOW64_64KEY)):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\InprocServer32",
                                0, winreg.KEY_READ | view) as k:
                found.append(f"{label}: {winreg.QueryValue(k, None)}")
        except OSError:
            pass
    bits = struct.calcsize("P") * 8
    status = OK if any(f.startswith(f"{bits}bit") for f in found) else WARN
    report.add("COM 登録 (InprocServer32)", status,
               f"CLSID={clsid} " + ("; ".join(found) or "InprocServer32 なし")
               + ("" if status == OK else f"  ※この Python ({bits}bit) 用の登録がありません"))


def _rt_code(com, dataspec: str, key: str) -> int:
    """JVRTOpen を呼び、戻り値に関わらず JVClose する。"""
    try:
        return int(com.JVRTOpen(dataspec, key))
    finally:
        try:
            com.JVClose()
        except Exception:
            pass


def probe_jvrtopen(report: Report, com) -> None:
    """JVInit 済みの COM に対して JVRTOpen のエラー処理経路を検証する。"""
    # a. 不正キー
    code = _rt_code(com, "0B31", "0000")
    report.add("JVRTOpen 不正キー", OK if code == -114 else (WARN if code < 0 else NG),
               f"戻り値 {_desc(code)}（期待値 -114）"
               + ("" if code < 0 else "  ※不正キーを受理しています"))
    if code in AUTH_CODES:
        report.add("利用キー認証", NG, f"{_desc(code)}  JV-Link 設定で利用キーを確認してください")
        return

    # b. 不正 dataspec
    code = _rt_code(com, "0B99", "20000101")
    report.add("JVRTOpen 不正 dataspec", OK if code == -111 else (WARN if code < 0 else NG),
               f"戻り値 {_desc(code)}（期待値 -111）")

    # c/d. 形式は正しいが存在しないレースのキー（16 桁・12 桁）
    for label, key in (("16桁", DUMMY_RACE_KEY_16), ("12桁", DUMMY_RACE_KEY_12)):
        validate_rt_key("0B31", key)  # 本システム側の検査を通ること
        code = _rt_code(com, "0B31", key)
        if code in (0, -1):
            report.add(f"JVRTOpen キー形式 {label}", OK,
                       f"{key} → {_desc(code)}（キー形式は受理）")
        elif code == -114:
            report.add(f"JVRTOpen キー形式 {label}", NG if label == "16桁" else WARN,
                       f"{key} → {_desc(code)}  ※この形式のキーは JV-Link に拒否されました")
        elif code in AUTH_CODES:
            report.add("利用キー認証", NG, f"{_desc(code)}  JV-Link 設定で利用キーを確認してください")
            return
        else:
            report.add(f"JVRTOpen キー形式 {label}", WARN, f"{key} → {_desc(code)}")

    # e. JVClose 漏れ検出（同じ呼び出しを 2 回続けて -202 にならないこと）
    codes = [_rt_code(com, "0B31", DUMMY_RACE_KEY_16) for _ in range(2)]
    report.add("JVRTOpen 連続呼び出し", NG if -202 in codes else OK,
               f"戻り値 {codes}" + ("  ※JVClose が効いていません" if -202 in codes else ""))


def probe_race(report: Report, fetch: Callable[[str, str], list], race_key: str,
               policy=None, reset: Optional[Callable[[], None]] = None,
               sleep: Callable[[float], None] = time.sleep) -> None:
    """
    実レースの 0B31/0B32 を取得し、レコード長・レースキー・オッズ件数を検査する。
    取得は本番と同じバックオフ付き再試行で行い、各試行の段階・エラーコードを表示する。
    """
    from .records import RECORD_LENGTH, RecordLengthError, parse_o1, parse_o2, record_spec
    from .retry import RetryPolicy, call_with_retry
    policy = policy or RetryPolicy()
    for spec, rtype, parser in (("0B31", "O1", parse_o1), ("0B32", "O2", parse_o2)):
        history: list = []
        try:
            recs = call_with_retry(f"check-env {spec}", lambda: fetch(spec, race_key), policy,
                                   reset=reset, sleep=sleep, history=history)
        except Exception as e:
            report.add(f"実取得 {spec}", NG, describe_exception(e))
            _add_attempts(report, spec, history)
            continue
        _add_attempts(report, spec, history)
        if not recs:
            report.add(f"実取得 {spec}", WARN, f"{race_key}: 該当データなし（発売前・提供期間外の可能性）")
            continue
        kinds = sorted({record_spec(r) for r in recs})
        target = [r for r in recs if record_spec(r) == rtype]
        lens = sorted({len(r) for r in target})
        try:
            parsed = [parser(r) for r in target]
        except RecordLengthError as e:
            report.add(f"実取得 {spec}", NG, f"{e}（仕様 {RECORD_LENGTH[rtype]} byte）")
            continue
        n = sum(len(p.win_place) + len(p.quinella) for p in parsed)
        keys_ok = all(p.race_id == race_key[:16] for p in parsed) if len(race_key) == 16 else True
        report.add(f"実取得 {spec}", OK if parsed and keys_ok and n else WARN,
                   f"レコード {len(recs)} 件 種別={kinds} 長さ={lens} 明細 {n} 行 "
                   f"発表={parsed[-1].happyo_at if parsed else '-'} 区分={parsed[-1].data_kubun if parsed else '-'}"
                   + ("" if keys_ok else "  ※レースキー不一致"))


def _add_attempts(report: Report, spec: str, history: list) -> None:
    """再試行が発生した場合のみ、各試行の結果を WARN として残す。"""
    if len(history) <= 1:
        return
    for h in history:
        if not h.ok:
            report.add(f"  {spec} 試行 {h.attempt}", WARN,
                       h.detail + (f" → {h.wait:.1f}s 待機" if h.wait else ""))


def run_env_check(progid: str, sid: str, race_key: Optional[str] = None,
                  com_factory: Optional[Callable[[], object]] = None,
                  fetch: Optional[Callable[[str, str], list]] = None,
                  policy=None, sleep: Callable[[float], None] = time.sleep) -> Report:
    report = Report()
    if com_factory is None:
        if not check_python(report):
            return report
        check_registry(report, progid)
        from .jvlink import create_com
        com_factory = lambda: create_com(progid)  # noqa: E731

    try:
        com = com_factory()
    except Exception as e:
        report.add("COM 生成", NG, str(e))
        return report
    report.add("COM 生成", OK, progid)

    try:
        code = int(com.JVInit(sid))
    except Exception as e:
        report.add("JVInit", NG, f"例外: {describe_exception(e)}")
        return report
    if code != 0:
        report.add("JVInit", NG, _desc(code))
        return report
    report.add("JVInit", OK, f"sid={sid}")

    try:
        probe_jvrtopen(report, com)
    except Exception as e:  # COM 切断など
        report.add("JVRTOpen プローブ", NG, f"例外: {describe_exception(e)}")
        try:
            com.JVClose()
        except Exception:
            pass

    if race_key:
        try:
            validate_rt_key("0B31", race_key)
        except JVLinkError as e:
            report.add("--race-key", NG, str(e))
        else:
            reset = None
            if fetch is None:
                from .jvlink import JVLinkClient
                client = JVLinkClient(sid, progid)
                client._jv = com  # 生成済みの COM を流用（JVInit 済み）
                fetch, reset = client.rt_records, client.reset
            probe_race(report, fetch, race_key, policy, reset, sleep)
    return report
