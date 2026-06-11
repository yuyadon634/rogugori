"""
Render.com で常時起動する LINE Webhook 中継サーバー。

役割:
  1. LINE から Webhook POST を受信する
  2. メッセージが「今日の分析」ボタン（postback or text）の場合、
     GitHub Actions の llm-analysis ワークフローを repository_dispatch で起動する
  3. 「明日のメニューを詳しく」ボタンは mode=tomorrow_plan で同ワークフローを起動する
  4. 「今週の傾向を見る」ボタンは Google Sheets を直接参照して即時 Push 返信する
  5. その他のメッセージは無視する

セキュリティ:
  - LINE の署名検証（X-Line-Signature）を行い、不正リクエストを弾く
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import sys

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request

load_dotenv()

# webhook/ から src パッケージを import できるようにリポジトリルートを sys.path に追加する
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")
GITHUB_TOKEN = os.getenv("GH_PAT", "") or os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GH_REPO", "") or os.getenv("GITHUB_REPO", "")  # 例: "username/rogugori"
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
ANALYSIS_TRIGGER_TEXTS = {"今日の分析", "分析して", "analyze"}
WEIGHT_SYNC_TRIGGER_TEXTS = {"体重", "体重同期", "weight", "sync"}
ANALYSIS_POSTBACK_DATA = "action=llm_analysis"
TOMORROW_PLAN_POSTBACK_DATA = "action=tomorrow_plan"
WEEKLY_TREND_POSTBACK_DATA = "action=weekly_trend"
WEIGHT_SYNC_POSTBACK_DATA = "action=weight_sync"


# ------------------------------------------------------------------
# 署名検証
# ------------------------------------------------------------------

def verify_line_signature(body: bytes, signature: str) -> bool:
    """LINE Webhook の署名を検証する。"""
    if not LINE_CHANNEL_SECRET:
        logger.warning("LINE_CHANNEL_SECRET が未設定です。署名検証をスキップします（本番では設定必須）")
        return True
    expected = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected_b64 = base64.b64encode(expected).decode("utf-8")
    return hmac.compare_digest(expected_b64, signature)


# ------------------------------------------------------------------
# GitHub Actions トリガー
# ------------------------------------------------------------------

def reply_line(reply_token: str, text: str) -> None:
    """LINE Reply API でメッセージを即時返信する。"""
    if not reply_token:
        return
    try:
        requests.post(
            LINE_REPLY_URL,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": text}],
            },
            timeout=5,
        )
    except Exception as e:
        logger.warning("LINE 返信失敗: %s", e)


def trigger_data_sync(force_weight: bool = True) -> bool:
    """
    GitHub Actions の data-sync ワークフローを repository_dispatch で即時起動する。
    repository_dispatch は repo スコープの PAT で動作し、workflow スコープは不要。
    force_weight=True の場合、weight_sent フラグを無視して体重データを強制再取得する。
    成功時は True、失敗時は False を返す。
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.error("GITHUB_TOKEN または GITHUB_REPO が未設定です")
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/dispatches"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "event_type": "data_sync_trigger",
        "client_payload": {
            "force_weight": "true" if force_weight else "false",
        },
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 204:
            logger.info("GitHub Actions data-sync ワークフローを起動しました (force_weight=%s)", force_weight)
            return True
        else:
            logger.error(
                "GitHub Actions data-sync 起動失敗: status=%d body=%s",
                resp.status_code,
                resp.text,
            )
            return False
    except requests.RequestException as e:
        logger.error("GitHub Actions へのリクエストエラー: %s", e)
        return False


def trigger_llm_analysis(mode: str = "default") -> bool:
    """
    GitHub Actions の llm-analysis ワークフローを repository_dispatch で起動する。
    force=true を渡し、llm_sent フラグに関わらず再分析させる。
    mode: "default"（通常レビュー）または "tomorrow_plan"（翌日プラン）
    成功時は True、失敗時は False を返す。
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.error("GITHUB_TOKEN または GITHUB_REPO が未設定です")
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/dispatches"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "event_type": "llm_analysis_trigger",
        "client_payload": {"force": True, "mode": mode},
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 204:
            logger.info("GitHub Actions llm-analysis ワークフローを起動しました (mode=%s)", mode)
            return True
        else:
            logger.error(
                "GitHub Actions 起動失敗: status=%d body=%s",
                resp.status_code,
                resp.text,
            )
            return False
    except requests.RequestException as e:
        logger.error("GitHub Actions へのリクエストエラー: %s", e)
        return False


def push_line(text: str) -> None:
    """LINE Push API でユーザーにメッセージを送信する。"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        logger.warning("LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が未設定です")
        return
    try:
        requests.post(
            LINE_PUSH_URL,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "to": LINE_USER_ID,
                "messages": [{"type": "text", "text": text}],
            },
            timeout=5,
        )
    except Exception as e:
        logger.warning("LINE Push 送信失敗: %s", e)


def get_weekly_trend_text() -> str:
    """
    直近7日分のサマリーから即時返信用のテキストを生成して返す。
    Sheets アクセス・整形ロジックは SheetsClient に集約しており、ここは委譲のみ行う。
    Sheets 接続に失敗した場合はエラーメッセージを返す。
    """
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return "⚠️ Sheets の設定が未完了のため傾向を取得できなかったウホ…"

    try:
        from src.sheets_client import SheetsClient

        creds_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        sheets = SheetsClient(creds_info, GOOGLE_SHEETS_ID)
        return sheets.get_weekly_trend_text(days=7)
    except Exception as e:
        logger.error("週間傾向取得エラー: %s", e)
        return f"⚠️ 傾向の取得中にエラーが発生したウホ…\n原因: {str(e)[:80]}"


# ------------------------------------------------------------------
# イベント判定
# ------------------------------------------------------------------

def _get_postback_data(event: dict) -> str:
    """postback イベントの data 文字列を返す。"""
    return event.get("postback", {}).get("data", "")


def is_analysis_request(event: dict) -> bool:
    """イベントが LLM 分析リクエストかどうかを判定する。"""
    event_type = event.get("type")
    if event_type == "postback":
        return _get_postback_data(event) == ANALYSIS_POSTBACK_DATA
    if event_type == "message":
        msg = event.get("message", {})
        if msg.get("type") == "text":
            return msg.get("text", "").strip() in ANALYSIS_TRIGGER_TEXTS
    return False


def is_tomorrow_plan_request(event: dict) -> bool:
    """イベントが翌日プランリクエストかどうかを判定する。"""
    return (
        event.get("type") == "postback"
        and _get_postback_data(event) == TOMORROW_PLAN_POSTBACK_DATA
    )


def is_weekly_trend_request(event: dict) -> bool:
    """イベントが週間傾向リクエストかどうかを判定する。"""
    return (
        event.get("type") == "postback"
        and _get_postback_data(event) == WEEKLY_TREND_POSTBACK_DATA
    )


def is_weight_sync_request(event: dict) -> bool:
    """イベントが体重即時同期リクエストかどうかを判定する。"""
    event_type = event.get("type")
    if event_type == "postback":
        return _get_postback_data(event) == WEIGHT_SYNC_POSTBACK_DATA
    if event_type == "message":
        msg = event.get("message", {})
        if msg.get("type") == "text":
            return msg.get("text", "").strip() in WEIGHT_SYNC_TRIGGER_TEXTS
    return False


# ------------------------------------------------------------------
# Flask エンドポイント
# ------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_line_signature(body, signature):
        logger.warning("署名検証失敗: 不正なリクエストを拒否しました")
        abort(400)

    try:
        payload = request.get_json(force=True)
    except Exception:
        logger.warning("JSON パースに失敗しました")
        abort(400)

    events = payload.get("events", [])
    for event in events:
        reply_token = event.get("replyToken", "")

        if is_analysis_request(event):
            logger.info("分析リクエストを受信しました")
            reply_line(reply_token, "🔍 分析中ウホ！\nGarmin × Eufy × 睡眠を集計中…\n1〜2分後に結果を送るウホ！")
            if not trigger_llm_analysis(mode="default"):
                logger.error("分析ワークフローの起動に失敗しました")

        elif is_tomorrow_plan_request(event):
            logger.info("翌日プランリクエストを受信しました")
            reply_line(reply_token, "🏃 明日のメニューを生成中ウホ！\n今日の疲労度・体重・睡眠を分析中…\n1〜2分後に結果を送るウホ！")
            if not trigger_llm_analysis(mode="tomorrow_plan"):
                logger.error("翌日プランワークフローの起動に失敗しました")

        elif is_weekly_trend_request(event):
            logger.info("週間傾向リクエストを受信しました")
            # ① まず Sheets の生データを即時返信して体感速度を改善する
            trend_text = get_weekly_trend_text()
            reply_line(reply_token, trend_text)
            # ② Gemini による詳細分析は GitHub Actions で非同期実行（1〜2分後に Flex Message で追送）
            if not trigger_llm_analysis(mode="weekly_trend"):
                logger.error("週間傾向ワークフローの起動に失敗しました")

        elif is_weight_sync_request(event):
            logger.info("体重即時同期リクエストを受信しました")
            reply_line(reply_token, "⚖️ Eufy から体重データを今すぐ取得するウホ！\n30秒〜1分後に結果を送るウホ🦍")
            if not trigger_data_sync(force_weight=True):
                logger.error("data-sync ワークフローの起動に失敗しました")

    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    """Render.com のヘルスチェック用エンドポイント。"""
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
