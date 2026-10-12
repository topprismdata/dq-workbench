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
