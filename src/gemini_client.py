"""
Gemini API を使った LLM 分析クライアント。
config.json で定義されたゴリラコーチのキャラクター・評価軸をシステムプロンプトに組み込む。
"""

import json
import logging
import re
from datetime import date
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
        "top_priority": {"type": "string"},
        "action_plan": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "good_points", "issues", "top_priority", "action_plan"],
}

# analyze_weekly_trend() の戻り値スキーマ
_WEEKLY_TREND_SCHEMA = {
    "type": "object",
    "properties": {
        "weekly_summary": {"type": "string"},
        "best_performance": {"type": "string"},
        "key_issue": {"type": "string"},
        "next_week_focus": {"type": "string"},
        "numeric_goal": {"type": "string"},
    },
    "required": ["weekly_summary", "best_performance", "key_issue", "next_week_focus", "numeric_goal"],
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

    @staticmethod
    def _build_race_context(config: dict) -> str:
        """config.json のレース情報から残り日数・トレーニングフェーズを計算して文字列で返す。"""
        race_date_str = config.get("race_date")
        if not race_date_str:
            return ""
        try:
            race_dt = date.fromisoformat(race_date_str)
        except ValueError:
            return ""

        days_left = (race_dt - date.today()).days
        if days_left < 0:
            return ""

        weeks_left = days_left // 7
        race_name = config.get("race_name", "目標レース")
        goal_time = config.get("goal_finish_time", "")
        goal_pace = config.get("goal_pace_per_km", "")

        if weeks_left <= 2:
            phase = "テーパー期（疲労抜き・強度低減が最優先）"
        elif weeks_left <= 4:
            phase = "レース前調整期（ペース感覚・スピード維持）"
        elif weeks_left <= 8:
            phase = "強化期（ロング走・閾値走でスタミナ構築）"
        elif weeks_left <= 16:
            phase = "ベース期（Zone2有酸素走で基礎体力向上）"
        else:
            phase = "オフ期（故障予防・リカバリー重視）"

        goal_str = f"  目標タイム: {goal_time}（ペース {goal_pace}/km）\n" if goal_time else ""
        return (
            f"【レースコンテキスト】\n"
            f"  {race_name}: {race_date_str}（残り {days_left}日 / {weeks_left}週）\n"
            f"{goal_str}"
            f"  現在のフェーズ: {phase}\n"
            f"  ※ フェーズに応じたアドバイスを必ず反映すること\n"
        )

    def _build_common_header(self) -> str:
        """役割・トーン・レースコンテキスト・Zone2評価基準の共通ヘッダーを返す。"""
        role = self._config.get("role", "コーチ")
        tone = self._config.get("tone", "")
        race_context = self._build_race_context(self._config)
        z2_threshold = self._config.get("zone2_threshold_bpm", 145)
        z2_target = self._config.get("zone2_target_pct", 70)

        return (
            f"あなたは「{role}」です。\n"
            f"コーチングスタイル: {tone}\n"
            f"\n"
            f"{race_context}"
            f"\n"
            f"【Zone2評価基準】Zone2上限: {z2_threshold}bpm\n"
            f"  - Zone2比率 {z2_target}%以上 → 有酸素効率良好（サブ4に最適な強度）\n"
            f"  - Zone2比率 50〜{z2_target - 1}% → やや強度高め（継続は疲労蓄積リスクあり）\n"
            f"  - Zone2比率 50%未満 → 強度過多（翌日の回復を優先すべき）\n"
        )

    def _build_system_prompt(self) -> str:
        metrics = self._config.get("focus_metrics", [])
        metrics_text = "\n".join(f"  - {m}" for m in metrics)
        z2_threshold = self._config.get("zone2_threshold_bpm", 145)
        z2_target = self._config.get("zone2_target_pct", 70)

        return (
            self._build_common_header()
            + f"\n"
            f"入力データの構成:\n"
            f"  - Garmin: アクティビティ（距離・ペース・心拍・Zone比率・所要時間・カロリー）\n"
            f"  - Garmin: 睡眠（スコア・合計睡眠時間・深睡眠・REM）\n"
            f"  - Eufy スケール: 体重・体脂肪率・BMI・除脂肪体重\n"
            f"  - 過去7日の日次サマリー（距離・ペース・心拍・体重・体脂肪・BMI・睡眠スコア）\n"
            f"  - 過去30日の体重・体脂肪・BMI・運動日数・ペース推移\n"
            f"\n"
            f"以下の評価軸に基づいてデータを分析し、具体的なフィードバックを日本語で提供してください:\n"
            f"{metrics_text}\n"
            f"\n"
            f"各フィードバックで必ず触れること（各項目は1〜2文で簡潔に）:\n"
            f"- 心拍: 平均心拍と{z2_threshold}bpmの乖離。Zone比率がある場合は上記基準で評価（良好/高め/過多）・疲労兆候・有酸素能力トレンドに触れる\n"
            f"- ペース: サブ4目標（5:41/km）との差（秒/km単位）・過去7日のペース推移（改善・悪化の傾向と変化幅）・ペース心拍バランス\n"
            f"- BMI: ランニングパフォーマンスへの影響（体重1kg減≒フルマラソン2〜3分短縮を目安に具体的な秒数で示す）\n"
            f"- 睡眠: 深睡眠・REM時間から疲労回復度を評価し、翌日の推奨トレーニング強度に言及\n"
            f"\n"
            f"過去30日のトレンドも考慮して、連続日数・体重推移・体脂肪率・BMI・除脂肪体重の変化・睡眠スコアの変化に言及すること。\n"
            f"\n"
            f"以下のJSON形式のみで出力すること（各フィールドは簡潔に、summary は2〜3文、リストは各2〜4項目）:\n"
            f"summary: 今日の総評\n"
            f"good_points: 良かった点のリスト\n"
            f"issues: 課題・改善点のリスト\n"
            f"top_priority: 今日のデータから最も改善すべき1点（15文字以内、体言止め）\n"
            f"action_plan: 明日へのアクションプランのリスト。以下の3原則を必ず守ること:\n"
            f"  1. issuesで指摘した課題のそれぞれに対応するアクションを最低1つ含めること（課題の放置は不可）\n"
            f"  2. 種目・距離・ペース・心拍の数値を含む具体的な内容で、現在のフェーズに合った内容にすること\n"
            f"  3. BMI・体重の改善に言及する場合は「今週-0.3kg」「月-1kg」など週次・月次の具体的な数値目標まで落とし込むこと"
        )

    def _build_weekly_trend_prompt(self) -> str:
        z2_target = self._config.get("zone2_target_pct", 70)

        return (
            self._build_common_header()
            + f"\n"
            f"以下の直近7日間のデータをもとに週間コーチングレポートを作成してください。\n"
            f"\n"
            f"分析の観点:\n"
            f"- 今週の運動量（総距離・運動日数）と強度（ペース・心拍・Zone比率）の総括\n"
            f"- 体重・体脂肪・BMI のトレンドとパフォーマンスへの影響\n"
            f"- 睡眠スコアの傾向と疲労回復の評価\n"
            f"- 来週に向けた重点テーマと具体的な数値目標（距離・ペース等）\n"
            f"- 現在のトレーニングフェーズに合った来週の方針\n"
            f"\n"
            f"以下のJSON形式のみで出力すること:\n"
            f"weekly_summary: 今週の総括（2〜3文、数値を交えて）\n"
            f"best_performance: 今週のベストパフォーマンス（1文、具体的な数値で）\n"
            f"key_issue: 今週の最重要課題（1文、15文字以内・体言止め）\n"
            f"next_week_focus: 来週の重点テーマ（1文、フェーズに合った内容で）\n"
            f"numeric_goal: 来週の数値目標（例: 合計30km走る、Zone2比率{z2_target}%以上を3回維持 など）"
        )

    def _build_tomorrow_plan_prompt(self) -> str:
        return (
            self._build_common_header()
            + f"\n"
            f"以下のデータ（当日の疲労状態・体重・睡眠スコア・直近の運動履歴）をもとに、\n"
            f"明日の具体的なトレーニングメニューを提案してください。\n"
            f"\n"
            f"提案の方針:\n"
            f"- 上記のレースコンテキスト（残り週数・フェーズ）を最優先で考慮してメニューを決定する\n"
            f"- サブ4目標（フルマラソン4時間以内、ペース5:41/km）に向けたトレーニング\n"
            f"- Zone2（145bpm以下）有酸素走を基本とし、疲労度に応じてメニューを調整\n"
            f"- 連続休養が2日以上なら積極的な運動を推奨、連続運動が4日以上なら休養を検討\n"
            f"- 種目・距離・目標ペース・心拍ターゲットを具体的な数値で示す\n"
            f"\n"
            f"以下のJSON形式のみで出力すること:\n"
            f"headline: メニューの見出し（1文、ゴリラ口調・残り週数への言及を入れると良い）\n"
            f"menu: メニュー項目のリスト（種目・距離・ペース・心拍の4要素を含む、2〜4項目）\n"
            f"rationale: このメニューを選んだ理由（1〜2文、フェーズとの関連を含める）\n"
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
                    f"時間: {a.get('duration_min', 0)} 分 | "
                    f"ペース: {a.get('avg_pace', 'N/A')} | "
                    f"心拍: {a.get('avg_heart_rate', 0)} bpm | "
                    f"カロリー: {a.get('calories', 0)} kcal"
                )
                zones = a.get("heart_rate_zones")
                if zones:
                    act_line += f" | Zone比率: {zones}"
                lines.append(act_line)
        else:
            lines.append("\nアクティビティ: なし（休養日）")

        sleep_score = today.get("sleep_score", "")
        sleep_hours = today.get("sleep_hours", "")
        deep_sleep = today.get("deep_sleep_hours", "")
        rem_sleep = today.get("rem_sleep_hours", "")
        if sleep_score or sleep_hours:
            sleep_line = f"\n睡眠スコア: {sleep_score} | 合計: {sleep_hours}時間"
            if deep_sleep:
                sleep_line += f" | 深睡眠: {deep_sleep}時間"
            if rem_sleep:
                sleep_line += f" | REM: {rem_sleep}時間"
            lines.append(sleep_line)

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
    def _pace_str_to_sec(pace_str: str) -> Optional[int]:
        """
        ペース文字列（例: "5'30\""）を秒/kmの整数に変換する。
        パース失敗時は None を返す。
        """
        if not pace_str or pace_str == "N/A":
            return None
        try:
            parts = re.findall(r"\d+", pace_str)
            if len(parts) >= 2:
                return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            pass
        return None

    @staticmethod
    def _format_history(summaries: list[dict]) -> str:
        """過去30日分のサマリーをテキスト整形する。"""
        if not summaries:
            return "【過去データ】なし"

        lines = ["【過去30日のサマリー】"]
        for s in summaries[-7:]:  # 直近7日分を詳細表示
            bmi_str  = f" BMI:{s.get('bmi', '-')}"                     if s.get("bmi")               else ""
            pace_str = f" ペース:{s.get('avg_pace_per_km', '-')}/km"   if s.get("avg_pace_per_km")   else ""
            hr_str   = f" 心拍:{s.get('avg_heart_rate', '-')}bpm"      if s.get("avg_heart_rate")    else ""
            lines.append(
                f"  {s.get('date', '')}: "
                f"距離{s.get('total_distance_km', 0)}km"
                f"{pace_str}{hr_str} "
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

        # 直近7日のペース推移を集計（運動日のみ）
        recent_paces = [
            (s["date"], GeminiClient._pace_str_to_sec(s.get("avg_pace_per_km", "")))
            for s in summaries[-7:]
            if s.get("avg_pace_per_km") and float(s.get("total_distance_km", 0) or 0) > 0
        ]
        recent_paces = [(d, p) for d, p in recent_paces if p is not None]
        if len(recent_paces) >= 2:
            oldest_sec = recent_paces[0][1]
            newest_sec = recent_paces[-1][1]
            diff_sec = newest_sec - oldest_sec
            trend = "改善" if diff_sec < 0 else "悪化" if diff_sec > 0 else "変化なし"
            sign = "+" if diff_sec > 0 else ""
            avg_sec = sum(p for _, p in recent_paces) // len(recent_paces)
            avg_min, avg_s = divmod(avg_sec, 60)
            lines.append(
                f"直近7日ペース推移: {sign}{diff_sec}秒/km（{trend}）"
                f" / 平均ペース {avg_min}'{avg_s:02d}\"/km（運動{len(recent_paces)}日分）"
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # API 呼び出し
    # ------------------------------------------------------------------

    def _call_json(self, system_prompt: str, user_data: str, schema: dict) -> Optional[dict]:
        """
        system_instruction と user_data を分離して Gemini を呼び出し、パース済み dict を返す。
        system_instruction にキャラ・評価ルールを渡すことでモデルが指示を確実に守るようにする。
        失敗時は None を返す。
        """
        logger.info(
            "Gemini API にリクエストを送信します（system: %d文字 / data: %d文字）",
            len(system_prompt),
            len(user_data),
        )
        try:
            response = self._client.models.generate_content(
                model=MODEL_NAME,
                contents=user_data,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
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
        戻り値のキー: summary, good_points, issues, top_priority, action_plan
        エラー時は None を返す。
        """
        system_prompt = self._build_system_prompt()
        today_text = self._format_today_data(today_summary, today_activities)
        history_text = self._format_history(history)
        user_data = f"{today_text}\n\n{history_text}"
        return self._call_json(system_prompt, user_data, _ANALYSIS_SCHEMA)

    @staticmethod
    def _format_weekly_data(summaries: list[dict]) -> str:
        """直近7日分のサマリーを週間レポート用に整形する。"""
        if not summaries:
            return "【今週のデータ】なし"

        lines = [f"【今週のデータ（{len(summaries)}日分）】"]
        for s in summaries:
            dist = float(s.get("total_distance_km", 0) or 0)
            run_label = f"ランニング {dist}km" if dist > 0 else "休養日"
            pace_str = f" ペース:{s.get('avg_pace_per_km', '-')}/km" if s.get("avg_pace_per_km") else ""
            hr_str   = f" 心拍:{s.get('avg_heart_rate', '-')}bpm"    if s.get("avg_heart_rate")   else ""
            weight_str = f" 体重:{s.get('weight_kg', '-')}kg"         if s.get("weight_kg")         else ""
            fat_str    = f" 体脂肪:{s.get('body_fat_pct', '-')}%"     if s.get("body_fat_pct")      else ""
            sleep_str  = f" 睡眠:{s.get('sleep_score', '-')}点"       if s.get("sleep_score")       else ""
            deep_str   = f"(深{s.get('deep_sleep_hours', '-')}h REM{s.get('rem_sleep_hours', '-')}h)" if s.get("deep_sleep_hours") else ""
            lines.append(
                f"  {s.get('date', '')}: {run_label}"
                f"{pace_str}{hr_str}{weight_str}{fat_str}{sleep_str}{deep_str}"
            )

        # 集計行
        total_dist = sum(float(s.get("total_distance_km", 0) or 0) for s in summaries)
        run_days = sum(1 for s in summaries if float(s.get("total_distance_km", 0) or 0) > 0)
        lines.append(f"\n合計距離: {round(total_dist, 1)}km / 運動日数: {run_days}/{len(summaries)}日")

        weights = [float(s["weight_kg"]) for s in summaries if s.get("weight_kg")]
        if len(weights) >= 2:
            diff = round(weights[-1] - weights[0], 1)
            sign = "+" if diff > 0 else ""
            lines.append(f"体重推移: {sign}{diff}kg（{weights[0]}kg → {weights[-1]}kg）")

        sleep_scores = [float(s["sleep_score"]) for s in summaries if s.get("sleep_score")]
        if sleep_scores:
            avg_sleep = round(sum(sleep_scores) / len(sleep_scores), 1)
            lines.append(f"平均睡眠スコア: {avg_sleep}点")

        return "\n".join(lines)

    def analyze_weekly_trend(self, history: list[dict]) -> Optional[dict]:
        """
        直近7日のサマリーを Gemini に送り、週間コーチングレポートを返す。
        戻り値のキー: weekly_summary, best_performance, key_issue, next_week_focus, numeric_goal
        エラー時は None を返す。
        """
        system_prompt = self._build_weekly_trend_prompt()
        weekly_data = self._format_weekly_data(history[-7:] if len(history) > 7 else history)
        return self._call_json(system_prompt, weekly_data, _WEEKLY_TREND_SCHEMA)

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
        user_data = f"{today_text}\n\n{history_text}"
        return self._call_json(system_prompt, user_data, _TOMORROW_PLAN_SCHEMA)
