"""
明らかに不正な体組成データ（同期エラー由来）を Sheets から除去するスクリプト。

判定基準（成人として絶対あり得ない値を除去）:
  - weight_kg     : 30 kg 未満
  - bmi           : 10 未満
  - body_fat_pct  : 1% 未満 または 90% 超
  - lean_body_mass_kg : 15 kg 未満
"""

import sys
import os
import json

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

_sa_path = os.path.join(_PROJECT_ROOT, "service_account.json")
if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") and os.path.exists(_sa_path):
    with open(_sa_path, encoding="utf-8") as f:
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = json.dumps(json.load(f))

from src.utils import build_sheets_client, load_env, setup_logging
from src.sheets_client import DAILY_SUMMARY_HEADERS

import logging
setup_logging()
logger = logging.getLogger(__name__)

BODY_FIELDS = {
    "weight_kg":          lambda v: float(v) < 30,
    "bmi":                lambda v: float(v) < 10,
    "body_fat_pct":       lambda v: float(v) < 1 or float(v) > 90,
    "lean_body_mass_kg":  lambda v: float(v) < 15,
}


def clear_invalid(row: dict) -> dict | None:
    fixes = {}
    for field, is_invalid in BODY_FIELDS.items():
        val = row.get(field)
        if val in (None, "", 0, "0"):
            continue
        try:
            if is_invalid(val):
                fixes[field] = ""
                logger.info("[%s] %s: %s → (クリア)", row.get("date"), field, val)
        except (TypeError, ValueError):
            pass

    if not fixes:
        return None

    updated = dict(row)
    updated.update(fixes)
    return updated


def main() -> None:
    logger.info("===== 不正体組成データ除去スクリプト開始 =====")
    env = load_env()
    sheets = build_sheets_client(env)

    ws = sheets._daily_ws
    records = ws.get_all_records()

    fix_count = 0
    for i, row in enumerate(records):
        updated = clear_invalid(row)
        if updated is None:
            continue

        row_index = i + 2
        values = [updated.get(h, "") for h in DAILY_SUMMARY_HEADERS]
        ws.update(range_name=f"A{row_index}", values=[values])
        fix_count += 1
        logger.info("[%s] クリア完了", row.get("date"))

    if fix_count == 0:
        logger.info("不正な値はありませんでした。")
    else:
        logger.info("合計 %d 行の不正値を除去しました。", fix_count)

    logger.info("===== 除去スクリプト終了 =====")


if __name__ == "__main__":
    main()
