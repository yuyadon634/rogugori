"""
Garmin Connect からデータを取得するクライアント。

セッション管理戦略:
  1. Google Sheets からセッションクッキーを読み込む
  2. セッションが存在すれば再ログインなしで API を叩く
  3. セッション切れ（401等）が発生した場合のみ再ログインし、新セッションを Sheets に保存
  この戦略により、Garmin の過多ログイン検知（BAN）リスクを低減する。
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

_JST = timezone(timedelta(hours=9))

from garminconnect import Garmin, GarminConnectAuthenticationError

from src.sheets_client import SheetsClient

logger = logging.getLogger(__name__)


class GarminClient:
    def __init__(self, email: str, password: str, sheets: SheetsClient):
        self._email = email
        self._password = password
        self._sheets = sheets
        self._client: Optional[Garmin] = None

    # ------------------------------------------------------------------
    # 認証
    # ------------------------------------------------------------------

    def _login(self) -> None:
        """パスワードで再ログインし、セッションを Sheets に保存する。"""
        logger.info("Garmin に再ログインします")
        client = Garmin(self._email, self._password)
        client.login()
        session_json = client.client.dumps()
        self._sheets.save_garmin_session(session_json)
        self._client = client
        logger.info("Garmin ログイン成功・セッション保存完了")

    def _init_client(self) -> None:
        """
        保存済みセッションでクライアントを初期化する。
        セッションがなければパスワードログインにフォールバックする。
        """
        if self._client is not None:
            return

        session_json = self._sheets.get_garmin_session()
        if session_json:
            try:
                # verify_login=False でトークン検証APIコールを省略し429を回避する
                client = Garmin(self._email, self._password, verify_login=False)
                client.login(tokenstore=session_json)
                self._client = client
                logger.info("保存済みセッションで Garmin に接続しました")
                return
            except Exception as e:
                logger.warning("保存済みセッションでの接続失敗、再ログインします: %s", e)

        self._login()

    def _with_session_retry(self, func):
        """
        セッション切れ時に自動で再ログインしてリトライするデコレータ的ヘルパー。
        func は self._client を受け取る callable。
        """
        self._init_client()
        try:
            return func(self._client)
        except GarminConnectAuthenticationError:
            logger.warning("セッション切れを検出、再ログインします")
            self._login()
            return func(self._client)

    # ------------------------------------------------------------------
    # データ取得
    # ------------------------------------------------------------------

    def get_today_activities(self) -> list[dict]:
        """
        当日のアクティビティ一覧を返す。
        運動ゼロの日は空リストを返す（エラーではない）。
        """
        today = str(datetime.now(_JST).date())

        def _fetch(client: Garmin) -> list[dict]:
            activities = client.get_activities_by_date(today, today)
            logger.info("アクティビティ取得: %d 件 (%s)", len(activities), today)
            return activities

        return self._with_session_retry(_fetch)

    def _fetch_sleep_for_date(self, target_date: str) -> Optional[dict]:
        """指定日の睡眠データを取得する。未処理・未取得なら None。"""

        def _fetch(client: Garmin) -> Optional[dict]:
            try:
                data = client.get_sleep_data(target_date)
                if not data or "dailySleepDTO" not in data:
                    logger.info("睡眠データなし: %s", target_date)
                    return None
                dto = data["dailySleepDTO"]
                sleep_seconds = dto.get("sleepTimeSeconds", 0) or 0
                if sleep_seconds < 1800:
                    logger.info(
                        "睡眠データが未処理（sleepTimeSeconds=%d）: %s。次回リトライします",
                        sleep_seconds, target_date,
                    )
                    return None
                logger.info(
                    "睡眠データ取得完了: %s (%.1fh)", target_date, sleep_seconds / 3600
                )
                return dto
            except Exception as e:
                logger.warning("睡眠データ取得失敗 (%s): %s", target_date, e)
                return None

        return self._with_session_retry(_fetch)

    def get_last_night_sleep(self) -> Optional[dict]:
        """
        昨夜の睡眠データを返す（JST基準）。

        Garmin Connect は睡眠を「起床した日」の日付に紐づける。
        例: 6/9 夜に寝て 6/10 朝起きた睡眠 → date=2026-06-10 で取得する。
        """
        today = str(datetime.now(_JST).date())
        return self._fetch_sleep_for_date(today)

    def get_yesterday_sleep(self) -> Optional[dict]:
        """後方互換用。get_last_night_sleep() を呼ぶ。"""
        return self.get_last_night_sleep()

    # ------------------------------------------------------------------
    # データ整形
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_hr_zones(activity: dict) -> Optional[str]:
        """
        Garmin アクティビティ raw データから心拍 Zone 比率を抽出して文字列で返す。
        データが存在しない場合は None を返す。

        Garmin Connect API は Zone 情報を複数のフィールド名で返すことがある:
          - heartRateZones: list of {zoneLowBoundary, zoneHighBoundary, secsInZone, ...}
          - timeInHeartRateZone: 同様
          - hrTimeInZone_0 〜 hrTimeInZone_4: 各 Zone の秒数（古い形式）
        """
        # 新形式: heartRateZones / timeInHeartRateZone
        zones_data = activity.get("heartRateZones") or activity.get("timeInHeartRateZone")
        if zones_data and isinstance(zones_data, list):
            total_secs = sum(z.get("secsInZone", 0) or 0 for z in zones_data)
            if total_secs > 0:
                parts = []
                for i, z in enumerate(zones_data, start=1):
                    secs = z.get("secsInZone", 0) or 0
                    pct = round(secs / total_secs * 100)
                    if pct > 0:
                        parts.append(f"Z{i}:{pct}%")
                if parts:
                    return " ".join(parts)

        # 旧形式: hrTimeInZone_0 〜 hrTimeInZone_4
        zone_secs = []
        for i in range(5):
            v = activity.get(f"hrTimeInZone_{i}", 0) or 0
            zone_secs.append(float(v))
        total_secs = sum(zone_secs)
        if total_secs > 0:
            parts = []
            for i, secs in enumerate(zone_secs, start=1):
                pct = round(secs / total_secs * 100)
                if pct > 0:
                    parts.append(f"Z{i}:{pct}%")
            if parts:
                return " ".join(parts)

        return None

    @staticmethod
    def format_activity_summary(activity: dict) -> dict:
        """
        Garmin のアクティビティ raw データから通知・LLM分析に必要なフィールドを抽出する。
        """
        distance_m = activity.get("distance", 0) or 0
        duration_s = activity.get("duration", 0) or 0
        avg_hr = activity.get("averageHR", 0) or 0

        distance_km = round(distance_m / 1000, 2)
        if distance_m > 0 and duration_s > 0:
            pace_sec_per_km = duration_s / (distance_m / 1000)
            pace_min = int(pace_sec_per_km // 60)
            pace_sec = int(pace_sec_per_km % 60)
            avg_pace = f"{pace_min}'{pace_sec:02d}\""
        else:
            avg_pace = "N/A"

        result = {
            "activity_id": str(activity.get("activityId", "")),
            "activity_type": activity.get("activityType", {}).get("typeKey", "unknown"),
            "start_time": activity.get("startTimeLocal", ""),
            "distance_km": distance_km,
            "duration_min": round(duration_s / 60, 1) if duration_s > 0 else 0,
            "avg_pace": avg_pace,
            "avg_heart_rate": avg_hr,
            "calories": activity.get("calories", 0),
        }

        zones = GarminClient._extract_hr_zones(activity)
        if zones:
            result["heart_rate_zones"] = zones

        return result

    @staticmethod
    def format_sleep_summary(sleep_dto: dict) -> dict:
        """睡眠データから通知・LLM分析に必要なフィールドを抽出する。"""
        duration_s = sleep_dto.get("sleepTimeSeconds", 0) or 0
        return {
            "sleep_score": sleep_dto.get("sleepScores", {}).get("overall", {}).get("value", None),
            "sleep_hours": round(duration_s / 3600, 1),
            "deep_sleep_hours": round((sleep_dto.get("deepSleepSeconds", 0) or 0) / 3600, 1),
            "rem_sleep_hours": round((sleep_dto.get("remSleepSeconds", 0) or 0) / 3600, 1),
            "light_sleep_hours": round((sleep_dto.get("lightSleepSeconds", 0) or 0) / 3600, 1),
        }
