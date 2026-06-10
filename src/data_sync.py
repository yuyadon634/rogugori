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
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

JST = timezone(timedelta(hours=9))

from src.eufy_client import EufyClient
from src.garmin_client import GarminClient
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


def build_eufy_client(env: dict, sheets: SheetsClient) -> EufyClient | None:
    """EufyClient を初期化する。認証情報未設定時は None（体重通知をスキップ）。"""
    email = env.get("EUFY_EMAIL") or os.getenv("EUFY_EMAIL")
    password = env.get("EUFY_PASSWORD") or os.getenv("EUFY_PASSWORD")
    if not email or not password:
        logger.warning("EufyLife 認証情報未設定。体重通知をスキップします。")
        return None
    height_raw = env.get("EUFY_HEIGHT_CM") or os.getenv("EUFY_HEIGHT_CM")
    height_cm: float | None = None
    if height_raw:
        try:
            height_cm = float(height_raw)
        except ValueError:
            logger.warning("EUFY_HEIGHT_CM の値が不正です（%s）。BMI 計算フォールバックを無効化します。", height_raw)
    return EufyClient(email, password, sheets, height_cm=height_cm)


def calc_streaks(sheets: SheetsClient) -> tuple[int, int]:
    """
    過去30日のサマリーから連続運動日数・連続休養日数を計算して返す。
    Returns: (consecutive_exercise_days, consecutive_rest_days)
    """
    summaries = sheets.get_recent_summaries(days=30)
    today_str = str(datetime.now(JST).date())

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
    """JST 04:00〜11:59 に睡眠レポートを1回送信する。
    FORCE_SLEEP=true の場合は時間窓チェックをスキップする。
    """
    force = os.getenv("FORCE_SLEEP", "false").lower() == "true"
    if not force:
        now_jst = datetime.now(JST)
        if not (4 <= now_jst.hour < 12):
            logger.info("睡眠レポートの時間窓外（JST %d時）のためスキップします", now_jst.hour)
            return
    if status.get("sleep_sent") in (True, "TRUE", "True", 1, "1"):
        return

    sleep_raw = garmin.get_last_night_sleep()
    if sleep_raw is None:
        logger.info("睡眠データが取得できなかったため睡眠レポートをスキップします")
        return

    sleep = garmin.format_sleep_summary(sleep_raw)
    _, rest_streak = calc_streaks(sheets)
    line.send_sleep_report(sleep, rest_streak)
    sheets.update_status({"sleep_sent": True})

    # daily_summary に睡眠データを反映
    today_jst = str(datetime.now(JST).date())
    today_summary = sheets.get_daily_summary(today_jst) or {"date": today_jst}
    today_summary.update({
        "sleep_score": sleep.get("sleep_score", ""),
        "sleep_hours": sleep.get("sleep_hours", ""),
        "deep_sleep_hours": sleep.get("deep_sleep_hours", ""),
        "rem_sleep_hours": sleep.get("rem_sleep_hours", ""),
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
    today_jst = str(datetime.now(JST).date())
    today_summary = sheets.get_daily_summary(today_jst) or {"date": today_jst}
    all_formatted = [garmin.format_activity_summary(a) for a in activities_raw]

    avg_hr_values = [a.get("avg_heart_rate", 0) for a in all_formatted if a.get("avg_heart_rate", 0)]
    avg_hr = round(sum(avg_hr_values) / len(avg_hr_values), 1) if avg_hr_values else ""

    # 最も距離の長いアクティビティのペースを代表値として記録する
    primary = max(all_formatted, key=lambda a: float(a.get("distance_km", 0) or 0), default=None)
    avg_pace = primary.get("avg_pace", "") if primary else ""

    today_summary.update({
        "total_distance_km": round(total_distance, 2),
        "avg_heart_rate": avg_hr,
        "avg_pace_per_km": avg_pace,
        "consecutive_exercise_days": ex_streak + 1,
        "consecutive_rest_days": 0,
    })
    sheets.upsert_daily_summary(today_summary)


def handle_weight(
    eufy: EufyClient,
    line: LineClient,
    sheets: SheetsClient,
    status: dict,
) -> None:
    """体重データを検出して即時通知する。
    FORCE_WEIGHT=true の場合は weight_sent フラグを無視して再取得する。
    """
    force = os.getenv("FORCE_WEIGHT", "false").lower() == "true"
    if not force and status.get("weight_sent") in (True, "TRUE", "True", 1, "1"):
        return

    body_data = eufy.get_today_body_data()
    weight = body_data.get("weight_kg")
    body_fat = body_data.get("body_fat_pct")
    bmi = body_data.get("bmi")
    lean_body_mass = body_data.get("lean_body_mass_kg")

    if weight is None:
        logger.info("本日の体重データなし、スキップします")
        return

    line.send_weight_notification(weight, body_fat, bmi, lean_body_mass)
    sheets.update_status({"weight_sent": True})

    today_jst = str(datetime.now(JST).date())
    today_summary = sheets.get_daily_summary(today_jst) or {"date": today_jst}
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
    """JST 23:00以降・当日アクティビティなし・未通知の場合に休養日通知を送信する。"""
    now_hour = datetime.now(JST).hour
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

    today_jst = str(datetime.now(JST).date())
    today_summary = sheets.get_daily_summary(today_jst) or {"date": today_jst}
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
        eufy = build_eufy_client(env, sheets)
        line = LineClient(env["LINE_CHANNEL_ACCESS_TOKEN"], env["LINE_USER_ID"])

        status = sheets.get_today_status()
        logger.info("本日のステータス: %s", status)

        handle_sleep(garmin, line, sheets, status)
        status = sheets.get_today_status()
        handle_activities(garmin, line, sheets, status)
        status = sheets.get_today_status()
        if eufy is not None:
            handle_weight(eufy, line, sheets, status)
        status = sheets.get_today_status()
        handle_rest_day(line, sheets, status)

    except Exception as e:
        logger.exception("data-sync で予期せぬエラーが発生しました: %s", e)
        sys.exit(1)
    finally:
        logger.info("===== data-sync 終了 =====")


if __name__ == "__main__":
    main()
