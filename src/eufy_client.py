"""
EufyLife Cloud API から体重・体組成データを取得するクライアント。

認証フロー:
  - email/password でログインしてアクセストークン・user_id を取得
  - トークンは Sheets の session シートに保存（有効期限 ~30日）
  - トークン切れ（401 または res_code != 1）時は自動で再ログイン

レスポンス構造（reverse engineering 済み・2026 時点）:
  GET /v1/device/data -> { res_code: 1, data: [ DeviceRecord, ... ] }
  DeviceRecord = {
    id, device_id, customer_id, create_time(unix秒), scale_data: {...}, ...
  }
  scale_data = {
    weight(×10 された整数, 例 750=75.0kg), bmi, body_fat, muscle,
    muscle_mass, bmr, bone_mass, water, body_age, fat_free_weight,
    heart_rate, height, ...
  }
  ※ 計測値はトップレベルではなく scale_data の中にある点に注意。

注意: 非公式 API のため、EufyLife がエンドポイントを変更すると動作しなくなる可能性がある。
また、EufyLife クラウドはスマホアプリを開いて同期した後にデータが反映される。
体重計に乗っただけではクラウドに上がらない場合がある。
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

# 妥当な成人体重の範囲（kg）。スケーリング判定に使用。
_MIN_PLAUSIBLE_WEIGHT = 20.0
_MAX_PLAUSIBLE_WEIGHT = 300.0

# 体脂肪率（%）の妥当範囲
_MIN_PLAUSIBLE_BODY_FAT = 3.0
_MAX_PLAUSIBLE_BODY_FAT = 70.0

# BMI の妥当範囲
_MIN_PLAUSIBLE_BMI = 10.0
_MAX_PLAUSIBLE_BMI = 60.0

# EufyLife API の認証エラーを示す res_code 群。これ以外の非 1 コードは
# トークン切れと無関係（データなし・パラメータ不正など）なので再ログインしない。
# 実測値が増えたらこのセットに追加すること。
_AUTH_ERROR_RES_CODES = {10002, 10003, 10004, 10010}


def _to_float(value) -> Optional[float]:
    """数値に変換できれば float、できなければ None を返す。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _null_if_zero(value) -> Optional[float]:
    """0 / None / 数値変換不可なら None、それ以外は float を返す。"""
    f = _to_float(value)
    if f is None or f == 0:
        return None
    return f


def _normalize_by_range(raw, min_val: float, max_val: float) -> Optional[float]:
    """
    EufyLife API が 1/10 単位の整数で返す可能性がある値を正規化する汎用関数。
    まず /10 した値が妥当範囲内かチェックし、そうでなければ生値を使う。
    どちらも範囲外の場合は /10 値を返す（ログで気付けるように）。
    """
    f = _to_float(raw)
    if f is None or f == 0:
        return None

    scaled = f / 10.0
    if min_val <= scaled <= max_val:
        return scaled
    if min_val <= f <= max_val:
        return f
    return scaled


def _normalize_weight(raw) -> Optional[float]:
    """
    scale_data.weight を kg に正規化する。
    API は体重を 1/10 kg 単位の整数（例: 750 = 75.0kg）で返すことが多いが、
    将来 float 直値で返す可能性もあるため妥当範囲チェックでフォールバックする。
    """
    return _normalize_by_range(raw, _MIN_PLAUSIBLE_WEIGHT, _MAX_PLAUSIBLE_WEIGHT)


def _normalize_body_fat(raw) -> Optional[float]:
    """体脂肪率（%）を正規化する。API が ×10 整数で返す場合も考慮する。"""
    return _normalize_by_range(raw, _MIN_PLAUSIBLE_BODY_FAT, _MAX_PLAUSIBLE_BODY_FAT)


def _normalize_bmi(raw) -> Optional[float]:
    """BMI を正規化する。API が ×10 整数で返す場合も考慮する。"""
    return _normalize_by_range(raw, _MIN_PLAUSIBLE_BMI, _MAX_PLAUSIBLE_BMI)


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
        self._user_id: Optional[str] = None
        self._request_host: Optional[str] = None

    # ------------------------------------------------------------------
    # 認証
    # ------------------------------------------------------------------

    def _login(self) -> None:
        """EufyLife にログインしてアクセストークン・user_id を取得・保存する。"""
        logger.info("EufyLife にログインします")
        resp = requests.post(
            f"{_BASE_URL}/v1/user/v2/email/login",
            headers={
                "category": "Health",
                "Content-Type": "application/json",
                "User-Agent": "EufyLife-iOS-3.3.7",
            },
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

        if data.get("res_code") != 1 or not data.get("access_token"):
            msg = data.get("message", "不明なエラー")
            raise RuntimeError(f"EufyLife ログイン失敗: {msg}")

        self._access_token = data["access_token"]
        self._user_id = data.get("user_id") or data.get("user_info", {}).get("id")
        self._request_host = data.get("user_info", {}).get("request_host") or _BASE_URL

        token_data = {
            "access_token": self._access_token,
            "refresh_token": data.get("refresh_token"),
            "user_id": self._user_id,
            "request_host": self._request_host,
        }
        self._sheets.save_eufy_token(json.dumps(token_data))
        logger.info("EufyLife ログイン成功・トークン保存完了 (user_id=%s)", self._user_id)

    def _init_token(self) -> None:
        """保存済みトークンを読み込む。なければ再ログインする。"""
        if self._access_token:
            return

        token_json = self._sheets.get_eufy_token()
        if token_json:
            try:
                token_data = json.loads(token_json)
                self._access_token = token_data["access_token"]
                self._user_id = token_data.get("user_id")
                self._request_host = token_data.get("request_host") or _BASE_URL
                logger.info("保存済み EufyLife トークンを読み込みました (user_id=%s)", self._user_id)
                return
            except Exception as e:
                logger.warning("保存済み EufyLife トークンの読み込み失敗: %s", e)

        self._login()

    def _auth_headers(self) -> dict:
        headers = {
            "token": self._access_token,
            "User-Agent": "EufyLife-iOS-3.3.7",
        }
        if self._user_id:
            headers["uid"] = self._user_id
        return headers

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """
        GET リクエストを送信する。
        HTTP 401 または res_code が認証エラー（_AUTH_ERROR_RES_CODES）を示す場合のみ
        再ログインしてリトライする。データなし・パラメータ不正などは再ログインしない。
        """
        self._init_token()
        url = f"{self._request_host}{path}"

        resp = requests.get(url, headers=self._auth_headers(), params=params, timeout=30)

        need_relogin = resp.status_code == 401
        if not need_relogin and resp.status_code == 200:
            try:
                body = resp.json()
                rc = body.get("res_code")
                if rc != 1:
                    if rc in _AUTH_ERROR_RES_CODES:
                        logger.warning(
                            "EufyLife 認証エラー応答: res_code=%s message=%s → 再ログインします",
                            rc,
                            body.get("message"),
                        )
                        need_relogin = True
                    else:
                        raise RuntimeError(
                            f"EufyLife API エラー: res_code={rc} "
                            f"message={body.get('message')}"
                        )
            except ValueError:
                pass

        if need_relogin:
            logger.warning("EufyLife トークン切れの可能性、再ログインします")
            self._access_token = None
            self._user_id = None
            self._login()
            # _login() で self._request_host が更新される可能性があるため URL を再構築する
            url = f"{self._request_host}{path}"
            resp = requests.get(url, headers=self._auth_headers(), params=params, timeout=30)

        resp.raise_for_status()
        body = resp.json()
        if body.get("res_code") != 1:
            raise RuntimeError(
                f"EufyLife API エラー: res_code={body.get('res_code')} "
                f"message={body.get('message')}"
            )
        return body

    # ------------------------------------------------------------------
    # データ取得
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_result() -> dict:
        return {"weight_kg": None, "body_fat_pct": None, "bmi": None, "lean_body_mass_kg": None}

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
            return self._empty_result()

        records = data.get("data") or data.get("records") or []
        if not records:
            logger.info("本日の EufyLife データなし（アプリを開いてクラウド同期したか確認）")
            return self._empty_result()

        # create_time（unix秒）降順で最新レコードを取得（API の並び順に依存しない）
        if isinstance(records, list) and len(records) > 1:
            records = sorted(records, key=self._record_ts, reverse=True)
        latest = records[0] if isinstance(records, list) else records

        # 診断用: 取得したレコードの構造をログ出力（フィールド名ズレの早期発見用）
        logger.info(
            "EufyLife レコード取得: %d件 / 最新 create_time=%s customer_id=%s",
            len(records) if isinstance(records, list) else 1,
            latest.get("create_time"),
            latest.get("customer_id"),
        )
        scale_data = latest.get("scale_data")
        if isinstance(scale_data, dict):
            logger.info("scale_data フィールド: %s", list(scale_data.keys()))
        else:
            # scale_data が無い場合は旧構造とみなしレコード自体を計測元にする
            logger.warning(
                "scale_data が見つかりません。レコードのキー: %s",
                list(latest.keys()),
            )
            scale_data = latest

        result = self._parse_scale_data(scale_data)

        for label, val, unit in [
            ("体重", result["weight_kg"], "kg"),
            ("体脂肪率", result["body_fat_pct"], "%"),
            ("BMI", result["bmi"], ""),
            ("除脂肪体重", result["lean_body_mass_kg"], "kg"),
        ]:
            if val is not None:
                logger.info("%s取得: %.1f%s", label, val, unit)
            else:
                logger.info("本日の%sデータなし", label)

        return result

    @staticmethod
    def _record_ts(r: dict) -> int:
        for key in ("create_time", "time", "timestamp", "measure_time", "update_time"):
            v = r.get(key)
            if v:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
        return 0

    def _parse_scale_data(self, sd: dict) -> dict:
        """scale_data（またはフラットなレコード）から計測値を抽出・正規化する。"""
        weight = _normalize_weight(sd.get("weight") or sd.get("weight_kg"))

        body_fat = _normalize_body_fat(
            sd.get("body_fat")
            or sd.get("body_fat_pct")
            or sd.get("bodyfat")
            or sd.get("body_fat_percentage")
        )

        bmi = _normalize_bmi(sd.get("bmi") or sd.get("bmi_index") or sd.get("BMI"))
        if bmi is None and weight and self._height_cm:
            height_m = self._height_cm / 100
            bmi = round(weight / (height_m ** 2), 1)
            logger.info("BMI を身長（%.1f cm）と体重から計算: %.1f", self._height_cm, bmi)

        # 除脂肪体重（fat-free mass）= 体重 − 体脂肪量。
        # 優先順: fat_free_weight 直値 → 体重−body_fat_mass → 体重×(1-体脂肪率)。
        # ※ muscle_mass は筋肉量のみで除脂肪体重より小さくなるため使わない。
        lean_body_mass = _normalize_weight(
            sd.get("fat_free_weight") or sd.get("fat_free_weight_kg")
        )
        if lean_body_mass is None and weight:
            body_fat_mass = _normalize_weight(sd.get("body_fat_mass") or sd.get("body_fat_mass_kg"))
            if body_fat_mass is not None:
                lean_body_mass = round(weight - body_fat_mass, 1)
            elif body_fat:
                lean_body_mass = round(weight * (1 - body_fat / 100), 1)

        return {
            "weight_kg": round(weight, 1) if weight is not None else None,
            "body_fat_pct": round(body_fat, 1) if body_fat is not None else None,
            "bmi": round(bmi, 1) if bmi is not None else None,
            "lean_body_mass_kg": round(lean_body_mass, 1) if lean_body_mass is not None else None,
        }
