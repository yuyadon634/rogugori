"""
Render.com で常時起動する LINE Webhook 中継サーバー。

役割:
  1. LINE から Webhook POST を受信する
  2. メッセージが「今日の分析」ボタン（postback or text）の場合、
     GitHub Actions の llm-analysis ワークフローを repository_dispatch で起動する
  3. その他のメッセージは無視する

セキュリティ:
  - LINE の署名検証（X-Line-Signature）を行い、不正リクエストを弾く
"""

import base64
import hashlib
import hmac
import logging
import os
import sys

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GITHUB_TOKEN = os.getenv("GH_PAT", "") or os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GH_REPO", "") or os.getenv("GITHUB_REPO", "")  # 例: "username/rogugori"

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
ANALYSIS_TRIGGER_TEXTS = {"今日の分析", "分析して", "analyze"}
ANALYSIS_POSTBACK_DATA = "action=llm_analysis"


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


def trigger_llm_analysis() -> bool:
    """
    GitHub Actions の llm-analysis ワークフローを repository_dispatch で起動する。
    force=true を渡し、llm_sent フラグに関わらず再分析させる。
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
        "client_payload": {"force": True},
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 204:
            logger.info("GitHub Actions llm-analysis ワークフローを起動しました")
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


# ------------------------------------------------------------------
# イベント判定
# ------------------------------------------------------------------

def is_analysis_request(event: dict) -> bool:
    """イベントが LLM 分析リクエストかどうかを判定する。"""
    event_type = event.get("type")

    if event_type == "postback":
        data = event.get("postback", {}).get("data", "")
        return data == ANALYSIS_POSTBACK_DATA

    if event_type == "message":
        msg = event.get("message", {})
        if msg.get("type") == "text":
            return msg.get("text", "").strip() in ANALYSIS_TRIGGER_TEXTS

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
        if is_analysis_request(event):
            logger.info("分析リクエストを受信しました")

            # 押下直後に「分析中」を即時返信（LINE の 5 秒タイムアウト内に応答させるため先に送る）
            reply_token = event.get("replyToken", "")
            reply_line(reply_token, "🔍 分析中ウホ！\nGarmin × Eufy × 睡眠を集計中…\n1〜2分後に結果を送るウホ！")

            success = trigger_llm_analysis()
            if not success:
                logger.error("分析ワークフローの起動に失敗しました")

    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    """Render.com のヘルスチェック用エンドポイント。"""
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
