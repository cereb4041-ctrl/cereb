"""
JV-Link (COM: JVDTLab.JVLink) の薄いラッパー。

  - pywin32 (win32com.client) で COM を生成し、JVInit / JVOpen / JVRTOpen / JVRead / JVClose を呼ぶ
  - 戻り値の負数は JVLinkError に変換し、呼び出し側で「再試行可能か」を判定できるようにする
  - pywin32 の out 引数は戻り値タプルとして返る:
        JVRead("", size, "")              -> (ret, buff, size, filename)
        JVOpen(spec, from, opt, 0, 0, "") -> (ret, readcount, downloadcount, lastfiletimestamp)
"""
from __future__ import annotations

import logging
import time
from typing import Iterator, Optional

log = logging.getLogger(__name__)

# JV-Link の主なエラーコード
ERROR_MESSAGES = {
    -1: "該当データなし",
    -2: "セットアップダイアログでキャンセル",
    -100: "パラメータ不正 (sid)",
    -101: "sid が設定されていない",
    -102: "sid が 64byte を超えている",
    -103: "sid 不正",
    -111: "dataspec パラメータ不正",
    -112: "fromtime パラメータ不正 (開始)",
    -113: "fromtime パラメータ不正 (終了)",
    -114: "key パラメータ不正",
    -115: "option パラメータ不正",
    -116: "dataspec と option の組み合わせ不正",
    -201: "JVInit が行われていない",
    -202: "前回の JVOpen/JVRTOpen に対して JVClose が呼ばれていない",
    -203: "JVOpen が行われていない",
    -211: "レジストリ内容が不正",
    -301: "認証エラー",
    -302: "利用キーの有効期限切れ",
    -303: "利用キーが設定されていない",
    -401: "JV-Link 内部エラー",
    -402: "ダウンロードファイル異常 (サイズ 0)",
    -403: "ダウンロードファイル異常 (データ内容)",
    -411: "サーバーエラー (HTTP 404)",
    -412: "サーバーエラー (HTTP 403)",
    -413: "サーバーエラー (HTTP その他)",
    -421: "サーバーエラー (応答不正)",
    -431: "サーバーエラー (サーバーアプリ内部)",
    -501: "スタートキット無効",
    -502: "ダウンロード失敗 (通信エラー/ディスクエラー)",
    -503: "ファイルが見つからない",
    -504: "サーバーメンテナンス中",
}

# 時間を置けば回復する見込みのあるエラー（通信・サーバー・状態不整合）
RETRYABLE_CODES = {-201, -202, -203, -401, -402, -403, -411, -412, -413, -421, -431, -502, -503, -504}
# 設定を直さない限り回復しないエラー
FATAL_CODES = {-100, -101, -102, -103, -211, -301, -302, -303}

READ_EOF = 0
READ_FILE_SWITCH = -1
READ_DOWNLOADING = -3


class JVLinkError(Exception):
    def __init__(self, func: str, code: int, detail: str = ""):
        self.func = func
        self.code = code
        msg = ERROR_MESSAGES.get(code, "不明なエラー")
        super().__init__(f"{func} failed: code={code} ({msg}){' ' + detail if detail else ''}")

    @property
    def retryable(self) -> bool:
        return self.code not in FATAL_CODES

    @property
    def no_data(self) -> bool:
        return self.code == -1


class JVLinkUnavailable(Exception):
    """COM オブジェクトを生成できない（未インストール・64bit Python など）。"""


def _pump():
    try:
        import pythoncom  # type: ignore
        pythoncom.PumpWaitingMessages()
    except Exception:
        pass


class JVLinkClient:
    """実機の JV-Link を操作するクライアント。"""

    def __init__(self, sid: str = "UNKNOWN", progid: str = "JVDTLab.JVLink",
                 read_buffer: int = 110000, download_timeout: int = 300):
        self.sid = sid
        self.progid = progid
        self.read_buffer = read_buffer
        self.download_timeout = download_timeout
        self._jv = None
        self._opened = False

    # ---- 接続管理 -------------------------------------------------------
    def connect(self) -> None:
        if self._jv is not None:
            return
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except ImportError as e:
            raise JVLinkUnavailable(
                "pywin32 が見つかりません。Windows 上で `pip install pywin32` を実行してください。") from e
        try:
            pythoncom.CoInitialize()
            self._jv = win32com.client.Dispatch(self.progid)
        except Exception as e:
            self._jv = None
            raise JVLinkUnavailable(
                f"COM オブジェクト {self.progid} を生成できません。JV-Link のインストールと "
                f"32bit 版 Python の使用を確認してください: {e}") from e
        ret = self._jv.JVInit(self.sid)
        if ret != 0:
            self._jv = None
            raise JVLinkError("JVInit", ret)
        log.info("JV-Link 初期化完了 (sid=%s)", self.sid)

    def close(self) -> None:
        if self._jv is not None and self._opened:
            try:
                self._jv.JVClose()
            except Exception as e:  # 切断済みなどは無視
                log.debug("JVClose で例外 (無視): %s", e)
        self._opened = False

    def reset(self) -> None:
        """切断・異常時に COM オブジェクトを破棄し、次回 connect で作り直す。"""
        self.close()
        self._jv = None
        try:
            import pythoncom  # type: ignore
            pythoncom.CoUninitialize()
        except Exception:
            pass
        log.info("JV-Link をリセットしました（次回再初期化）")

    # ---- 読み出し -------------------------------------------------------
    def _read_all(self, func: str) -> Iterator[str]:
        """JVRead を EOF まで回してレコード文字列を返す。"""
        assert self._jv is not None
        while True:
            res = self._jv.JVRead("", self.read_buffer, "")
            ret, buff = res[0], res[1]
            if ret > 0:
                yield buff
            elif ret == READ_EOF:
                return
            elif ret == READ_FILE_SWITCH:
                continue
            elif ret == READ_DOWNLOADING:
                _pump()
                time.sleep(0.5)
            else:
                raise JVLinkError(f"JVRead({func})", ret)

    def rt_records(self, dataspec: str, key: str) -> list[str]:
        """速報系 (JVRTOpen) のデータを全件取得する。該当データなしは空リスト。"""
        self.connect()
        assert self._jv is not None
        ret = self._jv.JVRTOpen(dataspec, key)
        if ret == -1:
            return []
        if ret < 0:
            raise JVLinkError(f"JVRTOpen({dataspec},{key})", ret)
        self._opened = True
        try:
            return list(self._read_all(f"{dataspec},{key}"))
        finally:
            self.close()

    def stored_records(self, dataspec: str, fromtime: str, option: int,
                       record_filter: Optional[set[str]] = None) -> list[str]:
        """蓄積系 (JVOpen) のデータを取得する。option=2 で今週データ。"""
        self.connect()
        assert self._jv is not None
        res = self._jv.JVOpen(dataspec, fromtime, option, 0, 0, "")
        ret = res[0] if isinstance(res, tuple) else res
        if ret == -1:
            return []
        if ret < 0:
            raise JVLinkError(f"JVOpen({dataspec},{fromtime},{option})", ret)
        self._opened = True
        try:
            download_count = res[2] if isinstance(res, tuple) and len(res) > 2 else 0
            self._wait_download(download_count)
            out = []
            for rec in self._read_all(dataspec):
                if record_filter is None or rec[:2] in record_filter:
                    out.append(rec)
            return out
        finally:
            self.close()

    def _wait_download(self, download_count: int) -> None:
        if download_count <= 0:
            return
        deadline = time.monotonic() + self.download_timeout
        while True:
            st = self._jv.JVStatus()
            if st < 0:
                raise JVLinkError("JVStatus", st)
            if st >= download_count:
                return
            if time.monotonic() > deadline:
                raise JVLinkError("JVStatus", -502, "ダウンロード待ちタイムアウト")
            _pump()
            time.sleep(0.5)
