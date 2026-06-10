"""
EufyLife Cloud API から体重・体組成データを取得するクライアント。

認証フロー:
  - email/password でログインしてアクセストークンを取得
  - トークンは Sheets の session シートに保存（有効期限 ~30日）
  - トークン切れ（401）時は自動で再ログイン

注意: 非公式 API のため、EufyLife がエンドポイントを変更すると動作しなくなる可能性がある。
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://home-api.eufylife.com"
_LOGIN_CLIENT_ID = "eufy-app"
_LOGIN_CLIENT_SECRET = "8FHf22gaTKu7MZXqz5zytw"
_JST = timezone(timedelta(hours=9))


class EufyClient:
    def __init__(self, email: str, password: str, sheets, height_cm: Optional[float] = None):
        """
        email     : EufyLife アカウントのメールアドレス
        password  : EufyLife アカウントのパスワード
        sheets    : SheetsClient インスタンス（トークンの保存・読み込みに使用）
        height_cm : 身長（cm）。BMI が API から取得できない場合の計算に使用
        """
        self._email = email
        self._password = password
        self._sheets = sheets
        self._height_cm = height_cm
        self._access_token: Optional[str] = None
        self._request_host: Optional[str] = None

    # ------------------------------------------------------------------
    # 認証
    # ------------------------------------------------------------------

    def _login(self) -> None:
        """EufyLife にログインしてアクセストークンを取得・保存する。"""
        logger.info("EufyLife にログインします")
        resp = requests.post(
            f"{_BASE_URL}/v1/user/v2/email/login",
            headers={"category": "Health", "Content-Type": "application/json"},
            json={
                "client_id": _LOGIN_CLIENT_ID,
                "client_secret": _LOGIN_CLIENT_SECRET,
                "email": self._email,
                "password": self._password,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        self._request_host = data.get("user_info", {}).get("request_host") or _BASE_URL

        token_data = {
            "access_token": self._access_token,
            "refresh_token": data.get("refresh_token"),
            "request_host": self._request_host,
        }
        self._sheets.save_eufy_token(json.dumps(token_data))
        logger.info("EufyLife ログイン成功・トークン保存完了")

    def _init_token(self) -> None:
        """保存済みトークンを読み込む。なければ再ログインする。"""
        if self._access_token:
            return

        token_json = self._sheets.get_eufy_token()
        if token_json:
            try:
                token_data = json.loads(token_json)
                self._access_token = token_data["access_token"]
                self._request_host = token_data.get("request_host") or _BASE_URL
                logger.info("保存済み EufyLife トークンを読み込みました")
                return
            except Exception as e:
                logger.warning("保存済み EufyLife トークンの読み込み失敗: %s", e)

        self._login()

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET リクエストを送信する。401 時は再ログインしてリトライする。"""
        self._init_token()
        url = f"{self._request_host}{path}"
        headers = {"token": self._access_token}

        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 401:
            logger.warning("EufyLife トークン切れ、再ログインします")
            self._access_token = None
            self._login()
            headers = {"token": self._access_token}
            resp = requests.get(url, headers=headers, params=params, timeout=30)

        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # データ取得
    # ------------------------------------------------------------------

    def get_today_body_data(self) -> dict:
        """
        当日の体組成データを返す。

        返却フィールド:
          weight_kg        : 体重（kg）
          body_fat_pct     : 体脂肪率（%）
          bmi              : BMI
          lean_body_mass_kg: 除脂肪体重（kg）
        取得できない場合は各フィールドが None。
        """
        today_start = datetime.now(_JST).replace(hour=0, minute=0, second=0, microsecond=0)
        after_ts = int(today_start.astimezone(timezone.utc).timestamp())

        try:
            data = self._get("/v1/device/data", params={"after": after_ts})
        except Exception as e:
            logger.warning("EufyLife 体組成データ取得失敗: %s", e)
            return {"weight_kg": None, "body_fat_pct": None, "bmi": None, "lean_body_mass_kg": None}

        records = data.get("data", []) or data.get("records", []) or []
        if not records:
            logger.info("本日の EufyLife データなし")
            return {"weight_kg": None, "body_fat_pct": None, "bmi": None, "lean_body_mass_kg": None}

        # タイムスタンプで降順ソートして最新レコードを取得（API の並び順に依存しない）
        if isinstance(records, list) and len(records) > 1:
            def _ts(r: dict) -> int:
                return int(r.get("time") or r.get("timestamp") or r.get("measure_time") or 0)
            records = sorted(records, key=_ts, reverse=True)
        latest = records[0] if isinstance(records, list) else records

        weight = latest.get("weight") or latest.get("weight_kg")
        body_fat = (
            latest.get("body_fat")
            or latest.get("body_fat_pct")
            or latest.get("bodyfat")
            or latest.get("body_fat_percentage")
        )
        # BMI: 複数のキー名を試し、それでも取れない場合は身長から計算
        bmi = (
            latest.get("bmi")
            or latest.get("bmi_index")
            or latest.get("BMI")
        )
        if bmi is None and weight and self._height_cm:
            height_m = self._height_cm / 100
            bmi = round(weight / (height_m ** 2), 1)
            logger.info("BMI を身長（%.1f cm）と体重から計算: %.1f", self._height_cm, bmi)

        muscle_kg = latest.get("muscle") or latest.get("muscle_kg")

        # muscle_kg が取れない場合は体重×(1-体脂肪率) でフォールバック計算
        lean_body_mass = muscle_kg
        if lean_body_mass is None and weight and body_fat:
            lean_body_mass = round(weight * (1 - body_fat / 100), 1)

        for label, val, unit in [
            ("体重", weight, "kg"),
            ("体脂肪率", body_fat, "%"),
            ("BMI", bmi, ""),
            ("除脂肪体重", lean_body_mass, "kg"),
        ]:
            if val is not None:
                logger.info("%s取得: %.1f%s", label, val, unit)
            else:
                logger.info("本日の%sデータなし", label)

        return {
            "weight_kg": round(weight, 1) if weight is not None else None,
            "body_fat_pct": round(body_fat, 1) if body_fat is not None else None,
            "bmi": round(bmi, 1) if bmi is not None else None,
            "lean_body_mass_kg": round(lean_body_mass, 1) if lean_body_mass is not None else None,
        }
