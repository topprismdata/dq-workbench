"""HITL 双人仲裁（SSI hitl/review_task.py 语义移植，挂账⑥闭环）。

移植的不变量（SSI Gate V2-3 / H3）：
1. CONFLICTED 是主张（claim）状态，不是人的动作——人从不"被冲突"；
   SUPPORT 与 REFUTE 并存时是主张冲突
2. 任何 SUPPORT×REFUTE 分歧一律 ESCALATE 二次评审——**与评审人权威无关**；
   高权威评审人管路由，绝不静默抹掉低权威产生的反证；全部裁决保留
3. **不用多数表决制造共识**（2:1 的 SUPPORT 仍 ESCALATE）
4. APPROVE_REPAIR 严格排除在裁决动作之外——修复批准是独立的治理命题
   （本包动作枚举里根本没有这个值）
5. is_revoked 的裁决不参与仲裁（撤销留痕，不删除）
6. PREDICTION_HIDDEN 可见性字段保留语义位：机器预测对首评审默认隐藏，
   防锚定偏差
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ReviewerRole(str, Enum):
    REVIEWER = "REVIEWER"
    DOMAIN_EXPERT = "DOMAIN_EXPERT"
    DATA_STEWARD = "DATA_STEWARD"
    POLICY_OWNER = "POLICY_OWNER"


class AdjudicationAction(str, Enum):
    SUPPORT = "SUPPORT"
    REFUTE = "REFUTE"
    UNRESOLVED = "UNRESOLVED"
    ESCALATE = "ESCALATE"


class PredictionVisibility(str, Enum):
    PREDICTION_HIDDEN = "PREDICTION_HIDDEN"
    PREDICTION_VISIBLE = "PREDICTION_VISIBLE"
    PARTIALLY_VISIBLE = "PARTIALLY_VISIBLE"


class ReviewConfidence(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass
class Adjudication:
    """一条人工裁决（追加式，撤销只标记）。"""

    adjudication_id: str
    issue_id: str
    reviewer: str
    role: ReviewerRole
    action: AdjudicationAction
    confidence: ReviewConfidence
    visibility: PredictionVisibility = PredictionVisibility.PREDICTION_HIDDEN
    note: str = ""
    is_revoked: bool = False


def arbitrate_review_conflict(adjudications: list[Adjudication]) -> AdjudicationAction:
    """给定一个工单的全部裁决，路由治理动作。SSI 同型语义。"""
    if not adjudications:
        return AdjudicationAction.UNRESOLVED
    actions = {a.action for a in adjudications if not a.is_revoked}
    if AdjudicationAction.ESCALATE in actions:
        return AdjudicationAction.ESCALATE
    if (AdjudicationAction.SUPPORT in actions
            and AdjudicationAction.REFUTE in actions):
        return AdjudicationAction.ESCALATE
    if AdjudicationAction.SUPPORT in actions:
        return AdjudicationAction.SUPPORT
    if AdjudicationAction.REFUTE in actions:
        return AdjudicationAction.REFUTE
    return AdjudicationAction.UNRESOLVED
