"""
Gemini API を使った LLM 分析クライアント。
config.json で定義されたゴリラコーチのキャラクター・評価軸をシステムプロンプトに組み込む。
"""

import json
import logging
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
MODEL_NAME = "gemini-2.5-flash"

# analyze() の戻り値スキーマ
_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "good_points": {"type": "array", "items": {"type": "string"}},
        "issues": {"type": "array", "items": {"type": "string"}},
        "action_plan": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "good_points", "issues", "action_plan"],
}

# analyze_tomorrow_plan() の戻り値スキーマ
_TOMORROW_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "menu": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
        "caution": {"type": "string"},
    },
    "required": ["headline", "menu", "rationale"],
}


class GeminiClient:
    def __init__(self, api_key: str, config_path: Path = CONFIG_PATH):
        self._client = genai.Client(api_key=api_key)
        self._config = self._load_config(config_path)

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
            f"入力データの構成:\n"
            f"  - Garmin: アクティビティ（距離・ペース・心拍・Zone比率）\n"
            f"  - Garmin: 睡眠（スコア・睡眠時間・深睡眠・REM）\n"
            f"  - Eufy スケール: 体重・体脂肪率・BMI・除脂肪体重\n"
            f"\n"
            f"以下の評価軸に基づいてデータを分析し、具体的なフィードバックを日本語で提供してください:\n"
            f"{metrics_text}\n"
            f"\n"
            f"各フィードバックで必ず触れること（各項目は1〜2文で簡潔に）:\n"
            f"- 心拍: 平均心拍とZone2基準（145bpm）の乖離。heart_rate_zonesデータがある場合はZone比率も言及。疲労兆候・有酸素能力トレンドに触れる\n"
            f"- ペース: サブ4目標（5:41/km）との差（秒/km単位）・直近7日推移・ペース心拍バランス\n"
            f"- BMI: ランニングパフォーマンスへの影響（体重1kg減≒フルマラソン2〜3分短縮を目安に具体的な秒数で示す）\n"
            f"\n"
            f"過去30日のトレンドも考慮して、連続日数・体重推移・体脂肪率・BMI・除脂肪体重の変化・睡眠スコアの変化に言及すること。\n"
            f"\n"
            f"以下のJSON形式のみで出力すること（各フィールドは簡潔に、summary は2〜3文、リストは各2〜4項目）:\n"
            f"summary: 今日の総評\n"
            f"good_points: 良かった点のリスト\n"
            f"issues: 課題・改善点のリスト\n"
            f"action_plan: 明日へのアクションプランのリスト（具体的に）"
        )

    def _build_tomorrow_plan_prompt(self) -> str:
        role = self._config.get("role", "コーチ")
        tone = self._config.get("tone", "")

        return (
            f"あなたは「{role}」です。\n"
            f"コーチングスタイル: {tone}\n"
            f"\n"
            f"以下のデータ（当日の疲労状態・体重・睡眠スコア・直近の運動履歴）をもとに、\n"
            f"明日の具体的なトレーニングメニューを提案してください。\n"
            f"\n"
            f"提案の方針:\n"
            f"- サブ4目標（フルマラソン4時間以内、ペース5:41/km）に向けたトレーニング\n"
            f"- Zone2（145bpm以下）有酸素走を基本とし、疲労度に応じてメニューを調整\n"
            f"- 連続休養が2日以上なら積極的な運動を推奨、連続運動が4日以上なら休養を検討\n"
            f"- 種目・距離・目標ペース・心拍ターゲットを具体的な数値で示す\n"
            f"\n"
            f"以下のJSON形式のみで出力すること:\n"
            f"headline: メニューの見出し（1文、ゴリラ口調）\n"
            f"menu: メニュー項目のリスト（種目・距離・ペース・心拍の4要素を含む、2〜4項目）\n"
            f"rationale: このメニューを選んだ理由（1〜2文）\n"
            f"caution: 注意事項（任意、1文）"
        )

    @staticmethod
    def _format_today_data(today: dict, activities: list[dict]) -> str:
        """当日データをテキスト整形する。"""
        lines = [f"【本日 {today.get('date', '')} のデータ】"]

        if activities:
            lines.append(f"\nアクティビティ ({len(activities)}件):")
            for a in activities:
                act_line = (
                    f"  - {a.get('activity_type', '不明')} | "
                    f"距離: {a.get('distance_km', 0)} km | "
                    f"ペース: {a.get('avg_pace', 'N/A')} | "
                    f"心拍: {a.get('avg_heart_rate', 0)} bpm"
                )
                zones = a.get("heart_rate_zones")
                if zones:
                    act_line += f" | Zone比率: {zones}"
                lines.append(act_line)
        else:
            lines.append("\nアクティビティ: なし（休養日）")

        sleep_score = today.get("sleep_score", "")
        sleep_hours = today.get("sleep_hours", "")
        if sleep_score or sleep_hours:
            lines.append(f"\n睡眠スコア: {sleep_score} | 睡眠時間: {sleep_hours}時間")

        weight = today.get("weight_kg", "")
        body_fat = today.get("body_fat_pct", "")
        bmi = today.get("bmi", "")
        lean = today.get("lean_body_mass_kg", "")
        if weight:
            eufy_parts = [f"体重: {weight} kg"]
            if body_fat:
                eufy_parts.append(f"体脂肪率: {body_fat}%")
            if bmi:
                eufy_parts.append(f"BMI: {bmi}")
            if lean:
                eufy_parts.append(f"除脂肪体重: {lean} kg")
            lines.append(f"【Eufy 体組成】 {' | '.join(eufy_parts)}")

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
            bmi_str = f" BMI:{s.get('bmi', '-')}" if s.get("bmi") else ""
            lines.append(
                f"  {s.get('date', '')}: "
                f"距離{s.get('total_distance_km', 0)}km "
                f"体重{s.get('weight_kg', '-')}kg "
                f"体脂肪{s.get('body_fat_pct', '-')}%"
                f"{bmi_str} "
                f"睡眠スコア{s.get('sleep_score', '-')}"
            )

        weights = [s["weight_kg"] for s in summaries if s.get("weight_kg")]
        if len(weights) >= 2:
            diff = round(float(weights[-1]) - float(weights[0]), 1)
            sign = "+" if diff > 0 else ""
            lines.append(f"\n30日間の体重推移: {sign}{diff} kg")

        body_fats = [s["body_fat_pct"] for s in summaries if s.get("body_fat_pct")]
        if len(body_fats) >= 2:
            diff = round(float(body_fats[-1]) - float(body_fats[0]), 1)
            sign = "+" if diff > 0 else ""
            lines.append(f"30日間の体脂肪率推移: {sign}{diff}%")

        bmis = [s["bmi"] for s in summaries if s.get("bmi")]
        if len(bmis) >= 2:
            diff = round(float(bmis[-1]) - float(bmis[0]), 1)
            sign = "+" if diff > 0 else ""
            lines.append(f"30日間のBMI推移: {sign}{diff}")

        exercise_days = sum(1 for s in summaries if float(s.get("total_distance_km", 0) or 0) > 0)
        lines.append(f"30日間の運動日数: {exercise_days}日 / {len(summaries)}日")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # API 呼び出し
    # ------------------------------------------------------------------

    def _call_json(self, prompt: str, schema: dict) -> Optional[dict]:
        """JSON スキーマ付きで Gemini を呼び出し、パース済み dict を返す。失敗時は None。"""
        logger.info("Gemini API にリクエストを送信します（プロンプト: %d文字）", len(prompt))
        try:
            response = self._client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
            result = json.loads(response.text)
            logger.info("Gemini JSON 分析完了")
            return result
        except Exception as e:
            logger.error("Gemini API エラー: %s", e)
            return None

    def analyze(
        self,
        today_summary: dict,
        today_activities: list[dict],
        history: list[dict],
    ) -> Optional[dict]:
        """
        当日データと過去30日のサマリーを Gemini に送り、分析結果の dict を返す。
        戻り値のキー: summary, good_points, issues, action_plan
        エラー時は None を返す。
        """
        system_prompt = self._build_system_prompt()
        today_text = self._format_today_data(today_summary, today_activities)
        history_text = self._format_history(history)
        full_prompt = f"{system_prompt}\n\n{today_text}\n\n{history_text}"
        return self._call_json(full_prompt, _ANALYSIS_SCHEMA)

    def analyze_tomorrow_plan(
        self,
        today_summary: dict,
        today_activities: list[dict],
        history: list[dict],
    ) -> Optional[dict]:
        """
        翌日のトレーニングプランを生成して dict で返す。
        戻り値のキー: headline, menu, rationale, caution（任意）
        エラー時は None を返す。
        """
        system_prompt = self._build_tomorrow_plan_prompt()
        today_text = self._format_today_data(today_summary, today_activities)
        history_text = self._format_history(history)
        full_prompt = f"{system_prompt}\n\n{today_text}\n\n{history_text}"
        return self._call_json(full_prompt, _TOMORROW_PLAN_SCHEMA)
