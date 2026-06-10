"""
EufyLife API の動作確認・デバッグ用スクリプト。

EufyLife クラウドから実際に返ってくる生レスポンスを表示し、
フィールド名や値（特に scale_data の中身）を確認するために使う。
連携がうまくいかない時の切り分けに有用。

必要な環境変数 (.env):
  - EUFY_EMAIL
  - EUFY_PASSWORD
  - EUFY_HEIGHT_CM   (任意。BMI フォールバック計算用)

使い方:
  python scripts/test_eufy.py
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

_BASE_URL = "https://home-api.eufylife.com"
_CLIENT_ID = "eufy-app"
_CLIENT_SECRET = "8FHf22gaTKu7MZXqz5zytw"
_JST = timezone(timedelta(hours=9))


def login(email: str, password: str) -> dict:
    resp = requests.post(
        f"{_BASE_URL}/v1/user/v2/email/login",
        headers={
            "category": "Health",
            "Content-Type": "application/json",
            "User-Agent": "EufyLife-iOS-3.3.7",
        },
        json={
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "email": email,
            "password": password,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("res_code") != 1:
        raise RuntimeError(f"ログイン失敗: {data.get('message')}")
    return data


def main() -> None:
    email = os.getenv("EUFY_EMAIL")
    password = os.getenv("EUFY_PASSWORD")
    if not email or not password:
        print("EUFY_EMAIL / EUFY_PASSWORD が未設定です（.env を確認）")
        sys.exit(1)

    print("=== ログイン中 ===")
    login_data = login(email, password)
    token = login_data["access_token"]
    user_id = login_data.get("user_id") or login_data.get("user_info", {}).get("id")
    request_host = login_data.get("user_info", {}).get("request_host") or _BASE_URL
    print(f"ログイン成功 user_id={user_id} request_host={request_host}")

    # 登録デバイス・家族プロファイルの確認
    devices = login_data.get("devices", [])
    customers = login_data.get("customers", [])
    print(f"\n登録デバイス数: {len(devices)}")
    for d in devices:
        prod = d.get("product", {})
        print(f"  - {d.get('name')} / product_code={prod.get('product_code')} id={d.get('id')}")
    print(f"\n家族プロファイル数: {len(customers)}")
    for c in customers:
        print(
            f"  - {c.get('name')} id={c.get('id')} "
            f"height={c.get('height')} target_weight={c.get('target_weight')}"
        )

    headers = {"token": token, "User-Agent": "EufyLife-iOS-3.3.7"}
    if user_id:
        headers["uid"] = user_id

    # 直近30日分のデータを取得
    after_ts = int(
        (datetime.now(_JST) - timedelta(days=30)).astimezone(timezone.utc).timestamp()
    )
    print(f"\n=== /v1/device/data?after={after_ts}（直近30日） ===")
    resp = requests.get(
        f"{request_host}/v1/device/data",
        headers=headers,
        params={"after": after_ts},
        timeout=30,
    )
    print(f"HTTP {resp.status_code}")
    body = resp.json()
    print(f"res_code={body.get('res_code')} message={body.get('message')}")

    records = body.get("data") or []
    print(f"レコード件数: {len(records)}")

    if records:
        latest = records[0]
        print("\n--- 最新レコードの生 JSON ---")
        print(json.dumps(latest, indent=2, ensure_ascii=False))

        sd = latest.get("scale_data")
        if isinstance(sd, dict):
            raw_weight = sd.get("weight")
            print("\n--- scale_data 主要値 ---")
            print(f"  weight(raw)={raw_weight}  -> {raw_weight/10 if raw_weight else None} kg")
            print(f"  bmi={sd.get('bmi')}")
            print(f"  body_fat={sd.get('body_fat')}")
            print(f"  muscle_mass={sd.get('muscle_mass')}")
            print(f"  fat_free_weight={sd.get('fat_free_weight')}")
            print(f"  heart_rate={sd.get('heart_rate')}")
    else:
        print(
            "\nデータが0件です。考えられる原因:\n"
            "  1. スマホの EufyLife アプリを開いてクラウド同期していない\n"
            "  2. この30日間で計測していない\n"
            "  3. 別アカウント / 別の家族プロファイルで計測している"
        )


if __name__ == "__main__":
    main()
