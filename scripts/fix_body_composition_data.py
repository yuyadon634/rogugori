"""
過去の daily_summary シートに保存された体脂肪率・BMI・除脂肪体重の
×10 スケーリングミスを修正するワンショットスクリプト。

EufyLife API は体重と同様に、体脂肪率・BMI・除脂肪体重も
1/10 単位の整数（例: 250 = 25.0%）で返すことがある。
体重は正規化済みだが、他のフィールドが未正規化のままシートに保存された場合に
このスクリプトで修正する。

実行前に .env を確認し、接続先スプレッドシートを確かめること。
実行は冪等（既に正しい値は変更しない）。
"""

import sys
import os
import json

# プロジェクトルートを sys.path に追加
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

# service_account.json が存在すれば GOOGLE_SERVICE_ACCOUNT_JSON を自動注入する
_sa_path = os.path.join(_PROJECT_ROOT, "service_account.json")
if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") and os.path.exists(_sa_path):
    with open(_sa_path, encoding="utf-8") as _f:
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = json.dumps(json.load(_f))

from src.utils import build_sheets_client, load_env, setup_logging
from src.sheets_client import DAILY_SUMMARY_HEADERS

import logging

setup_logging()
logger = logging.getLogger(__name__)


# --- 妥当範囲の定義（eufy_client.py と同じ値）---
_BODY_FAT_MIN, _BODY_FAT_MAX = 3.0, 70.0
_BMI_MIN, _BMI_MAX = 10.0, 60.0
_WEIGHT_MIN, _WEIGHT_MAX = 20.0, 300.0


def _needs_scale_down(value, min_val: float, max_val: float) -> bool:
    """値が /10 すると妥当範囲に収まる場合 True を返す（＝×10 のまま保存されている）。"""
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    if f == 0:
        return False
    scaled = f / 10.0
    return min_val <= scaled <= max_val and not (min_val <= f <= max_val)


def fix_row(row: dict) -> dict | None:
    """
    1行分のデータを検査し、修正が必要なフィールドを修正した dict を返す。
    修正不要なら None を返す。
    """
    fixes = {}

    body_fat = row.get("body_fat_pct")
    if _needs_scale_down(body_fat, _BODY_FAT_MIN, _BODY_FAT_MAX):
        fixes["body_fat_pct"] = round(float(body_fat) / 10.0, 1)
        logger.info(
            "[%s] body_fat_pct: %s → %s",
            row.get("date"), body_fat, fixes["body_fat_pct"]
        )

    bmi = row.get("bmi")
    if _needs_scale_down(bmi, _BMI_MIN, _BMI_MAX):
        fixes["bmi"] = round(float(bmi) / 10.0, 1)
        logger.info(
            "[%s] bmi: %s → %s",
            row.get("date"), bmi, fixes["bmi"]
        )

    lean = row.get("lean_body_mass_kg")
    if _needs_scale_down(lean, _WEIGHT_MIN, _WEIGHT_MAX):
        fixes["lean_body_mass_kg"] = round(float(lean) / 10.0, 1)
        logger.info(
            "[%s] lean_body_mass_kg: %s → %s",
            row.get("date"), lean, fixes["lean_body_mass_kg"]
        )

    if not fixes:
        return None

    updated = dict(row)
    updated.update(fixes)
    return updated


def main() -> None:
    logger.info("===== body composition データ修正スクリプト開始 =====")
    env = load_env()
    sheets = build_sheets_client(env)

    ws = sheets._daily_ws
    records = ws.get_all_records()

    fix_count = 0
    for i, row in enumerate(records):
        updated = fix_row(row)
        if updated is None:
            continue

        row_index = i + 2  # ヘッダー行 + 1-indexed
        values = [updated.get(h, "") for h in DAILY_SUMMARY_HEADERS]
        ws.update(range_name=f"A{row_index}", values=[values])
        fix_count += 1
        logger.info("[%s] 修正完了", row.get("date"))

    if fix_count == 0:
        logger.info("修正が必要な行はありませんでした。データは正常です。")
    else:
        logger.info("合計 %d 行を修正しました。", fix_count)

    logger.info("===== 修正スクリプト終了 =====")


if __name__ == "__main__":
    main()
