"""统一异常工作台核心对象。severity/action 与 GeoGov DSL 同型。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class Layer(str, Enum):
    SPATIAL = "SPATIAL"          # 空间层（geocode-arbiter / SSI）
    IDENTITY = "IDENTITY"        # 身份层（identity-service）
    STATISTICAL = "STATISTICAL"  # 统计层（mousheng anomalies）


class Severity(str, Enum):
    C0 = "C0"
    C1 = "C1"
    C2 = "C2"
    C3 = "C3"
    UNSPECIFIED = "UNSPECIFIED"


class IssueStatus(str, Enum):
    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    IN_REVIEW = "IN_REVIEW"
    RESOLVED = "RESOLVED"        # 提出修复，待人工确认
    CLOSED = "CLOSED"            # 仅人工可关（验收：工单状态机含人工关闭）
    REOPENED = "REOPENED"


class CheckResult(str, Enum):
    PASSED = "PASSED"
    FINDINGS = "FINDINGS"
    SKIPPED = "SKIPPED"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


@dataclass
class Finding:
    """体检发现：来自某一层某个规则。只入工作台，绝不直接改主数据。"""

    finding_id: str
    layer: Layer
    rule_id: str                              # GeoGov RuleID 命名空间
    subject: str                              # outlet_entity_id / source_record_id
    severity: Severity
    message: str
    evidence: list[str] = field(default_factory=list)   # evidence_id / decision_id
    source_run: str = ""                      # 产生该发现的体检 run
    detected_at: str = field(default_factory=now_iso)


@dataclass
class CheckReport:
    """一层体检的结果汇总。"""

    layer: Layer
    run_id: str
    result: CheckResult
    findings: list[Finding] = field(default_factory=list)
    note: str = ""


@dataclass
class Issue:
    """异常工单：可持久化/分派/SLA/人工关闭。"""

    issue_id: str
    finding_ids: list[str]
    title: str
    status: IssueStatus = IssueStatus.OPEN
    assignee: str = ""
    sla_due: str = ""
    created_at: str = field(default_factory=now_iso)
    closed_by: str = ""            # 人工关闭者（验收项）
    closed_at: str = ""
    resolution_note: str = ""


@dataclass
class AuditEvent:
    """追加式审计事件：工作台内一切写操作都产生。"""

    event_id: str
    event_type: str               # FINDING_INGESTED / ISSUE_CREATED / ISSUE_ASSIGNED / ...
    payload: dict = field(default_factory=dict)
    actor: str = ""
    at: str = field(default_factory=now_iso)
    prev_event_id: str = ""       # 哈希链位型（简化为链式引用）
