"""
Garmin Connect に MFA 対応でログインし、セッションを Google Sheets に保存するスクリプト。

初回セットアップ時や、セッション切れが続く場合に手動で実行する:
    py -m src.auth_garmin
"""

import json
import logging
import os
import sys

from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main() -> None:
    email = os.getenv("GARMIN_EMAIL", "")
    password = os.getenv("GARMIN_PASSWORD", "")
    google_service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID", "")

    if not email or not password:
        logger.error("GARMIN_EMAIL / GARMIN_PASSWORD が .env に設定されていません")
        sys.exit(1)

    # GOOGLE_SERVICE_ACCOUNT_JSON が未設定の場合は service_account.json から読み込む
    if not google_service_account_json:
        sa_path = os.path.join(os.path.dirname(__file__), "..", "service_account.json")
        if os.path.exists(sa_path):
            with open(sa_path, encoding="utf-8") as f:
                google_service_account_json = f.read()
            logger.info("service_account.json を読み込みました")
        else:
            logger.error("GOOGLE_SERVICE_ACCOUNT_JSON が未設定で service_account.json も見つかりません")
            sys.exit(1)

    if not spreadsheet_id:
        logger.error("GOOGLE_SHEETS_ID が .env に設定されていません")
        sys.exit(1)

    logger.info("Garmin に MFA 対応でログインします: %s", email)

    def prompt_mfa() -> str:
        return input("Garmin から届いた MFA コードを入力してください: ").strip()

    client = Garmin(email, password, prompt_mfa=prompt_mfa)
    client.login()

    # 新しいバージョンの garminconnect は client.client.dumps() でトークンを取得する
    session_json = client.client.dumps()
    logger.info("ログイン成功。セッションを Google Sheets に保存します...")

    from src.sheets_client import SheetsClient

    credentials_info = json.loads(google_service_account_json)
    sheets = SheetsClient(credentials_info, spreadsheet_id)
    sheets.save_garmin_session(session_json)

    logger.info("セッション保存完了！以降は GitHub Actions でも MFA なしでアクセスできます。")


if __name__ == "__main__":
    main()
