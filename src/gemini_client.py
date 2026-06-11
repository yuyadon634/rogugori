"""
Gemini API を使った LLM 分析クライアント。
config.json で定義されたゴリラコーチのキャラクター・評価軸をシステムプロンプトに組み込む。
"""

import json
import logging
import re
import time
from datetime import date
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
MODEL_NAME = "gemini-2.5-flash"

# Gemini API の一時的障害（ネットワーク瞬断・503 等）に備えたリトライ設定
_MAX_RETRIES = 3
_RETRY_BASE_WAIT_SEC = 1.0  # 待機時間は 1s → 2s → 4s の指数バックオフ

# analyze() の戻り値スキーマ
_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "good_points": {"type": "array", "items": {"type": "string"}},
        "issues": {"type": "array", "items": {"type": "string"}},
        "top_priority": {"type": "string"},
        "action_plan": {"type": "array", "items": {"type": "string"}},
        "gorilla_monologue": {"type": "string"},
    },
    "required": ["summary", "good_points", "issues", "top_priority", "action_plan", "gorilla_monologue"],
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

# critique() の戻り値スキーマ（品質監査エージェント）
_CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["pass", "issues"],
}

# モードごとの生成スキーマ対応表
_SCHEMA_BY_MODE = {
    "default": _ANALYSIS_SCHEMA,
    "weekly_trend": _WEEKLY_TREND_SCHEMA,
    "tomorrow_plan": _TOMORROW_PLAN_SCHEMA,
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

    @staticmethod
    def _build_continuity_block(previous_analysis: Optional[dict]) -> str:
        """前回レビューの最重要課題・アクションプランを継続評価用の文脈として整形する。"""
        if not previous_analysis:
            return ""

        prev_date = previous_analysis.get("date", "前回")
        prev_priority = previous_analysis.get("top_priority", "")
        prev_actions = previous_analysis.get("action_plan", []) or []
        if isinstance(prev_actions, str):
            prev_actions = [prev_actions]

        if not prev_priority and not prev_actions:
            return ""

        lines = [f"\n【前回レビュー（{prev_date}）との継続性】"]
        if prev_priority:
            lines.append(f"  前回の最重要課題: {prev_priority}")
        if prev_actions:
            actions_text = " / ".join(str(a) for a in prev_actions)
            lines.append(f"  前回のアクションプラン: {actions_text}")
        lines.append(
            "  ※ 今日のデータをもとに、上記の課題が改善されたか・アクションが実行されたかを\n"
            "    summary か good_points / issues のいずれかで必ず具体的に評価すること\n"
        )
        return "\n".join(lines)

    def _build_system_prompt(self, previous_analysis: Optional[dict] = None) -> str:
        metrics = self._config.get("focus_metrics", [])
        metrics_text = "\n".join(f"  - {m}" for m in metrics)
        z2_threshold = self._config.get("zone2_threshold_bpm", 145)
        z2_target = self._config.get("zone2_target_pct", 70)

        return (
            self._build_common_header()
            + self._build_continuity_block(previous_analysis)
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
            f"  3. BMI・体重の改善に言及する場合は「今週-0.3kg」「月-1kg」など週次・月次の具体的な数値目標まで落とし込むこと\n"
            f"\n"
            f"gorilla_monologue: レビューの最後に添える「コーチの戯言」。以下の条件をすべて満たすこと:\n"
            f"  - ゴリラコーチらしいウホ口調で、1〜2文（40〜80文字程度）\n"
            f"  - ランニング・トレーニング・体・人生・宇宙などに関係するような・しないような、意味深で考えさせられる内容\n"
            f"  - 読んだ人がクスッと笑えるユーモアを必ず含めること\n"
            f"  - 説教や指導ではなく、ぼんやりとした哲学的な独り言のトーン\n"
            f"  - 毎回必ず異なる内容にすること（使い回し禁止）"
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
        一時的な障害に備え、指数バックオフで最大 _MAX_RETRIES 回までリトライする。
        全試行が失敗した場合は None を返す。
        """
        logger.info(
            "Gemini API にリクエストを送信します（system: %d文字 / data: %d文字）",
            len(system_prompt),
            len(user_data),
        )

        last_error: Optional[Exception] = None
        for attempt in range(1, _MAX_RETRIES + 1):
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
                logger.info("Gemini JSON 分析完了（試行 %d/%d）", attempt, _MAX_RETRIES)
                return result
            except Exception as e:
                last_error = e
                logger.warning(
                    "Gemini API エラー（試行 %d/%d）: %s", attempt, _MAX_RETRIES, e
                )
                if attempt < _MAX_RETRIES:
                    wait = _RETRY_BASE_WAIT_SEC * (2 ** (attempt - 1))
                    logger.info("%.1f 秒待機してリトライします", wait)
                    time.sleep(wait)

        logger.error("Gemini API が %d 回すべて失敗しました: %s", _MAX_RETRIES, last_error)
        return None

    # ------------------------------------------------------------------
    # サブエージェント用: モード横断の生成・批評インターフェース
    # （ループ制御は AnalysisAgent が担い、ここは1回分の呼び出しのみを提供する）
    # ------------------------------------------------------------------

    def _build_user_data(
        self,
        mode: str,
        today_summary: Optional[dict],
        today_activities: Optional[list[dict]],
        history: Optional[list[dict]],
    ) -> str:
        """モードに応じて Gemini に渡す user_data テキストを組み立てる。"""
        if mode == "weekly_trend":
            summaries = history or []
            return self._format_weekly_data(summaries[-7:] if len(summaries) > 7 else summaries)
        today_text = self._format_today_data(today_summary or {}, today_activities or [])
        history_text = self._format_history(history or [])
        return f"{today_text}\n\n{history_text}"

    def _build_system_prompt_for_mode(
        self, mode: str, previous_analysis: Optional[dict] = None
    ) -> str:
        """モードに応じた system プロンプトを返す。"""
        if mode == "weekly_trend":
            return self._build_weekly_trend_prompt()
        if mode == "tomorrow_plan":
            return self._build_tomorrow_plan_prompt()
        return self._build_system_prompt(previous_analysis)

    @staticmethod
    def _build_deficiency_block(deficiencies: list[str]) -> str:
        """品質レビューで検出された不備を再生成用の修正指示として整形する。"""
        items = "\n".join(f"  - {d}" for d in deficiencies)
        return (
            "\n\n【品質レビューによる修正指示】\n"
            "直前の出力には以下の不備が検出されました。\n"
            "これらをすべて解消したうえで、同じJSON形式の完全版を改めて出力すること:\n"
            f"{items}\n"
        )

    def generate(
        self,
        mode: str,
        *,
        today_summary: Optional[dict] = None,
        today_activities: Optional[list[dict]] = None,
        history: Optional[list[dict]] = None,
        previous_analysis: Optional[dict] = None,
        deficiencies: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """
        指定モードの分析結果を1回生成して返す。
        deficiencies を渡すと、その不備を解消するよう system プロンプトに修正指示を付加する。
        失敗時は None を返す。
        """
        system_prompt = self._build_system_prompt_for_mode(mode, previous_analysis)
        if deficiencies:
            system_prompt += self._build_deficiency_block(deficiencies)
        user_data = self._build_user_data(mode, today_summary, today_activities, history)
        schema = _SCHEMA_BY_MODE.get(mode, _ANALYSIS_SCHEMA)
        return self._call_json(system_prompt, user_data, schema)

    def _build_critic_prompt(self, mode: str, previous_analysis: Optional[dict] = None) -> str:
        """品質監査エージェント用の system プロンプトを組み立てる。"""
        prev = previous_analysis or {}
        prev_priority = prev.get("top_priority", "")
        prev_monologue = prev.get("gorilla_monologue", "")

        if mode == "weekly_trend":
            target_field = "numeric_goal（来週の数値目標）"
            priority_field = "key_issue（今週の最重要課題）"
        elif mode == "tomorrow_plan":
            target_field = "menu（各メニュー項目）"
            priority_field = "headline（見出し）"
        else:
            target_field = "action_plan（各アクション項目）"
            priority_field = "top_priority（今日の最重要課題）"

        checks = [
            f"1. {target_field} に、距離(km)・ペース(分/km)・心拍(bpm)・体重(kg)・回数などの"
            f"具体的な数値が含まれているか。抽象的・精神論だけの項目があれば不備とする。",
            f"2. {priority_field} が、前回の最重要ポイント「{prev_priority or '（前回なし）'}」と"
            f"実質的に同じ文言・同じ趣旨の使い回しになっていないか。",
            "3. summary・good_points・issues などの各記述が、提示された元データ"
            "（心拍・ペース・体重・睡眠スコア等）の実際の数値を根拠にしているか。"
            "データに存在しない事実や、数値に触れない曖昧な指摘があれば不備とする。",
        ]
        if mode == "default":
            checks.append(
                f"4. gorilla_monologue が、前回の戯言「{prev_monologue or '（前回なし）'}」と"
                f"同一または酷似していないか（毎回新しい内容であること）。"
            )

        checks_text = "\n".join(checks)
        return (
            "あなたはランニングコーチング分析の品質監査担当です。\n"
            "以下に『元データ』と、それをもとに生成された『検証対象の生成結果(JSON)』を示します。\n"
            "生成結果が次のチェック項目をすべて満たしているか厳格に評価してください:\n"
            "\n"
            f"{checks_text}\n"
            "\n"
            "判定ルール:\n"
            "- 1項目でも満たさない場合は pass=false とし、issues に『どのフィールドが・なぜ不十分か・"
            "どう直すべきか』を具体的な日本語で列挙すること。\n"
            "- issues は再生成時の修正指示として使われるため、実行可能な粒度で書くこと。\n"
            "- すべて満たす場合のみ pass=true とし、issues は空配列にすること。\n"
            "- 甘い判定は避け、迷ったら不備として扱うこと。"
        )

    def critique(
        self,
        mode: str,
        result: dict,
        *,
        today_summary: Optional[dict] = None,
        today_activities: Optional[list[dict]] = None,
        history: Optional[list[dict]] = None,
        previous_analysis: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        生成結果を元データと照合して品質を監査し、{"pass": bool, "issues": [...]} を返す。
        批評呼び出し自体が失敗した場合は None を返す（呼び出し元は現結果を採用する）。
        """
        system_prompt = self._build_critic_prompt(mode, previous_analysis)
        data_text = self._build_user_data(mode, today_summary, today_activities, history)
        result_text = json.dumps(result, ensure_ascii=False, indent=2)
        user_data = (
            f"{data_text}\n\n"
            f"【検証対象の生成結果(JSON)】\n{result_text}"
        )
        return self._call_json(system_prompt, user_data, _CRITIQUE_SCHEMA)

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
