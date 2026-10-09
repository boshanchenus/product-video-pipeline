from __future__ import annotations

import re
from typing import Optional

from .models import QAReport, QAIssue, Storyboard


ABSOLUTE_CLAIMS = re.compile(r"(100%|绝对|永久|第一|最好|治愈|零风险|立刻见效)", re.I)


def selling_point_terms(text: str) -> list[str]:
    """Return meaningful phrases instead of matching on any single character."""
    return [
        term.strip()
        for term in re.split(r"[，,；;。.!！?？\n]+", text)
        if len(term.strip()) >= 2
    ]


def deterministic_qa(board: Storyboard, selling_points: str) -> QAReport:
    issues: list[QAIssue] = []
    terms = selling_point_terms(selling_points)
    positions = [s.position for s in board.shots]
    if positions != list(range(1, len(board.shots) + 1)):
        issues.append(QAIssue(severity="error", code="shot_order", message="镜头序号必须从 1 连续递增"))
    total = sum(s.duration for s in board.shots)
    if total > 60:
        issues.append(QAIssue(severity="error", code="duration", message=f"总时长 {total}s，超过 60s"))
    if len(board.shots) < 3:
        issues.append(QAIssue(severity="warning", code="shot_count", message="少于 3 个镜头，叙事可能不完整"))
    all_content = "".join(
        f"{s.title}{s.voiceover}{s.overlay_text}{s.prompt}" for s in board.shots
    )
    for term in terms:
        if term not in all_content:
            issues.append(QAIssue(
                severity="warning", code="missing_selling_point",
                message=f"输入卖点“{term}”未在整套分镜中明确体现，请人工确认",
            ))
    for s in board.shots:
        if ABSOLUTE_CLAIMS.search(f"{s.voiceover} {s.overlay_text}"):
            issues.append(QAIssue(severity="error", code="risky_claim", message="含绝对化或高风险功效宣称", shot_position=s.position))
        if len(s.overlay_text) > 18:
            issues.append(QAIssue(severity="warning", code="overlay_long", message="屏显文案偏长", shot_position=s.position))
    errors = sum(i.severity == "error" for i in issues)
    warnings = sum(i.severity == "warning" for i in issues)
    score = max(0, 100 - errors * 30 - warnings * 6)
    return QAReport(passed=errors == 0, score=score, issues=issues)


def merge_qa(local: QAReport, model_report: Optional[QAReport], pass_score: int = 70) -> QAReport:
    if not model_report:
        return local
    issues = []
    seen = set()
    for issue in local.issues + model_report.issues:
        key = (issue.code, issue.shot_position, issue.message)
        if key not in seen:
            issues.append(issue)
            seen.add(key)
    score = min(local.score, model_report.score)
    # M3 occasionally returns a boolean that contradicts its score/issues.
    # Keep one auditable gate: threshold plus absence of hard errors.
    passed = score >= pass_score and not any(i.severity == "error" for i in issues)
    return QAReport(passed=passed, score=score, issues=issues)
