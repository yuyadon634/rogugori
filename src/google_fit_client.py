"""
Google Fit REST API から体重・体脂肪データを取得するクライアント。

OAuth 2.0 フロー:
  - credentials_info (dict): Google Cloud の OAuth クライアント情報
  - token_info (dict | None): 保存済みトークン。存在すれば自動リフレッシュ。
  - 新規トークンが発行された場合は on_token_refresh コールバックで呼び出し元に通知し、
    呼び出し元（data_sync.py）が Sheets に保存する責務を持つ。
"""

import logging
from datetime import date, datetime, timezone
from typing import Callable, Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/fitness.body.read"]

# Google Fit データソース ID（Eufy スケールが Google Fit へ同期するデータタイプ）
DATA_SOURCE_WEIGHT = "derived:com.google.weight:com.google.android.gms:merge_weight"
DATA_SOURCE_BODY_FAT = "derived:com.google.body.fat.percentage:com.google.android.gms:merge_body_fat_percentage"
DATA_SOURCE_BMI = "derived:com.google.bmi:com.google.android.gms:merge_bmi_summary"
DATA_SOURCE_LEAN_BODY_MASS = "derived:com.google.lean_body_mass:com.google.android.gms:merge_lean_body_mass"


class GoogleFitClient:
    def __init__(
        self,
        credentials_info: dict,
        token_info: Optional[dict],
        on_token_refresh: Optional[Callable[[dict], None]] = None,
    ):
        """
        credentials_info  : OAuth クライアント情報 dict（client_id, client_secret 等）
        token_info        : 保存済みトークン dict。None の場合は初回認証が必要（CI環境では不可）
        on_token_refresh  : トークンが更新された際に呼び出されるコールバック (token_dict) -> None
        """
        self._credentials_info = credentials_info
        self._on_token_refresh = on_token_refresh
        self._service = self._build_service(token_info)

    def _build_service(self, token_info: Optional[dict]):
        if token_info is None:
            raise ValueError(
                "token_info が未設定です。初回認証が必要です。"
                "ローカルで src/auth_google_fit.py を実行してトークンを取得してください。"
            )

        creds = Credentials(
            token=token_info.get("token"),
            refresh_token=token_info.get("refresh_token"),
            token_uri=token_info.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=self._credentials_info.get("client_id"),
            client_secret=self._credentials_info.get("client_secret"),
            scopes=SCOPES,
        )

        if creds.expired and creds.refresh_token:
            logger.info("Google Fit トークンをリフレッシュします")
            creds.refresh(Request())
            if self._on_token_refresh:
                self._on_token_refresh(
                    {
                        "token": creds.token,
                        "refresh_token": creds.refresh_token,
                        "token_uri": creds.token_uri,
                        "client_id": creds.client_id,
                        "client_secret": creds.client_secret,
                        "scopes": list(creds.scopes) if creds.scopes else SCOPES,
                    }
                )
                logger.info("リフレッシュ済みトークンを保存しました")

        return build("fitness", "v1", credentials=creds, cache_discovery=False)

    # ------------------------------------------------------------------
    # データ取得
    # ------------------------------------------------------------------

    def _get_today_nanoseconds(self) -> tuple[int, int]:
        """当日の 0:00 〜 23:59:59 を UTC ナノ秒で返す。"""
        today = date.today()
        start_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=timezone.utc)
        return int(start_dt.timestamp() * 1e9), int(end_dt.timestamp() * 1e9)

    def _fetch_latest_data_point(self, data_source_id: str) -> Optional[float]:
        """
        指定したデータソースの当日最新値を返す。
        データが存在しない場合は None。
        """
        start_ns, end_ns = self._get_today_nanoseconds()
        try:
            result = (
                self._service.users()
                .dataSources()
                .datasets()
                .get(
                    userId="me",
                    dataSourceId=data_source_id,
                    datasetId=f"{start_ns}-{end_ns}",
                )
                .execute()
            )
        except Exception as e:
            logger.warning("Google Fit データ取得失敗 (%s): %s", data_source_id, e)
            return None

        points = result.get("point", [])
        if not points:
            return None

        # 最新の計測値を使う（endTimeNanos で降順ソート）
        latest = sorted(points, key=lambda p: int(p.get("endTimeNanos", 0)), reverse=True)[0]
        values = latest.get("value", [])
        if not values:
            return None

        # fpVal（浮動小数点）が体重・体脂肪に使われる
        return values[0].get("fpVal")

    def get_today_body_data(self) -> dict:
        """
        当日の体組成データを返す。Eufy スケールが Google Fit に同期した値を含む。

        返却フィールド:
          weight_kg       : 体重（kg）
          body_fat_pct    : 体脂肪率（%）
          bmi             : BMI
          lean_body_mass_kg: 除脂肪体重（kg）≒ 筋肉量の近似値
        取得できない場合は各フィールドが None。
        """
        weight = self._fetch_latest_data_point(DATA_SOURCE_WEIGHT)
        body_fat = self._fetch_latest_data_point(DATA_SOURCE_BODY_FAT)
        bmi = self._fetch_latest_data_point(DATA_SOURCE_BMI)
        lean_body_mass = self._fetch_latest_data_point(DATA_SOURCE_LEAN_BODY_MASS)

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
