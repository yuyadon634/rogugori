"""
分析品質を担保するサブエージェント・オーケストレーター。

「生成 → 批評 → ピンポイント修正再生成」のループを制御する:
  1. GeminiClient.generate() で分析を1回生成する
  2. GeminiClient.critique() で品質を監査する（JSON: pass / issues）
  3. pass=false なら issues を修正指示として system プロンプトに付加し、1回だけ再生成する

最大コール数は 3（生成1 + 批評1 + 再生成1）に制限し、所要時間を3分以内に収める。
GeminiClient は API 呼び出しのみを担い、ループ制御の責務はこのクラスに集約する。
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from src.gemini_client import GeminiClient

logger = logging.getLogger(__name__)

# 再生成の最大回数（生成1 + 批評1 + 再生成1 = 最大3コール）
MAX_REGENERATIONS = 1


@dataclass
class AnalysisOutcome:
    """サブエージェントの実行結果。analysis_log への記録に必要なメタ情報を含む。"""

    data: dict
    retry_count: int = 0
    critic_issues: list[str] = field(default_factory=list)


class AnalysisAgent:
    """生成エージェントと批評エージェントを協調させ、品質を担保した分析を返す。"""

    def __init__(self, gemini: GeminiClient, max_regenerations: int = MAX_REGENERATIONS):
        self._gemini = gemini
        self._max_regenerations = max_regenerations

    def run(
        self,
        mode: str,
        *,
        today_summary: Optional[dict] = None,
        today_activities: Optional[list[dict]] = None,
        history: Optional[list[dict]] = None,
        previous_analysis: Optional[dict] = None,
    ) -> Optional[AnalysisOutcome]:
        """
        指定モードの分析を生成→批評→（必要なら）再生成して AnalysisOutcome を返す。
        最初の生成に失敗した場合のみ None を返す。
        """
        inputs = {
            "today_summary": today_summary,
            "today_activities": today_activities,
            "history": history,
            "previous_analysis": previous_analysis,
        }

        logger.info("[AnalysisAgent] mode=%s 生成を開始します", mode)
        result = self._gemini.generate(mode, **inputs)
        if result is None:
            logger.error("[AnalysisAgent] 初回生成に失敗しました (mode=%s)", mode)
            return None

        retry_count = 0
        critic_issues: list[str] = []

        for _ in range(self._max_regenerations):
            critique = self._gemini.critique(mode, result, **inputs)
            if critique is None:
                logger.warning("[AnalysisAgent] 批評の呼び出しに失敗。現結果を採用します")
                break

            issues = critique.get("issues", []) or []
            if critique.get("pass"):
                logger.info("[AnalysisAgent] 品質チェック合格")
                critic_issues = []
                break

            critic_issues = issues
            logger.info(
                "[AnalysisAgent] 品質チェック不合格（%d件）。再生成します: %s",
                len(issues),
                issues,
            )
            regenerated = self._gemini.generate(mode, deficiencies=issues, **inputs)
            retry_count += 1
            if regenerated is None:
                logger.warning("[AnalysisAgent] 再生成に失敗。直前の結果を採用します")
                break
            result = regenerated

        if critic_issues:
            logger.info(
                "[AnalysisAgent] 最終結果に未解消の指摘が %d件残っています（最善結果として送信）",
                len(critic_issues),
            )

        return AnalysisOutcome(data=result, retry_count=retry_count, critic_issues=critic_issues)
