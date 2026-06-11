import sys, os, json

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

_sa_path = os.path.join(_PROJECT_ROOT, "service_account.json")
if os.path.exists(_sa_path):
    with open(_sa_path, encoding="utf-8") as f:
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = json.dumps(json.load(f))

import logging
logging.disable(logging.CRITICAL)

from src.utils import build_sheets_client, load_env
env = load_env()
sheets = build_sheets_client(env)
summaries = sheets.get_recent_summaries(days=30)

print(f"days: {len(summaries)}")
for s in summaries:
    w = s.get("weight_kg") or "-"
    bf = s.get("body_fat_pct") or "-"
    bmi = s.get("bmi") or "-"
    print(f"{s.get('date')} | weight:{w} | body_fat:{bf} | bmi:{bmi}")
