"""
共通ユーティリティ。
data_sync.py / analysis.py で重複していた定数・ロギング設定・ファクトリ関数を集約する。
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

JST = timezone(timedelta(hours=9))

_COMMON_REQUIRED_KEYS = [
    "GARMIN_EMAIL",
    "GARMIN_PASSWORD",
    "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_USER_ID",
    "GOOGLE_SHEETS_ID",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
]


def setup_logging() -> None:
    """ルートロガーにファイル + 標準出力のハンドラーを設定する。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("app.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_env(extra_keys: list[str] | None = None) -> dict:
    """
    .env を読み込み、共通必須キー + extra_keys の値を dict で返す。
    未設定のキーがあれば EnvironmentError を送出する。
    """
    load_dotenv()
    required = _COMMON_REQUIRED_KEYS + (extra_keys or [])
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise EnvironmentError(f"必須の環境変数が未設定です: {missing}")
    return {k: os.getenv(k) for k in required}


def build_sheets_client(env: dict):
    """env dict から SheetsClient を構築して返す。"""
    from src.sheets_client import SheetsClient
    credentials_info = json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"])
    return SheetsClient(credentials_info, env["GOOGLE_SHEETS_ID"])


def today_jst() -> str:
    """JST 基準の今日の日付文字列（YYYY-MM-DD）を返す。"""
    return str(datetime.now(JST).date())
