"""
Google Fit OAuth 初回認証スクリプト。
ローカルで一度だけ実行し、token.json を生成する。
生成された token.json の内容を GitHub Secrets に登録する。

使い方:
    python -m src.auth_google_fit
"""

import json
import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/fitness.body.read"]
CREDENTIALS_PATH = "credentials.json"
TOKEN_OUTPUT_PATH = "token.json"


def main():
    if not os.path.exists(CREDENTIALS_PATH):
        print(f"[ERROR] {CREDENTIALS_PATH} が見つかりません。")
        print("Google Cloud Console から OAuth クライアント認証情報をダウンロードして配置してください。")
        return

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else SCOPES,
    }

    with open(TOKEN_OUTPUT_PATH, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"[OK] {TOKEN_OUTPUT_PATH} を生成しました。")
    print("このファイルの内容を GitHub Secrets の GOOGLE_FIT_TOKEN_JSON に登録してください。")
    print("（ファイル自体は .gitignore に追加し、リポジトリにコミットしないこと）")


if __name__ == "__main__":
    main()
