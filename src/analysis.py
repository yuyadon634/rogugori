"""
llm-analysis ワークフローのエントリーポイント。
以下のタイミングで GitHub Actions から実行される:
  - 毎日 22:00（自動）
  - LINE の「今日の分析」ボタン経由（Render.com Webhook → repository_dispatch）

重複送信防止:
  status シートの llm_sent が True の場合は何もせず終了する。
"""

import json
import logging
import os
import sys
from datetime import date

from dotenv import load_dotenv

from src.garmin_client import GarminClient
from src.gemini_client import GeminiClient
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
        "GEMINI_API_KEY",
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


def _fetch_common_data(env: dict, sheets: SheetsClient) -> tuple:
    """当日サマリー・アクティビティ・過去履歴を取得して返す。"""
    today_str = str(date.today())
    today_summary = sheets.get_daily_summary(today_str) or {"date": today_str}

    garmin = GarminClient(env["GARMIN_EMAIL"], env["GARMIN_PASSWORD"], sheets)
    activities_raw = garmin.get_today_activities()
    today_activities = [garmin.format_activity_summary(a) for a in activities_raw]

    history = sheets.get_recent_summaries(days=30)
    history = [s for s in history if s.get("date") != today_str]

    logger.info(
        "分析データ: 当日アクティビティ %d件、履歴 %d日分",
        len(today_activities),
        len(history),
    )
    return today_summary, today_activities, history


def run_default_analysis(env: dict, sheets: SheetsClient, force: bool) -> None:
    """通常の日次レビュー分析を実行して LINE に送信する。"""
    status = sheets.get_today_status()
    if not force and status.get("llm_sent") in (True, "TRUE", "True", 1, "1"):
        logger.info("本日すでに LLM 分析を送信済みです。スキップします。")
        return

    today_summary, today_activities, history = _fetch_common_data(env, sheets)

    gemini = GeminiClient(env["GEMINI_API_KEY"])
    analysis = gemini.analyze(today_summary, today_activities, history)

    if analysis is None:
        logger.error("Gemini API からの分析結果が取得できませんでした")
        sys.exit(1)

    line = LineClient(env["LINE_CHANNEL_ACCESS_TOKEN"], env["LINE_USER_ID"])
    line.send_llm_analysis_flex(analysis)

    sheets.update_status({"llm_sent": True})
    logger.info("LLM 分析送信完了")


def run_tomorrow_plan(env: dict, sheets: SheetsClient) -> None:
    """翌日のトレーニングプランを生成して LINE に送信する。"""
    today_summary, today_activities, history = _fetch_common_data(env, sheets)

    gemini = GeminiClient(env["GEMINI_API_KEY"])
    plan = gemini.analyze_tomorrow_plan(today_summary, today_activities, history)

    if plan is None:
        logger.error("Gemini API からの翌日プラン生成に失敗しました")
        sys.exit(1)

    line = LineClient(env["LINE_CHANNEL_ACCESS_TOKEN"], env["LINE_USER_ID"])
    line.send_tomorrow_plan_flex(plan)
    logger.info("翌日プラン送信完了")


def main() -> None:
    logger.info("===== llm-analysis 開始 =====")

    mode = os.getenv("ANALYSIS_MODE", "default").strip().lower()
    force = os.getenv("FORCE_ANALYSIS", "false").lower() in ("true", "1", "yes")

    if force:
        logger.info("FORCE_ANALYSIS が有効です。llm_sent フラグを無視して再分析します。")
    logger.info("ANALYSIS_MODE: %s", mode)

    try:
        env = load_env()
        sheets = build_sheets_client(env)

        if mode == "tomorrow_plan":
            run_tomorrow_plan(env, sheets)
        else:
            run_default_analysis(env, sheets, force)

    except Exception as e:
        logger.exception("llm-analysis で予期せぬエラーが発生しました: %s", e)
        try:
            token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
            user_id = os.getenv("LINE_USER_ID", "")
            if token and user_id:
                error_line = LineClient(token, user_id)
                error_line._push(
                    f"⚠️ 分析中にエラーが発生したウホ…\n"
                    f"\n"
                    f"原因: {str(e)[:120]}\n"
                    f"\n"
                    f"しばらく待ってからもう一度試してみてウホ🦍"
                )
        except Exception as notify_err:
            logger.error("エラー通知の LINE 送信にも失敗: %s", notify_err)
        sys.exit(1)
    finally:
        logger.info("===== llm-analysis 終了 =====")


if __name__ == "__main__":
    main()
