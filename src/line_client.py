"""
LINE Bot Push メッセージ送信クライアント。
各通知タイプ（睡眠・アクティビティ・体重・休養日・LLM分析）のメッセージ整形を担当する。
"""

import logging
from typing import Optional

from linebot import LineBotApi
from linebot.models import (
    BubbleContainer,
    BoxComponent,
    FlexSendMessage,
    SeparatorComponent,
    TextComponent,
    TextSendMessage,
)

logger = logging.getLogger(__name__)


class LineClient:
    def __init__(self, channel_access_token: str, user_id: str):
        self._api = LineBotApi(channel_access_token)
        self._user_id = user_id

    def _push(self, text: str) -> None:
        """指定テキストを Push メッセージで送信する。"""
        try:
            self._api.push_message(self._user_id, TextSendMessage(text=text))
            logger.info("LINE 送信完了: %s文字", len(text))
        except Exception as e:
            logger.error("LINE 送信失敗: %s", e)
            raise

    def push_text(self, text: str) -> None:
        """プレーンテキストを Push メッセージで送信する（公開 API）。"""
        self._push(text)

    # ------------------------------------------------------------------
    # 通知メッセージ整形
    # ------------------------------------------------------------------

    def send_sleep_report(self, sleep: dict, consecutive_rest_days: int) -> None:
        """朝の睡眠レポートを送信する。"""
        score = sleep.get("sleep_score")
        hours = sleep.get("sleep_hours", 0)
        deep = sleep.get("deep_sleep_hours", 0)
        rem = sleep.get("rem_sleep_hours", 0)

        score_text = f"{score}点" if score is not None else "計測なし"

        rest_line = ""
        if consecutive_rest_days > 0:
            rest_line = f"\n🦍 連続休養: {consecutive_rest_days}日目"

        text = (
            f"🌙 おはよう！昨夜の睡眠レポートだウホ！\n"
            f"\n"
            f"睡眠スコア: {score_text}\n"
            f"合計睡眠: {hours}時間\n"
            f"深睡眠: {deep}時間 / REM: {rem}時間"
            f"{rest_line}"
        )
        self._push(text)

    def send_activity_notification(self, activity: dict, consecutive_exercise_days: int) -> None:
        """アクティビティ検出時の即時通知を送信する。"""
        act_type = activity.get("activity_type", "アクティビティ")
        distance = activity.get("distance_km", 0)
        pace = activity.get("avg_pace", "N/A")
        hr = activity.get("avg_heart_rate", 0)
        calories = activity.get("calories", 0)
        start = activity.get("start_time", "")

        streak_text = ""
        if consecutive_exercise_days > 1:
            streak_text = f"\n🔥 連続運動: {consecutive_exercise_days}日！ドラミング！"

        text = (
            f"💪 アクティビティ検出ウホ！\n"
            f"\n"
            f"種目: {act_type}\n"
            f"開始: {start}\n"
            f"距離: {distance} km\n"
            f"平均ペース: {pace}\n"
            f"平均心拍: {hr} bpm\n"
            f"消費カロリー: {calories} kcal"
            f"{streak_text}"
        )
        self._push(text)

    def send_rest_day_notification(self, consecutive_rest_days: int) -> None:
        """23:00 以降・運動なしの休養日通知を送信する。"""
        if consecutive_rest_days >= 3:
            comment = f"お休み{consecutive_rest_days}日連続…ゴリラも心配だウホ。明日は動くぞ！ドラミング！"
        elif consecutive_rest_days == 2:
            comment = "2日連続の休養だウホ。体は回復中か？明日に備えるんだウホ！"
        else:
            comment = "今日は休養日ウホ。しっかり休んで明日に備えるんだウホ！"

        text = (
            f"😴 本日の運動記録なし\n"
            f"\n"
            f"連続休養: {consecutive_rest_days}日目\n"
            f"\n"
            f"{comment}"
        )
        self._push(text)

    def send_weight_notification(
        self,
        weight_kg: float,
        body_fat_pct: Optional[float],
        bmi: Optional[float] = None,
        lean_body_mass_kg: Optional[float] = None,
    ) -> None:
        """体重・体脂肪データ検出時の即時通知を送信する。"""
        fat_text = f"{body_fat_pct}%" if body_fat_pct is not None else "計測なし"
        bmi_text = f"{bmi}" if bmi is not None else "計測なし"
        lean_text = f"{lean_body_mass_kg} kg" if lean_body_mass_kg is not None else "計測なし"
        text = (
            f"⚖️ Eufy 体組成データだウホ！\n"
            f"\n"
            f"体重: {weight_kg} kg\n"
            f"体脂肪率: {fat_text}\n"
            f"BMI: {bmi_text}\n"
            f"除脂肪体重: {lean_text}"
        )
        self._push(text)

    # ------------------------------------------------------------------
    # Flex Message ヘルパー
    # ------------------------------------------------------------------

    @staticmethod
    def _make_section_title(text: str) -> TextComponent:
        return TextComponent(text=text, weight="bold", size="sm", margin="md", color="#555555")

    @staticmethod
    def _make_bullet(text: str) -> TextComponent:
        return TextComponent(
            text=f"• {text}",
            wrap=True,
            size="sm",
            margin="xs",
            color="#333333",
        )

    def _build_analysis_bubble(self, analysis: dict) -> BubbleContainer:
        """analyze() の戻り値 dict から BubbleContainer を組み立てる。"""
        summary = analysis.get("summary", "")
        good_points = analysis.get("good_points", [])
        issues = analysis.get("issues", [])
        top_priority = analysis.get("top_priority", "")
        action_plan = analysis.get("action_plan", [])

        body_contents = [
            TextComponent(text=summary, wrap=True, size="sm", color="#333333"),
        ]

        if top_priority:
            body_contents.append(
                BoxComponent(
                    layout="vertical",
                    background_color="#FFF3E0",
                    corner_radius="6px",
                    margin="md",
                    padding_all="sm",
                    contents=[
                        TextComponent(
                            text="🎯 今日の最重要課題",
                            size="xs",
                            color="#E65100",
                            weight="bold",
                        ),
                        TextComponent(
                            text=top_priority,
                            size="sm",
                            color="#BF360C",
                            weight="bold",
                            wrap=True,
                            margin="xs",
                        ),
                    ],
                )
            )

        if good_points:
            body_contents.append(SeparatorComponent(margin="lg"))
            body_contents.append(self._make_section_title("✅ 良かった点"))
            for p in good_points:
                body_contents.append(self._make_bullet(p))

        if issues:
            body_contents.append(SeparatorComponent(margin="lg"))
            body_contents.append(self._make_section_title("⚠️ 課題・改善点"))
            for iss in issues:
                body_contents.append(self._make_bullet(iss))

        if action_plan:
            body_contents.append(SeparatorComponent(margin="lg"))
            body_contents.append(self._make_section_title("💪 明日のアクションプラン"))
            for plan in action_plan:
                body_contents.append(self._make_bullet(plan))

        return BubbleContainer(
            header=BoxComponent(
                layout="vertical",
                background_color="#2E7D32",
                contents=[
                    TextComponent(
                        text="🦍 ゴリラコーチのレビュー",
                        weight="bold",
                        size="md",
                        color="#FFFFFF",
                    )
                ],
            ),
            body=BoxComponent(
                layout="vertical",
                contents=body_contents,
                padding_all="lg",
            ),
        )

    def _build_tomorrow_plan_bubble(self, plan: dict) -> BubbleContainer:
        """analyze_tomorrow_plan() の戻り値 dict から BubbleContainer を組み立てる。"""
        headline = plan.get("headline", "明日のトレーニングプラン")
        menu = plan.get("menu", [])
        rationale = plan.get("rationale", "")
        caution = plan.get("caution", "")

        body_contents = [
            TextComponent(text=headline, wrap=True, size="sm", weight="bold", color="#333333"),
        ]

        if menu:
            body_contents.append(SeparatorComponent(margin="lg"))
            body_contents.append(self._make_section_title("🏃 メニュー"))
            for item in menu:
                body_contents.append(self._make_bullet(item))

        if rationale:
            body_contents.append(SeparatorComponent(margin="lg"))
            body_contents.append(self._make_section_title("📝 選んだ理由"))
            body_contents.append(
                TextComponent(text=rationale, wrap=True, size="sm", color="#333333", margin="xs")
            )

        if caution:
            body_contents.append(SeparatorComponent(margin="lg"))
            body_contents.append(self._make_section_title("⚡ 注意"))
            body_contents.append(
                TextComponent(text=caution, wrap=True, size="sm", color="#333333", margin="xs")
            )

        return BubbleContainer(
            header=BoxComponent(
                layout="vertical",
                background_color="#1565C0",
                contents=[
                    TextComponent(
                        text="🏃 明日のトレーニングメニュー",
                        weight="bold",
                        size="md",
                        color="#FFFFFF",
                    )
                ],
            ),
            body=BoxComponent(
                layout="vertical",
                contents=body_contents,
                padding_all="lg",
            ),
        )

    def _build_weekly_trend_bubble(self, trend: dict) -> BubbleContainer:
        """analyze_weekly_trend() の戻り値 dict から BubbleContainer を組み立てる。"""
        weekly_summary  = trend.get("weekly_summary", "")
        best            = trend.get("best_performance", "")
        key_issue       = trend.get("key_issue", "")
        next_focus      = trend.get("next_week_focus", "")
        numeric_goal    = trend.get("numeric_goal", "")

        body_contents = [
            TextComponent(text=weekly_summary, wrap=True, size="sm", color="#333333"),
        ]

        if best:
            body_contents.append(SeparatorComponent(margin="lg"))
            body_contents.append(self._make_section_title("🏆 今週のベスト"))
            body_contents.append(self._make_bullet(best))

        if key_issue:
            body_contents.append(
                BoxComponent(
                    layout="vertical",
                    background_color="#FFF3E0",
                    corner_radius="6px",
                    margin="md",
                    padding_all="sm",
                    contents=[
                        TextComponent(text="🎯 今週の最重要課題", size="xs", color="#E65100", weight="bold"),
                        TextComponent(text=key_issue, size="sm", color="#BF360C", weight="bold", wrap=True, margin="xs"),
                    ],
                )
            )

        if next_focus:
            body_contents.append(SeparatorComponent(margin="lg"))
            body_contents.append(self._make_section_title("📅 来週の重点テーマ"))
            body_contents.append(self._make_bullet(next_focus))

        if numeric_goal:
            body_contents.append(SeparatorComponent(margin="lg"))
            body_contents.append(self._make_section_title("🎯 来週の数値目標"))
            body_contents.append(self._make_bullet(numeric_goal))

        return BubbleContainer(
            header=BoxComponent(
                layout="vertical",
                background_color="#4527A0",
                contents=[
                    TextComponent(
                        text="📊 ゴリラコーチ週間レポート",
                        weight="bold",
                        size="md",
                        color="#FFFFFF",
                    )
                ],
            ),
            body=BoxComponent(
                layout="vertical",
                contents=body_contents,
                padding_all="lg",
            ),
        )

    # ------------------------------------------------------------------
    # LLM 分析送信
    # ------------------------------------------------------------------

    def send_llm_analysis(self, analysis_text: str) -> None:
        """プレーンテキストで LLM 分析を送信する（フォールバック用）。"""
        header = "🦍 ゴリラコーチからの今日のレビューだウホ！\n\n"
        self._push(header + analysis_text)

    def send_llm_analysis_flex(self, analysis: dict) -> None:
        """
        analyze() の戻り値 dict を 4セクション Flex Message で送信する。
        Flex 組み立て失敗時はプレーンテキストにフォールバックする。
        """
        try:
            bubble = self._build_analysis_bubble(analysis)
            summary = analysis.get("summary", "")
            alt = f"🦍 ゴリラコーチのレビュー: {summary[:50]}"
            msg = FlexSendMessage(alt_text=alt, contents=bubble)
            self._api.push_message(self._user_id, msg)
            logger.info("LINE Flex レビュー送信完了")
        except Exception as e:
            logger.warning("Flex Message 送信失敗、プレーンテキストにフォールバック: %s", e)
            lines = ["🦍 ゴリラコーチからの今日のレビューだウホ！\n"]
            if analysis.get("summary"):
                lines.append(analysis["summary"])
            if analysis.get("top_priority"):
                lines.append(f"\n🎯 最重要課題: {analysis['top_priority']}")
            if analysis.get("good_points"):
                lines.append("\n✅ 良かった点")
                lines.extend(f"• {p}" for p in analysis["good_points"])
            if analysis.get("issues"):
                lines.append("\n⚠️ 課題・改善点")
                lines.extend(f"• {i}" for i in analysis["issues"])
            if analysis.get("action_plan"):
                lines.append("\n💪 明日のアクションプラン")
                lines.extend(f"• {a}" for a in analysis["action_plan"])
            self._push("\n".join(lines))

    def send_weekly_trend_flex(self, trend: dict) -> None:
        """
        analyze_weekly_trend() の戻り値 dict を Flex Message で送信する。
        Flex 組み立て失敗時はプレーンテキストにフォールバックする。
        """
        try:
            bubble = self._build_weekly_trend_bubble(trend)
            summary = trend.get("weekly_summary", "今週の週間レポート")
            msg = FlexSendMessage(alt_text=f"📊 ゴリラコーチ週間レポート: {summary[:40]}", contents=bubble)
            self._api.push_message(self._user_id, msg)
            logger.info("LINE Flex 週間レポート送信完了")
        except Exception as e:
            logger.warning("Flex Message 送信失敗、プレーンテキストにフォールバック: %s", e)
            lines = ["📊 ゴリラコーチ週間レポートだウホ！\n"]
            if trend.get("weekly_summary"):
                lines.append(trend["weekly_summary"])
            if trend.get("best_performance"):
                lines.append(f"\n🏆 今週のベスト: {trend['best_performance']}")
            if trend.get("key_issue"):
                lines.append(f"🎯 最重要課題: {trend['key_issue']}")
            if trend.get("next_week_focus"):
                lines.append(f"\n📅 来週の重点テーマ: {trend['next_week_focus']}")
            if trend.get("numeric_goal"):
                lines.append(f"🎯 数値目標: {trend['numeric_goal']}")
            self._push("\n".join(lines))

    def send_tomorrow_plan_flex(self, plan: dict) -> None:
        """
        analyze_tomorrow_plan() の戻り値 dict を Flex Message で送信する。
        Flex 組み立て失敗時はプレーンテキストにフォールバックする。
        """
        try:
            bubble = self._build_tomorrow_plan_bubble(plan)
            headline = plan.get("headline", "明日のトレーニングプラン")
            msg = FlexSendMessage(alt_text=f"🏃 {headline[:50]}", contents=bubble)
            self._api.push_message(self._user_id, msg)
            logger.info("LINE Flex 翌日プラン送信完了")
        except Exception as e:
            logger.warning("Flex Message 送信失敗、プレーンテキストにフォールバック: %s", e)
            lines = [f"🏃 {plan.get('headline', '明日のトレーニングプラン')}\n"]
            if plan.get("menu"):
                lines.append("メニュー:")
                lines.extend(f"• {m}" for m in plan["menu"])
            if plan.get("rationale"):
                lines.append(f"\n理由: {plan['rationale']}")
            if plan.get("caution"):
                lines.append(f"⚡ 注意: {plan['caution']}")
            self._push("\n".join(lines))
