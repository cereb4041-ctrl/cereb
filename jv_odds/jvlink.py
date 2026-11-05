"""
JV-Link (COM: JVDTLab.JVLink) の薄いラッパー。

呼び出し順序（JV-Link インターフェース仕様書 4.9.0.1）:
    JVInit → JVOpen / JVRTOpen → (JVStatus でダウンロード待ち) → JVGets を EOF まで → JVClose

  - レコードは JVGets で **Shift-JIS の bytes のまま** 受け取る。JVRead は Unicode 文字列
    (BSTR) を返すため、cp932 に戻すと固定長のバイト位置がずれる恐れがある。
  - JVOpen / JVRTOpen が -1（該当データなし）を返した場合も JVClose が必要。呼ばないと
    次の Open が -202 になる。
  - 戻り値の負数は JVLinkError に変換し、呼び出し側で「再試行可能か」を判定する。
  - pywin32 では out 引数が戻り値タプルとして返る:
        JVGets(buff, size, filename)       -> (ret, buff(memoryview), filename)
        JVRead(buff, size, filename)       -> (ret, buff(str), size, filename)
        JVOpen(spec, from, opt, 0, 0, "")  -> (ret, readcount, downloadcount, lastfiletimestamp)
"""
from __future__ import annotations

import logging
import re
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

# 設定・引数を直さない限り回復しないエラー（再試行しない）
FATAL_CODES = {-100, -101, -102, -103, -111, -112, -113, -114, -115, -116,
               -211, -301, -302, -303}
# ダウンロード済みファイルが壊れている（JVFiledelete で削除して取り直す）
BROKEN_FILE_CODES = {-402, -403}

GETS_EOF = 0
GETS_FILE_SWITCH = -1
GETS_DOWNLOADING = -3

# JVGets/JVRead に渡すバッファ長。今週データ (RACE) には H6 (102,890 byte) が含まれるため
# それより大きくする。
DEFAULT_BUFFER = 200_000


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


# ---------------------------------------------------------------------
# JVRTOpen の dataspec とキー形式（JV-Data 仕様書「データ種別一覧」/ 提供単位）
#   レース単位 : YYYYMMDDJJKKHHRR (年月日+場+回+日目+R) または YYYYMMDDJJRR (年月日+場+R)
#   開催日単位 : YYYYMMDD
# ---------------------------------------------------------------------
RACE_KEY_RE = re.compile(r"^\d{8}(\d{2})(\d{4})?(\d{2})$")  # 12 桁 or 16 桁
DAY_KEY_RE = re.compile(r"^\d{8}$")

RT_DATASPECS = {
    "0B11": ("速報馬体重", "day_or_race"),
    "0B12": ("速報レース情報(成績確定後)", "day_or_race"),
    "0B13": ("速報タイム型データマイニング予想", "day_or_race"),
    "0B14": ("速報開催情報(一括)", "day"),
    "0B15": ("速報レース情報(出走馬名表～)", "day_or_race"),
    "0B16": ("速報開催情報(指定)", "day_or_race"),
    "0B17": ("速報対戦型データマイニング予想", "day_or_race"),
    "0B30": ("速報オッズ(全賭式)", "race"),
    "0B31": ("速報オッズ(単複枠)", "race"),
    "0B32": ("速報オッズ(馬連)", "race"),
    "0B33": ("速報オッズ(ワイド)", "race"),
    "0B34": ("速報オッズ(馬単)", "race"),
    "0B35": ("速報オッズ(3連複)", "race"),
    "0B36": ("速報オッズ(3連単)", "race"),
    "0B41": ("時系列オッズ(単複枠)", "race"),
    "0B42": ("時系列オッズ(馬連)", "race"),
}


def make_race_key(year: int | str, monthday: str, jyo_cd: str, kaiji: int, nichiji: int,
                  race_num: int) -> str:
    """16 桁のレースキー YYYYMMDDJJKKHHRR を組み立てる。"""
    return f"{int(year):04d}{monthday}{jyo_cd}{int(kaiji):02d}{int(nichiji):02d}{int(race_num):02d}"


def validate_rt_key(dataspec: str, key: str) -> None:
    """
    JVRTOpen に渡す前に dataspec とキーの組み合わせを検査する。
    不正なら JV-Link と同じコード (-111 / -114) の JVLinkError を送出する（再試行しない）。
    """
    if dataspec not in RT_DATASPECS:
        raise JVLinkError(f"JVRTOpen({dataspec},{key})", -111, "未対応の dataspec")
    unit = RT_DATASPECS[dataspec][1]
    is_race = bool(RACE_KEY_RE.match(key))
    is_day = bool(DAY_KEY_RE.match(key))
    ok = {"race": is_race, "day": is_day, "day_or_race": is_race or is_day}[unit]
    if is_race:
        m = RACE_KEY_RE.match(key)
        mmdd, rr = key[4:8], m.group(3)
        ok = ok and "0101" <= mmdd <= "1231" and "01" <= rr <= "12"
    if not ok:
        expect = {"race": "YYYYMMDDJJKKHHRR (16桁) / YYYYMMDDJJRR (12桁)",
                  "day": "YYYYMMDD (8桁)",
                  "day_or_race": "YYYYMMDD / YYYYMMDDJJKKHHRR"}[unit]
        raise JVLinkError(f"JVRTOpen({dataspec},{key})", -114, f"キー形式は {expect}")


def _pump():
    try:
        import pythoncom  # type: ignore
        pythoncom.PumpWaitingMessages()
    except Exception:
        pass


def create_com(progid: str = "JVDTLab.JVLink"):
    """COM オブジェクトを生成する（CoInitialize 込み）。"""
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as e:
        raise JVLinkUnavailable(
            "pywin32 が見つかりません。Windows 上で `pip install pywin32` を実行してください。") from e
    pythoncom.CoInitialize()
    try:
        return win32com.client.Dispatch(progid)
    except Exception as e:
        pythoncom.CoUninitialize()
        raise JVLinkUnavailable(
            f"COM オブジェクト {progid} を生成できません。JV-Link のインストールと "
            f"32bit 版 Python の使用を確認してください: {e}") from e


class JVLinkClient:
    """実機の JV-Link を操作するクライアント。"""

    def __init__(self, sid: str = "UNKNOWN", progid: str = "JVDTLab.JVLink",
                 read_buffer: int = DEFAULT_BUFFER, download_timeout: int = 300,
                 read_method: str = "gets"):
        self.sid = sid
        self.progid = progid
        self.read_buffer = max(read_buffer, DEFAULT_BUFFER)
        self.download_timeout = download_timeout
        self.read_method = read_method  # "gets" (推奨) / "read"
        self._jv = None
        self._opened = False

    # ---- 接続管理 -------------------------------------------------------
    def connect(self) -> None:
        if self._jv is not None:
            return
        com = create_com(self.progid)
        ret = int(com.JVInit(self.sid))
        if ret != 0:
            raise JVLinkError("JVInit", ret)
        self._jv = com
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
        had = self._jv is not None
        self.close()
        self._jv = None
        if had:
            try:
                import pythoncom  # type: ignore
                pythoncom.CoUninitialize()
            except Exception:
                pass
            log.info("JV-Link をリセットしました（次回再初期化）")

    # ---- 読み出し -------------------------------------------------------
    def _read_one(self) -> tuple[int, bytes, str]:
        """JVGets（または JVRead）を 1 回呼び、(コード, レコード bytes, ファイル名) を返す。"""
        assert self._jv is not None
        if self.read_method == "gets":
            # バッファは空で渡す（JV-Link が確保した配列に差し替えて返す。仕様書 p.28）
            ret, buff, filename = self._jv.JVGets(bytearray(), self.read_buffer, bytearray())
            code = int(ret)
            data = bytes(buff)[:code] if code > 0 and buff is not None else b""
            return code, data, str(filename or "")
        res = self._jv.JVRead("", self.read_buffer, "")
        code = int(res[0])
        data = res[1].encode("cp932", errors="replace") if code > 0 and res[1] else b""
        return code, data, str(res[3] if len(res) > 3 else "")

    def _read_all(self, func: str) -> Iterator[bytes]:
        while True:
            code, data, filename = self._read_one()
            if code > 0:
                yield data
            elif code == GETS_EOF:
                return
            elif code == GETS_FILE_SWITCH:
                continue
            elif code == GETS_DOWNLOADING:
                _pump()
                time.sleep(0.5)
            elif code in BROKEN_FILE_CODES:
                # 壊れたファイルを削除しておけば、次回の取得で再ダウンロードされる
                if filename:
                    try:
                        self._jv.JVFiledelete(filename)
                        log.warning("破損ファイルを削除しました: %s", filename)
                    except Exception as e:
                        log.warning("JVFiledelete 失敗 %s: %s", filename, e)
                raise JVLinkError(f"JVGets({func})", code, filename)
            else:
                raise JVLinkError(f"JVGets({func})", code)

    def rt_open_code(self, dataspec: str, key: str) -> int:
        """
        JVRTOpen を呼んで戻り値をそのまま返し、必ず JVClose する（環境チェック用）。
        """
        self.connect()
        try:
            return int(self._jv.JVRTOpen(dataspec, key))
        finally:
            self._opened = True
            self.close()

    def rt_records(self, dataspec: str, key: str) -> list[bytes]:
        """速報系 (JVRTOpen) のデータを全件取得する。該当データなしは空リスト。"""
        validate_rt_key(dataspec, key)
        self.connect()
        assert self._jv is not None
        try:
            ret = int(self._jv.JVRTOpen(dataspec, key))
            self._opened = True  # -1 でも JVClose が必要
            if ret == -1:
                return []
            if ret < 0:
                raise JVLinkError(f"JVRTOpen({dataspec},{key})", ret)
            return list(self._read_all(f"{dataspec},{key}"))
        finally:
            self.close()

    def stored_records(self, dataspec: str, fromtime: str, option: int,
                       record_filter: Optional[set[str]] = None) -> list[bytes]:
        """蓄積系 (JVOpen) のデータを取得する。option=2 で今週データ。"""
        self.connect()
        assert self._jv is not None
        wanted = {s.encode("ascii") for s in record_filter} if record_filter else None
        try:
            res = self._jv.JVOpen(dataspec, fromtime, int(option), 0, 0, "")
            ret = int(res[0]) if isinstance(res, (tuple, list)) else int(res)
            self._opened = True  # -1 でも JVClose が必要
            if ret == -1:
                return []
            if ret < 0:
                raise JVLinkError(f"JVOpen({dataspec},{fromtime},{option})", ret)
            download_count = int(res[2] or 0) if isinstance(res, (tuple, list)) and len(res) > 2 else 0
            self._wait_download(download_count)
            return [rec for rec in self._read_all(dataspec) if wanted is None or rec[:2] in wanted]
        finally:
            self.close()

    def _wait_download(self, download_count: int) -> None:
        if download_count <= 0:
            return
        deadline = time.monotonic() + self.download_timeout
        while True:
            st = int(self._jv.JVStatus())
            if st < 0:
                raise JVLinkError("JVStatus", st)
            if st >= download_count:
                return
            if time.monotonic() > deadline:
                try:
                    self._jv.JVCancel()
                except Exception:
                    pass
                raise JVLinkError("JVStatus", -502, "ダウンロード待ちタイムアウト")
            _pump()
            time.sleep(0.5)
