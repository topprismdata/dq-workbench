"""WP08 测试：三层编排 / 工单状态机 / SLA / 审计链 / 主数据隔离。"""
from __future__ import annotations

import json

import pytest

from dq_workbench.models import (
    AuditEvent,
    CheckResult,
    Finding,
    IssueStatus,
    Layer,
    Severity,
)
from dq_workbench.orchestrate import JsonlFindingsAdapter, orchestrate
from dq_workbench.workbench import IllegalTransition, Workbench


@pytest.fixture()
def wb(tmp_path):
    w = Workbench(tmp_path / "w.db")
    yield w
    w.close_db()


def _finding(i=1, layer=Layer.SPATIAL, sev="C2"):
    return Finding(finding_id=f"f{i}", layer=layer, rule_id="R018", subject="O1",
                   severity=Severity(sev), message="混帧偏移 621m", evidence=["dec_1"])


def test_orchestrate_three_layers(tmp_path):
    p1 = tmp_path / "spatial.jsonl"
    p1.write_text(json.dumps({"layer": "SPATIAL", "rule_id": "R018", "subject": "O1",
                              "severity": "C2", "message": "x"}), encoding="utf-8")
    p2 = tmp_path / "identity.jsonl"
    p2.write_text(json.dumps({"layer": "IDENTITY", "rule_id": "R023", "subject": "rec_9",
                              "severity": "C3", "message": "重复候选"}), encoding="utf-8")
    p3 = tmp_path / "stat.jsonl"
    p3.write_text("", encoding="utf-8")
    reports = orchestrate([
        JsonlFindingsAdapter(str(p1), Layer.SPATIAL),
        JsonlFindingsAdapter(str(p2), Layer.IDENTITY),
        JsonlFindingsAdapter(str(p3), Layer.STATISTICAL),
    ])
    assert [r.layer for r in reports] == [Layer.SPATIAL, Layer.IDENTITY, Layer.STATISTICAL]
    assert reports[0].result is CheckResult.FINDINGS
    assert reports[2].result is CheckResult.PASSED
    # run_id 贯穿同一次编排
    run_ids = {r.run_id for r in reports}
    assert len(run_ids) == 1
    assert reports[0].findings[0].source_run == reports[1].findings[0].source_run


def test_full_flow_ingest_issue_assign_close(wb):
    ids = wb.ingest([_finding(1), _finding(2)])
    issue = wb.open_issue(ids, title="坐标混帧待复核", sla_due="2026-01-10")
    wb.transition(issue.issue_id, IssueStatus.ASSIGNED, actor="alice")
    wb.transition(issue.issue_id, IssueStatus.IN_REVIEW)
    wb.transition(issue.issue_id, IssueStatus.RESOLVED, note="已修正坐标")
    closed = wb.transition(issue.issue_id, IssueStatus.CLOSED, actor="bob")
    assert closed.closed_by == "bob" and closed.closed_at
    assert closed.status is IssueStatus.CLOSED


def test_close_requires_human_actor(wb):
    ids = wb.ingest([_finding(3)])
    issue = wb.open_issue(ids, title="t")
    with pytest.raises(PermissionError):
        wb.transition(issue.issue_id, IssueStatus.CLOSED, actor="")  # 系统路径不可关


def test_illegal_transition_rejected(wb):
    ids = wb.ingest([_finding(4)])
    issue = wb.open_issue(ids, title="t")
    with pytest.raises(IllegalTransition):
        wb.transition(issue.issue_id, IssueStatus.RESOLVED)  # OPEN → RESOLVED 非法


def test_reopen_cycle(wb):
    ids = wb.ingest([_finding(5)])
    issue = wb.open_issue(ids, title="t")
    wb.transition(issue.issue_id, IssueStatus.ASSIGNED, actor="a")
    wb.transition(issue.issue_id, IssueStatus.IN_REVIEW)
    wb.transition(issue.issue_id, IssueStatus.RESOLVED, note="fix")
    wb.transition(issue.issue_id, IssueStatus.REOPENED)
    wb.transition(issue.issue_id, IssueStatus.ASSIGNED, actor="b")
    assert wb._get_issue(issue.issue_id).assignee == "b"


def test_audit_chain_connected(wb):
    ids = wb.ingest([_finding(6)])
    issue = wb.open_issue(ids, title="t")
    wb.transition(issue.issue_id, IssueStatus.ASSIGNED, actor="a")
    wb.transition(issue.issue_id, IssueStatus.CLOSED, actor="b")
    chain = wb.audit_chain()
    types = [e.event_type for e in chain]
    assert types == ["FINDINGS_INGESTED", "ISSUE_CREATED", "ISSUE_ASSIGNED", "ISSUE_CLOSED"]
    # prev_event_id 链连续
    for prev, cur in zip(chain, chain[1:]):
        assert cur.prev_event_id == prev.event_id


def test_overdue_listing(wb):
    ids = wb.ingest([_finding(7)])
    wb.open_issue(ids, title="t", sla_due="2020-01-01")
    assert len(wb.overdue(today="2026-01-01")) == 1


def test_findings_never_touch_master_data(wb):
    """W1：发现只进工作台库。工作台不提供任何主数据写路径——
    结构性证据：Workbench 公开方法清单里没有 outlet/master 写操作。"""
    public = [m for m in dir(Workbench) if not m.startswith("_")]
    assert not any(k in public for k in ("outlet", "master", "identity_write", "save_outlet"))
    ids = wb.ingest([_finding(8)])
    assert wb.finding(ids[0]).source_run is not None  # 只在 workbench 自己库里


# ---- HITL 双人仲裁（SSI V2-3/H3 移植）----

from dq_workbench.hitl import (
    AdjudicationAction,
    ReviewConfidence,
    ReviewerRole,
    arbitrate_review_conflict,
)


def _issue(wb):
    ids = wb.ingest([_finding(20)])
    return wb.open_issue(ids, title="待仲裁")


def test_support_refute_conflict_escalates_regardless_of_authority(wb):
    iss = _issue(wb)
    wb.adjudicate(iss.issue_id, "r1", ReviewerRole.REVIEWER,
                  AdjudicationAction.SUPPORT, ReviewConfidence.HIGH)
    r = wb.adjudicate(iss.issue_id, "r2", ReviewerRole.POLICY_OWNER,
                      AdjudicationAction.REFUTE, ReviewConfidence.HIGH)
    assert r is AdjudicationAction.ESCALATE  # 权威不压冲突


def test_no_majority_vote(wb):
    """2 SUPPORT vs 1 REFUTE 仍 ESCALATE——不用多数表决制造共识。"""
    iss = _issue(wb)
    for name in ("a", "b"):
        wb.adjudicate(iss.issue_id, name, ReviewerRole.DATA_STEWARD,
                      AdjudicationAction.SUPPORT, ReviewConfidence.HIGH)
    r = wb.adjudicate(iss.issue_id, "c", ReviewerRole.REVIEWER,
                      AdjudicationAction.REFUTE, ReviewConfidence.LOW)
    assert r is AdjudicationAction.ESCALATE
    # 反证保留：低权威的 REFUTE 记录仍在
    refute_kept = [a for a in wb.adjudications_for(iss.issue_id)
                   if a.action is AdjudicationAction.REFUTE and not a.is_revoked]
    assert len(refute_kept) == 1


def test_unanimous_support_resolves_routing(wb):
    iss = _issue(wb)
    wb.adjudicate(iss.issue_id, "r1", ReviewerRole.REVIEWER,
                  AdjudicationAction.SUPPORT, ReviewConfidence.MEDIUM)
    r = wb.adjudicate(iss.issue_id, "r2", ReviewerRole.DOMAIN_EXPERT,
                      AdjudicationAction.SUPPORT, ReviewConfidence.HIGH)
    assert r is AdjudicationAction.SUPPORT
    # SUPPORT 路由后走正常人工迁移；仲裁不代替关闭（W2）
    wb.transition(iss.issue_id, IssueStatus.ASSIGNED, actor="r1")
    wb.transition(iss.issue_id, IssueStatus.IN_REVIEW)
    wb.transition(iss.issue_id, IssueStatus.RESOLVED, note="双 SUPPORT")
    with pytest.raises(PermissionError):
        wb.transition(iss.issue_id, IssueStatus.CLOSED, actor="")  # 仍须人工关闭


def test_revoke_marks_not_deletes_and_rerarbitrates(wb):
    iss = _issue(wb)
    wb.adjudicate(iss.issue_id, "r1", ReviewerRole.REVIEWER,
                  AdjudicationAction.SUPPORT, ReviewConfidence.HIGH)
    r_ref = wb.adjudicate(iss.issue_id, "r2", ReviewerRole.REVIEWER,
                          AdjudicationAction.REFUTE, ReviewConfidence.HIGH)
    assert r_ref is AdjudicationAction.ESCALATE
    adj_r = [a for a in wb.adjudications_for(iss.issue_id)
             if a.action is AdjudicationAction.REFUTE][0]
    after = wb.revoke_adjudication(adj_r.adjudication_id, actor="r2")
    assert after is AdjudicationAction.SUPPORT
    assert len(wb.adjudications_for(iss.issue_id)) == 2  # 记录仍在，只标 revoked


def test_approve_repair_absent_from_action_enum():
    # 修复批准是独立治理命题：裁决动作枚举里没有 APPROVE_REPAIR
    assert not any("REPAIR" in a.value for a in AdjudicationAction)


def test_adjudication_events_in_audit_chain(wb):
    iss = _issue(wb)
    wb.adjudicate(iss.issue_id, "r1", ReviewerRole.REVIEWER,
                  AdjudicationAction.SUPPORT, ReviewConfidence.HIGH)
    types = [e.event_type for e in wb.audit_chain()]
    assert "ISSUE_ADJUDICATED" in types and "ARBITRATION_ROUTED" in types
