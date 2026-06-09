"""
Gemini API を使った LLM 分析クライアント。
config.json で定義されたゴリラコーチのキャラクター・評価軸をシステムプロンプトに組み込む。
"""

import json
import logging
from pathlib import Path
from typing import Optional

import google.generativeai as genai

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
MODEL_NAME = "gemini-2.0-flash"


class GeminiClient:
    def __init__(self, api_key: str, config_path: Path = CONFIG_PATH):
        genai.configure(api_key=api_key)
        self._config = self._load_config(config_path)
        self._model = genai.GenerativeModel(MODEL_NAME)

    @staticmethod
    def _load_config(config_path: Path) -> dict:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        logger.debug("config.json を読み込みました: %s", config_path)
        return config

    # ------------------------------------------------------------------
    # プロンプト構築
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        role = self._config.get("role", "コーチ")
        tone = self._config.get("tone", "")
        metrics = self._config.get("focus_metrics", [])
        metrics_text = "\n".join(f"  - {m}" for m in metrics)

        return (
            f"あなたは「{role}」です。\n"
            f"コーチングスタイル: {tone}\n"
            f"\n"
            f"以下の評価軸に基づいてデータを分析し、具体的なフィードバックを日本語で提供してください:\n"
            f"{metrics_text}\n"
            f"\n"
            f"フィードバックの構成:\n"
            f"1. 今日の総評（2〜3文）\n"
            f"2. 良かった点（箇条書き）\n"
            f"3. 課題・改善点（箇条書き）\n"
            f"4. 明日へのアクションプラン（1〜2つ、具体的に）\n"
            f"\n"
            f"過去30日のトレンドも考慮して、連続日数・体重推移・睡眠スコアの変化に言及すること。\n"
            f"出力は LINE メッセージとして読みやすい長さ（400〜600文字目安）に収めること。"
        )

    @staticmethod
    def _format_today_data(today: dict, activities: list[dict]) -> str:
        """当日データをテキスト整形する。"""
        lines = [f"【本日 {today.get('date', '')} のデータ】"]

        if activities:
            lines.append(f"\nアクティビティ ({len(activities)}件):")
            for a in activities:
                lines.append(
                    f"  - {a.get('activity_type', '不明')} | "
                    f"距離: {a.get('distance_km', 0)} km | "
                    f"ペース: {a.get('avg_pace', 'N/A')} | "
                    f"心拍: {a.get('avg_heart_rate', 0)} bpm"
                )
        else:
            lines.append("\nアクティビティ: なし（休養日）")

        sleep_score = today.get("sleep_score", "")
        sleep_hours = today.get("sleep_hours", "")
        if sleep_score or sleep_hours:
            lines.append(f"\n睡眠スコア: {sleep_score} | 睡眠時間: {sleep_hours}時間")

        weight = today.get("weight_kg", "")
        body_fat = today.get("body_fat_pct", "")
        if weight:
            lines.append(f"体重: {weight} kg | 体脂肪率: {body_fat}%")

        ex_streak = today.get("consecutive_exercise_days", 0)
        rest_streak = today.get("consecutive_rest_days", 0)
        if ex_streak:
            lines.append(f"連続運動: {ex_streak}日")
        if rest_streak:
            lines.append(f"連続休養: {rest_streak}日")

        return "\n".join(lines)

    @staticmethod
    def _format_history(summaries: list[dict]) -> str:
        """過去30日分のサマリーをテキスト整形する。"""
        if not summaries:
            return "【過去データ】なし"

        lines = ["【過去30日のサマリー】"]
        for s in summaries[-7:]:  # 直近7日分を詳細表示
            lines.append(
                f"  {s.get('date', '')}: "
                f"距離{s.get('total_distance_km', 0)}km "
                f"体重{s.get('weight_kg', '-')}kg "
                f"睡眠スコア{s.get('sleep_score', '-')}"
            )

        weights = [s["weight_kg"] for s in summaries if s.get("weight_kg")]
        if len(weights) >= 2:
            diff = round(weights[-1] - weights[0], 1)
            sign = "+" if diff > 0 else ""
            lines.append(f"\n30日間の体重推移: {sign}{diff} kg")

        exercise_days = sum(1 for s in summaries if float(s.get("total_distance_km", 0) or 0) > 0)
        lines.append(f"30日間の運動日数: {exercise_days}日 / {len(summaries)}日")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # API 呼び出し
    # ------------------------------------------------------------------

    def analyze(
        self,
        today_summary: dict,
        today_activities: list[dict],
        history: list[dict],
    ) -> Optional[str]:
        """
        当日データと過去30日のサマリーを Gemini に送り、分析テキストを返す。
        エラー時は None を返す。
        """
        system_prompt = self._build_system_prompt()
        today_text = self._format_today_data(today_summary, today_activities)
        history_text = self._format_history(history)

        user_message = f"{today_text}\n\n{history_text}"

        full_prompt = f"{system_prompt}\n\n{user_message}"

        logger.info("Gemini API にリクエストを送信します（プロンプト: %d文字）", len(full_prompt))
        try:
            response = self._model.generate_content(full_prompt)
            result = response.text
            logger.info("Gemini 分析完了（レスポンス: %d文字）", len(result))
            return result
        except Exception as e:
            logger.error("Gemini API エラー: %s", e)
            return None
