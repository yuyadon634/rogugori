"""
data-sync ワークフローのエントリーポイント。
1時間ごとに GitHub Actions から実行され、以下の通知を担当する:
  - 睡眠レポート（06:00〜08:00）
  - アクティビティ通知（新規検出時に即時）
  - 体重通知（新規検出時に即時）
  - 休養日通知（23:00以降・当日アクティビティなし）

LLM 分析（analysis.py）は別ワークフローで行う。
"""

import json
import logging
import os
import sys
from datetime import date, datetime

from dotenv import load_dotenv

from src.garmin_client import GarminClient
from src.google_fit_client import GoogleFitClient
from src.line_client import LineClient
from src.sheets_client import SheetsClient

# ------------------------------------------------------------------
# ロギング設定
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("app.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def load_env() -> dict:
    load_dotenv()
    required = [
        "GARMIN_EMAIL",
        "GARMIN_PASSWORD",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_USER_ID",
        "GOOGLE_SHEETS_ID",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise EnvironmentError(f"必須の環境変数が未設定です: {missing}")
    return {k: os.getenv(k) for k in required}


def build_sheets_client(env: dict) -> SheetsClient:
    credentials_info = json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"])
    return SheetsClient(credentials_info, env["GOOGLE_SHEETS_ID"])


def build_google_fit_client(sheets: SheetsClient) -> GoogleFitClient | None:
    """
    Google Fit クライアントを初期化する。
    トークン情報は Sheets の session シートから読み込む。
    リフレッシュ時は Sheets に自動保存する。
    トークン未設定時は None を返す（体重通知はスキップされる）。
    """
    token_raw = _get_fit_token(sheets)
    credentials_raw = os.getenv("GOOGLE_FIT_CREDENTIALS_JSON", "{}")
    credentials_info = json.loads(credentials_raw)
    token_info = json.loads(token_raw) if token_raw else None

    if token_info is None:
        logger.warning("Google Fit トークン未設定。体重通知をスキップします。初回認証が必要です。")
        return None

    def on_refresh(new_token: dict) -> None:
        _save_fit_token(sheets, json.dumps(new_token))

    try:
        return GoogleFitClient(credentials_info, token_info, on_token_refresh=on_refresh)
    except Exception as e:
        logger.warning("Google Fit クライアント初期化失敗: %s", e)
        return None


def _get_fit_token(sheets: SheetsClient) -> str | None:
    """session シートから Google Fit トークンを取得する。"""
    records = sheets._session_ws.get_all_records()
    for row in records:
        if row.get("key") == "google_fit_token":
            v = row.get("value", "")
            return v if v else None
    return None


def _save_fit_token(sheets: SheetsClient, token_json: str) -> None:
    """session シートに Google Fit トークンを保存する。"""
    records = sheets._session_ws.get_all_records()
    for i, row in enumerate(records):
        if row.get("key") == "google_fit_token":
            row_index = i + 2
            sheets._session_ws.update(f"A{row_index}", [["google_fit_token", token_json]])
            return
    sheets._session_ws.append_row(["google_fit_token", token_json])


def calc_streaks(sheets: SheetsClient) -> tuple[int, int]:
    """
    過去30日のサマリーから連続運動日数・連続休養日数を計算して返す。
    Returns: (consecutive_exercise_days, consecutive_rest_days)
    """
    summaries = sheets.get_recent_summaries(days=30)
    today_str = str(date.today())

    exercise_streak = 0
    rest_streak = 0

    # 今日を除く直近の日付を降順で走査
    past = sorted(
        [s for s in summaries if s.get("date") != today_str],
        key=lambda s: s["date"],
        reverse=True,
    )

    for s in past:
        has_exercise = float(s.get("total_distance_km", 0) or 0) > 0
        if has_exercise:
            if rest_streak == 0:
                exercise_streak += 1
            else:
                break
        else:
            if exercise_streak == 0:
                rest_streak += 1
            else:
                break

    return exercise_streak, rest_streak


# ------------------------------------------------------------------
# 通知処理
# ------------------------------------------------------------------

def handle_sleep(
    garmin: GarminClient,
    line: LineClient,
    sheets: SheetsClient,
    status: dict,
) -> None:
    """06:00〜08:00 に睡眠レポートを1回送信する。"""
    now_hour = datetime.now().hour
    if not (6 <= now_hour < 8):
        return
    if status.get("sleep_sent") in (True, "TRUE", "True", 1, "1"):
        return

    sleep_raw = garmin.get_yesterday_sleep()
    if sleep_raw is None:
        logger.info("睡眠データが取得できなかったため睡眠レポートをスキップします")
        return

    sleep = garmin.format_sleep_summary(sleep_raw)
    _, rest_streak = calc_streaks(sheets)
    line.send_sleep_report(sleep, rest_streak)
    sheets.update_status({"sleep_sent": True})

    # daily_summary に睡眠データを反映
    today_summary = sheets.get_daily_summary(str(date.today())) or {"date": str(date.today())}
    today_summary.update({
        "sleep_score": sleep.get("sleep_score", ""),
        "sleep_hours": sleep.get("sleep_hours", ""),
    })
    sheets.upsert_daily_summary(today_summary)
    logger.info("睡眠レポート送信完了")


def handle_activities(
    garmin: GarminClient,
    line: LineClient,
    sheets: SheetsClient,
    status: dict,
) -> None:
    """新規アクティビティを検出して即時通知する。"""
    activities_raw = garmin.get_today_activities()
    notified_ids = sheets.get_notified_activity_ids()
    ex_streak, _ = calc_streaks(sheets)

    new_activities = [
        a for a in activities_raw
        if str(a.get("activityId", "")) not in notified_ids
    ]

    if not new_activities:
        logger.info("新規アクティビティなし")
        return

    total_distance = sum(
        (a.get("distance", 0) or 0) for a in activities_raw
    ) / 1000

    for activity_raw in new_activities:
        activity = garmin.format_activity_summary(activity_raw)
        # 連続運動日数を今日の運動があるので +1 して渡す
        line.send_activity_notification(activity, ex_streak + 1)
        sheets.add_notified_activity_id(activity["activity_id"])
        logger.info("アクティビティ通知送信: %s", activity["activity_id"])

    # daily_summary に集計値を反映
    today_summary = sheets.get_daily_summary(str(date.today())) or {"date": str(date.today())}
    avg_hr_values = [
        a.get("avg_heart_rate", 0) for a in [garmin.format_activity_summary(a) for a in activities_raw]
        if a.get("avg_heart_rate", 0)
    ]
    avg_hr = round(sum(avg_hr_values) / len(avg_hr_values), 1) if avg_hr_values else ""

    today_summary.update({
        "total_distance_km": round(total_distance, 2),
        "avg_heart_rate": avg_hr,
        "consecutive_exercise_days": ex_streak + 1,
        "consecutive_rest_days": 0,
    })
    sheets.upsert_daily_summary(today_summary)


def handle_weight(
    fit: GoogleFitClient,
    line: LineClient,
    sheets: SheetsClient,
    status: dict,
) -> None:
    """体重データを検出して即時通知する。"""
    if status.get("weight_sent") in (True, "TRUE", "True", 1, "1"):
        return

    body_data = fit.get_today_body_data()
    weight = body_data.get("weight_kg")
    body_fat = body_data.get("body_fat_pct")
    bmi = body_data.get("bmi")
    lean_body_mass = body_data.get("lean_body_mass_kg")

    if weight is None:
        logger.info("本日の体重データなし、スキップします")
        return

    line.send_weight_notification(weight, body_fat, bmi, lean_body_mass)
    sheets.update_status({"weight_sent": True})

    today_summary = sheets.get_daily_summary(str(date.today())) or {"date": str(date.today())}
    today_summary.update({
        "weight_kg": weight,
        "body_fat_pct": body_fat if body_fat is not None else "",
        "bmi": bmi if bmi is not None else "",
        "lean_body_mass_kg": lean_body_mass if lean_body_mass is not None else "",
    })
    sheets.upsert_daily_summary(today_summary)
    logger.info("体重通知送信完了")


def handle_rest_day(
    line: LineClient,
    sheets: SheetsClient,
    status: dict,
) -> None:
    """23:00以降・当日アクティビティなし・未通知の場合に休養日通知を送信する。"""
    now_hour = datetime.now().hour
    if now_hour < 23:
        return
    if status.get("rest_day_sent") in (True, "TRUE", "True", 1, "1"):
        return

    notified_ids = sheets.get_notified_activity_ids()
    if notified_ids:
        # 今日アクティビティがあれば休養日通知は不要
        return

    _, rest_streak = calc_streaks(sheets)
    rest_streak += 1  # 今日も休養

    line.send_rest_day_notification(rest_streak)
    sheets.update_status({"rest_day_sent": True})

    today_summary = sheets.get_daily_summary(str(date.today())) or {"date": str(date.today())}
    today_summary.update({
        "total_distance_km": 0,
        "consecutive_exercise_days": 0,
        "consecutive_rest_days": rest_streak,
    })
    sheets.upsert_daily_summary(today_summary)
    logger.info("休養日通知送信完了（連続%d日）", rest_streak)


# ------------------------------------------------------------------
# メインエントリーポイント
# ------------------------------------------------------------------

def main() -> None:
    logger.info("===== data-sync 開始 =====")
    try:
        env = load_env()
        sheets = build_sheets_client(env)
        garmin = GarminClient(env["GARMIN_EMAIL"], env["GARMIN_PASSWORD"], sheets)
        fit = build_google_fit_client(sheets)
        line = LineClient(env["LINE_CHANNEL_ACCESS_TOKEN"], env["LINE_USER_ID"])

        status = sheets.get_today_status()
        logger.info("本日のステータス: %s", status)

        handle_sleep(garmin, line, sheets, status)
        status = sheets.get_today_status()
        handle_activities(garmin, line, sheets, status)
        status = sheets.get_today_status()
        if fit is not None:
            handle_weight(fit, line, sheets, status)
            status = sheets.get_today_status()
        handle_rest_day(line, sheets, status)

    except Exception as e:
        logger.exception("data-sync で予期せぬエラーが発生しました: %s", e)
        sys.exit(1)
    finally:
        logger.info("===== data-sync 終了 =====")


if __name__ == "__main__":
    main()
