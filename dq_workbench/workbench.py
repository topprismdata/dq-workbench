"""工作台存储：工单状态机 + 追加式审计。

验收不变量（测试锁定）：
- W1 拒绝/异常发现永不落主数据：本包没有任何主数据写路径
     （grep 级证据：模块无 outlet/identity 写接口；测试断言 findings 只进
      workbench 自己的库）
- W2 工单只能人工关闭：close() 必须给 actor；系统路径无法关闭
- W3 一切写操作产生 AuditEvent，事件链 prev_event_id 连续
- W4 状态机合法迁移；非法迁移拒绝
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from dq_workbench.models import (
    AuditEvent,
    Finding,
    Issue,
    IssueStatus,
    new_id,
    now_iso,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
  finding_id TEXT PRIMARY KEY, doc JSON NOT NULL
);
CREATE TABLE IF NOT EXISTS issues (
  issue_id TEXT PRIMARY KEY, status TEXT, doc JSON NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  event_id TEXT PRIMARY KEY, at TEXT, doc JSON NOT NULL
);
"""

# 合法状态迁移（REOPENED 可再走一轮）
_TRANSITIONS = {
    IssueStatus.OPEN: {IssueStatus.ASSIGNED, IssueStatus.CLOSED},
    IssueStatus.ASSIGNED: {IssueStatus.IN_REVIEW, IssueStatus.CLOSED},
    IssueStatus.IN_REVIEW: {IssueStatus.RESOLVED, IssueStatus.CLOSED, IssueStatus.REOPENED},
    IssueStatus.RESOLVED: {IssueStatus.CLOSED, IssueStatus.REOPENED},
    IssueStatus.REOPENED: {IssueStatus.ASSIGNED, IssueStatus.CLOSED},
    IssueStatus.CLOSED: set(),
}


class IllegalTransition(Exception):
    pass


class Workbench:
    def __init__(self, db_path: str | Path = "workbench.db"):
        self.db = sqlite3.connect(str(db_path))
        self.db.executescript(_SCHEMA)
        self._last_event_id = self._latest_event_id()

    # ---- findings ----
    def ingest(self, findings: list[Finding], actor: str = "orchestrator") -> list[str]:
        """发现入库：只进工作台库，永不触碰主数据（W1）。"""
        ids = []
        for f in findings:
            self.db.execute("INSERT OR REPLACE INTO findings VALUES (?,?)",
                            (f.finding_id, json.dumps(f.__dict__, ensure_ascii=False, default=str)))
            ids.append(f.finding_id)
        self.db.commit()
        self._audit("FINDINGS_INGESTED", {"n": len(ids), "finding_ids": ids}, actor)
        return ids

    def finding(self, finding_id: str) -> Finding | None:
        row = self.db.execute("SELECT doc FROM findings WHERE finding_id=?",
                              (finding_id,)).fetchone()
        return self._load_finding(row[0]) if row else None

    @staticmethod
    def _load_finding(doc: str) -> Finding:
        from dq_workbench.models import Layer, Severity

        d = json.loads(doc)
        return Finding(
            finding_id=d["finding_id"], layer=Layer(d["layer"]), rule_id=d["rule_id"],
            subject=d["subject"], severity=Severity(d["severity"]), message=d["message"],
            evidence=d.get("evidence", []), source_run=d.get("source_run", ""),
            detected_at=d.get("detected_at", ""))

    # ---- issues ----
    def open_issue(self, finding_ids: list[str], title: str, sla_due: str = "") -> Issue:
        missing = [fid for fid in finding_ids if self.finding(fid) is None]
        if missing:
            raise KeyError(f"findings 不存在: {missing}")
        issue = Issue(issue_id=new_id("iss"), finding_ids=finding_ids, title=title,
                      sla_due=sla_due)
        self._put_issue(issue)
        self._audit("ISSUE_CREATED", {"issue_id": issue.issue_id, "findings": finding_ids})
        return issue

    def transition(self, issue_id: str, to: IssueStatus, actor: str = "",
                   note: str = "") -> Issue:
        issue = self._get_issue(issue_id)
        if issue is None:
            raise KeyError(issue_id)
        if to not in _TRANSITIONS[issue.status]:
            raise IllegalTransition(f"{issue.status.value} → {to.value} 非法")
        if to is IssueStatus.CLOSED:
            # W2：只能人工关闭——必须给 actor
            if not actor or not actor.strip():
                raise PermissionError("工单只能人工关闭（W2）：actor 必填")
            issue.closed_by = actor.strip()
            issue.closed_at = now_iso()
        issue.status = to
        if note:
            issue.resolution_note = note
        if to is IssueStatus.ASSIGNED and actor:
            issue.assignee = actor
        self._put_issue(issue)
        self._audit(f"ISSUE_{to.value}", {"issue_id": issue_id, "actor": actor, "note": note})
        return issue

    def overdue(self, today: str | None = None) -> list[str]:
        import datetime

        day = today or datetime.date.today().isoformat()
        rows = self.db.execute(
            "SELECT issue_id, doc FROM issues WHERE status NOT IN ('CLOSED')").fetchall()
        out = []
        for iid, doc in rows:
            d = json.loads(doc)
            if d.get("sla_due") and d["sla_due"] < day:
                out.append(iid)
        return out

    # ---- audit ----
    def _audit(self, event_type: str, payload: dict, actor: str = "") -> None:
        ev = AuditEvent(event_id=new_id("evt"), event_type=event_type,
                        payload=payload, actor=actor, prev_event_id=self._last_event_id)
        self.db.execute("INSERT INTO audit_events VALUES (?,?,?)",
                        (ev.event_id, ev.at, json.dumps(ev.__dict__, ensure_ascii=False, default=str)))
        self.db.commit()
        self._last_event_id = ev.event_id

    def audit_chain(self) -> list[AuditEvent]:
        rows = self.db.execute("SELECT doc FROM audit_events ORDER BY rowid").fetchall()
        out = []
        for (doc,) in rows:
            d = json.loads(doc)
            out.append(AuditEvent(event_id=d["event_id"], event_type=d["event_type"],
                                  payload=d.get("payload", {}), actor=d.get("actor", ""),
                                  at=d["at"], prev_event_id=d.get("prev_event_id", "")))
        return out

    # ---- 内部 ----
    def _put_issue(self, issue: Issue) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO issues VALUES (?,?,?)",
            (issue.issue_id, issue.status.value,
             json.dumps(issue.__dict__, ensure_ascii=False)))
        self.db.commit()

    def _get_issue(self, issue_id: str) -> Issue | None:
        row = self.db.execute("SELECT doc FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
        if not row:
            return None
        d = json.loads(row[0])
        return Issue(
            issue_id=d["issue_id"], finding_ids=d["finding_ids"], title=d["title"],
            status=IssueStatus(d["status"]), assignee=d.get("assignee", ""),
            sla_due=d.get("sla_due", ""), created_at=d.get("created_at", ""),
            closed_by=d.get("closed_by", ""), closed_at=d.get("closed_at", ""),
            resolution_note=d.get("resolution_note", ""))

    def _latest_event_id(self) -> str:
        row = self.db.execute(
            "SELECT event_id FROM audit_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else ""

    def close_db(self) -> None:
        self.db.close()
