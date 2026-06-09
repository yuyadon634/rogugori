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


def main() -> None:
    logger.info("===== llm-analysis 開始 =====")
    try:
        env = load_env()
        sheets = build_sheets_client(env)

        # 重複送信防止チェック
        status = sheets.get_today_status()
        if status.get("llm_sent") in (True, "TRUE", "True", 1, "1"):
            logger.info("本日すでに LLM 分析を送信済みです。スキップします。")
            return

        # 当日サマリー取得
        today_str = str(date.today())
        today_summary = sheets.get_daily_summary(today_str) or {"date": today_str}

        # 当日アクティビティ（Garmin から再取得してフォーマット済みリストを作成）
        garmin = GarminClient(env["GARMIN_EMAIL"], env["GARMIN_PASSWORD"], sheets)
        activities_raw = garmin.get_today_activities()
        today_activities = [garmin.format_activity_summary(a) for a in activities_raw]

        # 過去30日サマリー取得
        history = sheets.get_recent_summaries(days=30)
        # 当日分は today_summary で別渡しするため除外
        history = [s for s in history if s.get("date") != today_str]

        logger.info(
            "分析データ: 当日アクティビティ %d件、履歴 %d日分",
            len(today_activities),
            len(history),
        )

        # Gemini 分析
        gemini = GeminiClient(env["GEMINI_API_KEY"])
        analysis_text = gemini.analyze(today_summary, today_activities, history)

        if analysis_text is None:
            logger.error("Gemini API からの分析結果が取得できませんでした")
            sys.exit(1)

        # LINE 送信
        line = LineClient(env["LINE_CHANNEL_ACCESS_TOKEN"], env["LINE_USER_ID"])
        line.send_llm_analysis(analysis_text)

        # 送信済みフラグを更新
        sheets.update_status({"llm_sent": True})
        logger.info("LLM 分析送信完了")

    except Exception as e:
        logger.exception("llm-analysis で予期せぬエラーが発生しました: %s", e)
        sys.exit(1)
    finally:
        logger.info("===== llm-analysis 終了 =====")


if __name__ == "__main__":
    main()
