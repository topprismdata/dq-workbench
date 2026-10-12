"""三层体检编排：spatial → identity → statistics。

CheckAdapter 协议：各域检查器接入面（WP09 与 geocode-arbiter /
identity-service / mousheng anomalies 做真集成；本包提供 JSONL 回放
adapter 与一个示例规则 adapter）。
编排语义：
- 顺序执行三层；某层抛错不吞——体检失败=本次 run 失败（fail-closed）
- 每层产出的 Finding 全量入工作台并逐条审计
- run 级留痕：run_id 贯穿 CheckReport 与 Finding.source_run
"""
from __future__ import annotations

from typing import Protocol

from dq_workbench.models import (
    CheckReport,
    CheckResult,
    Finding,
    Layer,
    new_id,
)


class CheckAdapter(Protocol):
    layer: Layer

    def run(self, run_id: str) -> CheckReport: ...


class JsonlFindingsAdapter:
    """从 JSONL 回放发现（列: layer,rule_id,subject,severity,message）。

    用于 WP09 集成前的管道验证与历史数据回灌。
    """

    def __init__(self, path: str, layer: Layer):
        self.layer = layer
        self._path = path

    def run(self, run_id: str) -> CheckReport:
        import json

        findings: list[Finding] = []
        with open(self._path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                d = json.loads(line)
                findings.append(Finding(
                    finding_id=new_id("f"),
                    layer=Layer(d.get("layer", self.layer.value)),
                    rule_id=d["rule_id"],
                    subject=d["subject"],
                    severity=d.get("severity", "C2"),
                    message=d.get("message", ""),
                    evidence=d.get("evidence", []),
                    source_run=run_id,
                ))
        result = CheckResult.FINDINGS if findings else CheckResult.PASSED
        return CheckReport(self.layer, run_id, result, findings)


def orchestrate(adapters: list[CheckAdapter]) -> list[CheckReport]:
    """顺序跑三层；异常直接上抛（不静默跳过）。"""
    run_id = new_id("run")
    reports = []
    for ad in adapters:
        rep = ad.run(run_id)
        # 回放层可能给错层标签：以编排注册层为准
        for f in rep.findings:
            f.layer = ad.layer
        reports.append(rep)
    return reports
