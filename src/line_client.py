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
    ButtonComponent,
    FlexSendMessage,
    PostbackAction,
    SeparatorComponent,
    TextComponent,
    TextSendMessage,
)

logger = logging.getLogger(__name__)

_ANALYSIS_BUTTON = ButtonComponent(
    action=PostbackAction(label="🔍 分析開始", data="action=llm_analysis"),
    style="primary",
    color="#4CAF50",
    margin="md",
)


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

    def _push_with_analysis_button(self, text: str) -> None:
        """区切り線と「分析開始」ボタンを末尾に付けた Flex Message を送信する。"""
        bubble = BubbleContainer(
            body=BoxComponent(
                layout="vertical",
                contents=[
                    TextComponent(
                        text=text,
                        wrap=True,
                        size="sm",
                    ),
                    SeparatorComponent(margin="lg"),
                    _ANALYSIS_BUTTON,
                ],
            )
        )
        msg = FlexSendMessage(alt_text=text[:60], contents=bubble)
        try:
            self._api.push_message(self._user_id, msg)
            logger.info("LINE Flex 送信完了: %s文字", len(text))
        except Exception as e:
            logger.error("LINE Flex 送信失敗: %s", e)
            raise

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
        self._push_with_analysis_button(text)

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
        self._push_with_analysis_button(text)

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
        self._push_with_analysis_button(text)

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
        self._push_with_analysis_button(text)

    def send_llm_analysis(self, analysis_text: str) -> None:
        """LLM による総括分析テキストを送信する。"""
        header = "🦍 ゴリラコーチからの今日のレビューだウホ！\n\n"
        self._push(header + analysis_text)
