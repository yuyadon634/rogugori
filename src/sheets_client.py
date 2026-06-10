"""
Google Sheets を使ったデータ永続化層。
3つのシートを管理する:
  - daily_summary : 日次の健康データ（30日分の履歴）
  - status        : 当日の通知送信状態管理
  - session       : Garmin セッション・Eufy トークンの保存
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

_JST = timezone(timedelta(hours=9))

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEET_DAILY_SUMMARY = "daily_summary"
SHEET_STATUS = "status"
SHEET_SESSION = "session"

DAILY_SUMMARY_HEADERS = [
    "date",
    "total_distance_km",
    "avg_pace_per_km",
    "avg_heart_rate",
    "sleep_score",
    "sleep_hours",
    "weight_kg",
    "body_fat_pct",
    "bmi",
    "lean_body_mass_kg",
    "consecutive_exercise_days",
    "consecutive_rest_days",
]

STATUS_HEADERS = [
    "date",
    "sleep_sent",
    "activities_notified",  # JSON 配列文字列でアクティビティIDを管理
    "weight_sent",
    "llm_sent",
    "rest_day_sent",
]


class SheetsClient:
    def __init__(self, credentials_info: dict, spreadsheet_id: str):
        """
        credentials_info: サービスアカウントの認証情報 dict
        spreadsheet_id  : Google スプレッドシートのID
        """
        creds = Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
        self._gc = gspread.authorize(creds)
        self._spreadsheet_id = spreadsheet_id
        self._spreadsheet = self._gc.open_by_key(spreadsheet_id)
        self._ensure_sheets()

    # ------------------------------------------------------------------
    # シート初期化
    # ------------------------------------------------------------------

    def _get_or_create_sheet(self, title: str, headers: list[str]) -> gspread.Worksheet:
        try:
            ws = self._spreadsheet.worksheet(title)
            # 既存シートのヘッダー行を確認・修復する
            first_row = ws.row_values(1)
            if first_row != headers:
                logger.warning(
                    "シート '%s' のヘッダーが不正です。修復します: %s → %s",
                    title, first_row, headers
                )
                ws.update("A1", [headers])
        except gspread.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
            ws.update("A1", [headers])
            logger.info("シート '%s' を作成しました", title)
        return ws

    def _ensure_sheets(self) -> None:
        self._daily_ws = self._get_or_create_sheet(SHEET_DAILY_SUMMARY, DAILY_SUMMARY_HEADERS)
        self._status_ws = self._get_or_create_sheet(SHEET_STATUS, STATUS_HEADERS)
        self._session_ws = self._get_or_create_sheet(SHEET_SESSION, ["key", "value"])

    # ------------------------------------------------------------------
    # daily_summary
    # ------------------------------------------------------------------

    def upsert_daily_summary(self, data: dict) -> None:
        """
        当日の daily_summary 行を更新（なければ追記）する。
        data には DAILY_SUMMARY_HEADERS のキーを含む dict を渡す。
        """
        today = data.get("date", str(datetime.now(_JST).date()))
        records = self._daily_ws.get_all_records()
        for i, row in enumerate(records):
            if row.get("date") == today:
                row_index = i + 2  # ヘッダー行 + 1-indexed
                values = [data.get(h, row.get(h, "")) for h in DAILY_SUMMARY_HEADERS]
                self._daily_ws.update(f"A{row_index}", [values])
                logger.debug("daily_summary 行を更新: %s", today)
                return
        values = [data.get(h, "") for h in DAILY_SUMMARY_HEADERS]
        self._daily_ws.append_row(values)
        logger.debug("daily_summary 行を追記: %s", today)

    def get_daily_summary(self, target_date: str) -> Optional[dict]:
        records = self._daily_ws.get_all_records()
        for row in records:
            if row.get("date") == target_date:
                return dict(row)
        return None

    def get_recent_summaries(self, days: int = 30) -> list[dict]:
        """過去 days 日分の daily_summary を日付昇順で返す"""
        cutoff = datetime.now(_JST).date() - timedelta(days=days)
        records = self._daily_ws.get_all_records()
        result = []
        for row in records:
            try:
                row_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
                if row_date >= cutoff:
                    result.append(dict(row))
            except (ValueError, KeyError):
                continue
        return sorted(result, key=lambda r: r["date"])

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def get_today_status(self) -> dict:
        """当日の status 行を返す（JST基準）。存在しない場合は初期値で新規作成する。"""
        today = str(datetime.now(_JST).date())
        records = self._status_ws.get_all_records(expected_headers=STATUS_HEADERS)
        for row in records:
            if row.get("date") == today:
                return dict(row)
        initial = {
            "date": today,
            "sleep_sent": False,
            "activities_notified": "[]",
            "weight_sent": False,
            "llm_sent": False,
            "rest_day_sent": False,
        }
        self._status_ws.append_row([initial[h] for h in STATUS_HEADERS])
        logger.debug("status 行を新規作成: %s", today)
        return initial

    def update_status(self, updates: dict) -> None:
        """当日の status 行の指定フィールドを更新する（JST基準）。"""
        today = str(datetime.now(_JST).date())
        records = self._status_ws.get_all_records(expected_headers=STATUS_HEADERS)
        for i, row in enumerate(records):
            if row.get("date") == today:
                row.update(updates)
                row_index = i + 2
                values = [row.get(h, "") for h in STATUS_HEADERS]
                self._status_ws.update(f"A{row_index}", [values])
                logger.debug("status 行を更新: %s %s", today, updates)
                return
        logger.warning("status 行が見つからないため update をスキップ: %s", today)

    def get_notified_activity_ids(self) -> list[str]:
        status = self.get_today_status()
        raw = status.get("activities_notified", "[]")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []

    def add_notified_activity_id(self, activity_id: str) -> None:
        ids = self.get_notified_activity_ids()
        if activity_id not in ids:
            ids.append(activity_id)
            self.update_status({"activities_notified": json.dumps(ids)})

    # ------------------------------------------------------------------
    # session (Garmin クッキー)
    # ------------------------------------------------------------------

    def get_garmin_session(self) -> Optional[str]:
        """保存済みのGarminセッションJSON文字列を返す。なければ None。"""
        records = self._session_ws.get_all_records(expected_headers=["key", "value"])
        for row in records:
            if row.get("key") == "garmin_session":
                v = row.get("value", "")
                return v if v else None
        return None

    def save_garmin_session(self, session_json: str) -> None:
        """GarminセッションJSON文字列を保存（上書き）する。"""
        records = self._session_ws.get_all_records(expected_headers=["key", "value"])
        for i, row in enumerate(records):
            if row.get("key") == "garmin_session":
                row_index = i + 2
                self._session_ws.update(f"A{row_index}", [["garmin_session", session_json]])
                logger.debug("Garmin セッションを更新しました")
                return
        self._session_ws.append_row(["garmin_session", session_json])
        logger.debug("Garmin セッションを新規保存しました")

    # ------------------------------------------------------------------
    # session (Eufy トークン)
    # ------------------------------------------------------------------

    def get_eufy_token(self) -> Optional[str]:
        """保存済みの EufyLife トークン JSON 文字列を返す。なければ None。"""
        records = self._session_ws.get_all_records(expected_headers=["key", "value"])
        for row in records:
            if row.get("key") == "eufy_token":
                v = row.get("value", "")
                return v if v else None
        return None

    def save_eufy_token(self, token_json: str) -> None:
        """EufyLife トークン JSON 文字列を保存（上書き）する。"""
        records = self._session_ws.get_all_records(expected_headers=["key", "value"])
        for i, row in enumerate(records):
            if row.get("key") == "eufy_token":
                row_index = i + 2
                self._session_ws.update(f"A{row_index}", [["eufy_token", token_json]])
                logger.debug("EufyLife トークンを更新しました")
                return
        self._session_ws.append_row(["eufy_token", token_json])
        logger.debug("EufyLife トークンを新規保存しました")
