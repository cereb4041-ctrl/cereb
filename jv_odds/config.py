"""設定ファイル (config.ini) の読み込み。"""
from __future__ import annotations

import configparser
from dataclasses import dataclass
from datetime import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.ini"


@dataclass
class Settings:
    sid: str = "UNKNOWN"
    progid: str = "JVDTLab.JVLink"
    read_buffer: int = 110000
    download_timeout: int = 300

    interval_minutes: int = 10
    start_time: time = time(8, 30)
    stop_before_post_minutes: int = 0
    fallback_end_time: time = time(17, 0)
    retry_count: int = 2
    retry_wait_seconds: int = 15
    reinit_after_errors: int = 1
    race_refresh_minutes: int = 60
    final_fetch_minutes_before: int = 3

    db_path: Path = BASE_DIR / "data" / "jv_odds.db"
    log_dir: Path = BASE_DIR / "logs"


def _time(s: str) -> time:
    hh, mm = s.strip().split(":")
    return time(int(hh), int(mm))


def _path(s: str) -> Path:
    p = Path(s)
    return p if p.is_absolute() else BASE_DIR / p


def load_settings(path: Path | str | None = None) -> Settings:
    st = Settings()
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        return st
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")

    j = cp["jvlink"] if cp.has_section("jvlink") else {}
    st.sid = j.get("sid", st.sid)
    st.progid = j.get("progid", st.progid)
    st.read_buffer = int(j.get("read_buffer", st.read_buffer))
    st.download_timeout = int(j.get("download_timeout", st.download_timeout))

    c = cp["collector"] if cp.has_section("collector") else {}
    st.interval_minutes = int(c.get("interval_minutes", st.interval_minutes))
    if "start_time" in c:
        st.start_time = _time(c["start_time"])
    st.stop_before_post_minutes = int(c.get("stop_before_post_minutes", st.stop_before_post_minutes))
    if "fallback_end_time" in c:
        st.fallback_end_time = _time(c["fallback_end_time"])
    st.retry_count = int(c.get("retry_count", st.retry_count))
    st.retry_wait_seconds = int(c.get("retry_wait_seconds", st.retry_wait_seconds))
    st.reinit_after_errors = int(c.get("reinit_after_errors", st.reinit_after_errors))
    st.race_refresh_minutes = int(c.get("race_refresh_minutes", st.race_refresh_minutes))
    st.final_fetch_minutes_before = int(c.get("final_fetch_minutes_before",
                                              st.final_fetch_minutes_before))

    p = cp["paths"] if cp.has_section("paths") else {}
    if "db_path" in p:
        st.db_path = _path(p["db_path"])
    if "log_dir" in p:
        st.log_dir = _path(p["log_dir"])
    return st
