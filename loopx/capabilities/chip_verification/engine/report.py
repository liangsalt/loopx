"""Human-readable projections; report files never become authoritative state."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path

from .workflow import save_json, effective_planning_tasks, superseded_plan_ids


def _agent_receipts(project, tasks):
    """Count accepted current worker executions, never abandoned attempt files."""
    done = {task["id"]: task for task in tasks if task["status"] == "done"}
    receipts, excluded, identities, agent_tasks = [], [], set(), set()
    for artifact in project.store.list_artifacts(validity="current"):
        task = done.get(artifact["task_id"])
        path = project.root / artifact["path"]
        if task is None or path.name != "receipt.json":
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            runtime, session_id = receipt.get("runtime"), receipt.get("session_id")
            if not isinstance(runtime, str) or runtime not in {"codex", "pi"} or not isinstance(session_id, str) or not session_id.strip():
                raise ValueError("No supported agent runtime and nonempty session identity")
            if receipt.get("returncode") != 0 or receipt.get("attempt_id") != artifact["attempt_id"]:
                raise ValueError("Receipt does not identify this accepted successful attempt")
            prompt_chars = receipt.get("prompt_chars")
            if type(prompt_chars) is not int or prompt_chars < 0:
                raise ValueError("Receipt has no valid measured prompt length")
        except (OSError, ValueError, AttributeError) as error:
            excluded.append({"task_id": task["id"], "receipt": str(path), "reason": str(error)})
            continue
        identities.add((runtime, session_id))
        agent_tasks.add(task["id"])
        receipts.append({"runtime": runtime, "session_id": session_id, "task_id": task["id"],
                         "attempt_id": artifact["attempt_id"], "prompt_chars": prompt_chars,
                         "usage": receipt.get("usage"), "receipt": str(path)})
    return receipts, excluded, identities, agent_tasks


def _proposal_ancestors(task, tasks, agent_tasks):
    """A run only demonstrates agent iteration when it consumes that proposal."""
    proposals, visited, pending = [], set(), list(task["depends_on"])
    while pending:
        identifier = pending.pop()
        if identifier in visited:
            continue
        visited.add(identifier)
        ancestor = tasks.get(identifier)
        if ancestor is None or ancestor["status"] != "done":
            continue
        if ancestor["kind"] == "analyze" and "proposal" in ancestor["result"] and identifier in agent_tasks:
            proposals.append(identifier)
        pending.extend(ancestor["depends_on"])
    return sorted(proposals)


def write_report(project, output):
    target = Path(output)
    if not target.is_absolute():
        target = project.root / target
    target.mkdir(parents=True, exist_ok=True)
    initial_status = project.status()
    from .verification import validate_simulation_evidence, verify_hashes
    from .models import ValidationError
    validated_runs, invalidated_runs = [], []
    for task in project.store.list_tasks(status="done", kind="regress"):
        # A failed predecessor may already have invalidated this descendant.
        if project.store.get_task(task["id"])["status"] != "done":
            continue
        evidence = task["result"].get("evidence", {})
        if evidence.get("status") != "passed":
            continue
        try:
            verdict = validate_simulation_evidence(evidence, task["payload"]["benchmark"], task["payload"]["inputs"])
        except Exception as error:
            invalidated = project.store.invalidate(task["id"], "Run evidence no longer current: " + str(error))
            invalidated_runs.append({"task_id": task["id"], "reason": str(error), "invalidated_tasks": invalidated})
        else:
            validated_runs.append({"task_id": task["id"],
                                   "evidence": {**evidence, "coverage": verdict["measured"]["coverage"]}})
    validated_generic = set()
    for task in project.store.list_tasks(status="done"):
        evidence = task["result"].get("evidence", {})
        if task["kind"] not in {"build", "smoke"} or evidence.get("backend") != "vivado_xsim_generic":
            continue
        if project.store.get_task(task["id"])["status"] != "done":
            continue
        try:
            from .simulators.generic import validate_generic_evidence
            inputs = task["payload"]["inputs"]
            verify_hashes(inputs)
            if {entry["path"]: entry["sha256"] for entry in evidence.get("inputs", [])} != inputs:
                raise ValidationError("Generic run inputs differ from the accepted task baseline")
            verdict = validate_generic_evidence(evidence, task["payload"]["manifest"], phase=task["kind"])
            if verdict.get("accepted") is not True or verdict.get("phase") != task["kind"] or verdict.get("build_passed") is not True:
                raise ValidationError(verdict.get("reason", "Generic build evidence was not accepted"))
            if task["kind"] == "smoke" and (verdict.get("smoke_passed") is not True or verdict.get("verified") is not True):
                raise ValidationError("Generic smoke has no accepted transaction/checker activity proof")
        except Exception as error:
            invalidated = project.store.invalidate(task["id"], "Run evidence no longer current: " + str(error))
            invalidated_runs.append({"task_id": task["id"], "phase": task["kind"], "reason": str(error),
                                     "invalidated_tasks": invalidated})
        else:
            validated_generic.add(task["id"])
    # Evidence revalidation mutates lifecycle state. Every projection below
    # must use the resulting snapshot, including downstream agent receipts.
    status = project.status()
    status["changed_artifacts"] = initial_status.get("changed_artifacts", []) + status.get("changed_artifacts", [])
    tasks = project.store.list_tasks()
    done = [task for task in tasks if task["status"] == "done"]
    by_id = {task["id"]: task for task in tasks}
    dispositions = Counter()
    for task in done:
        if task["kind"] == "model":
            dispositions.update(entry["classification"] for entry in task["result"]["dispositions"])
    receipts, excluded_receipts, sessions, agent_tasks = _agent_receipts(project, tasks)
    prompt_sizes = [receipt["prompt_chars"] for receipt in receipts]
    input_tokens = [receipt["usage"].get("input_tokens", 0) for receipt in receipts
                    if isinstance(receipt["usage"], dict)]
    input_tokens = [value for value in input_tokens if type(value) is int and value >= 0]
    accepted_runs = []
    for record in validated_runs:
        task = by_id.get(record["task_id"])
        if task and task["status"] == "done":
            proposals = _proposal_ancestors(task, by_id, agent_tasks)
            accepted_runs.append({**record["evidence"], "task_id": task["id"],
                                  "agent_proposal_tasks": proposals})
    best = max(accepted_runs, key=lambda item: (item["coverage"]["percent"], bool(item["agent_proposal_tasks"])), default=None)
    agent_run = max((run for run in accepted_runs if run["agent_proposal_tasks"]),
                    key=lambda item: item["coverage"]["percent"], default=None)
    issues = project.store.list_issues()
    data = {"generated_at": datetime.now(timezone.utc).isoformat(), "status": status,
            "spec_bytes": status["baseline"]["spec_bytes"], "source_dispositions": dict(dispositions),
            "source_disposition_count": sum(dispositions.values()),
            "unique_agent_sessions": len(sessions), "max_prompt_chars": max(prompt_sizes, default=0),
            "max_measured_input_tokens": max(input_tokens, default=0),
            "agent_receipts": receipts, "excluded_agent_receipts": excluded_receipts,
            "invalidated_simulations": invalidated_runs, "accepted_simulations": len(accepted_runs),
            "generic_builds": sum(task["kind"] == "build" and task["id"] in validated_generic for task in done),
            "generic_smokes": sum(task["kind"] == "smoke" and task["id"] in validated_generic for task in done),
            "best_coverage": best["coverage"] if best else None,
            "best_run": best, "issues": issues,
            "all_spec_signoff": False,
            "limits": ["Functional coverage is limited to each run's frozen bin model and configured DUT; it is not code coverage or full specification signoff.",
                       "Configurations and requirements outside the accepted run's explicit scope remain unverified.",
                       "Source inventory completeness and candidate requirements are distinct from semantic signoff.",
                       "Only accepted real EDA runs appear in measured coverage; transport fixtures are excluded.",
                       "Generic UVM source generation produces candidates; benchmark coverage does not establish that those candidates compiled or ran."]}
    planning_tasks = effective_planning_tasks(tasks)
    data["superseded_plan_tasks"] = sorted(superseded_plan_ids(tasks))
    data["source_to_plan_tasks_complete"] = bool(planning_tasks) and all(task["status"] == "done" for task in planning_tasks)
    data["planning_tasks"] = {kind: {"total": sum(task["kind"] == kind for task in planning_tasks),
                                     "done": sum(task["kind"] == kind and task["status"] == "done" for task in planning_tasks)}
                              for kind in ("model", "plan")}
    data["agent_stimulus_iteration"] = any(run["agent_proposal_tasks"] for run in accepted_runs)
    data["best_run_agent_stimulus_iteration"] = bool(best and best["agent_proposal_tasks"])
    data["agent_iteration_run"] = agent_run["task_id"] if agent_run else None
    data["agent_iteration_coverage"] = agent_run["coverage"] if agent_run else None
    data["generated_uvm_candidate_components"] = sum(task["kind"] == "implement" and task["result"].get("candidate_only", False) for task in done)
    data["benchmark_acceptance"] = bool(data["spec_bytes"] >= 200 * 1024 and len(sessions) >= 2 and
                                         data["source_disposition_count"] == status["source_count"] and
                                         data["source_to_plan_tasks_complete"] and agent_run and
                                         agent_run["coverage"]["percent"] >= 90.0)
    save_json(target / "summary.json", data)
    coverage = data["best_coverage"]
    coverage_text = f"{coverage['percent']:.2f}% ({coverage['hit_bins']}/{coverage['total_bins']})" if coverage else "尚无有效仿真证据"
    lines = ["# LoopX 芯片验证报告", "", f"生成时间（UTC）：{data['generated_at']}", "",
             f"- 规格大小：{data['spec_bytes']:,} 字节", f"- 来源片段：{status['source_count']}",
             f"- 已分类来源：{data['source_disposition_count']}", f"- 独立 agent 会话：{len(sessions)}",
             f"- 单次最大任务输入：{data['max_prompt_chars']:,} 字符",
             f"- 实测最大模型输入：{data['max_measured_input_tokens']:,} tokens（包括宿主提示）",
             f"- 通用 UVM 编译 / 活动检查：{data['generic_builds']} / {data['generic_smokes']}（不计覆盖率）",
             f"- 验收的真实仿真运行：{len(accepted_runs)}", f"- 功能覆盖率：{coverage_text}", "",
             "## 验证范围", "", status["scope"], "", "## 实测证据", ""]
    for run in accepted_runs:
        c = run["coverage"]
        lines.append(f"- {run['run_dir']}: {c['hit_bins']}/{c['total_bins']} = {c['percent']:.2f}%; checks={run['checks']}")
    lines.extend(["", "## 当前工作与问题", "", f"任务状态：{status['tasks']}", "",
                  f"未解决的规格/引用问题：{len(issues)}。这些问题保留在状态中，不因覆盖率达标自动关闭。", "",
                  "## 边界", ""])
    lines.extend("- " + item for item in data["limits"])
    (target / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    rows = "".join(f"<tr><td>{escape(t['kind'])}</td><td>{escape(t['title'])}</td><td>{escape(t['status'])}</td></tr>" for t in tasks)
    run_rows = "".join(
        f"<tr><td>{'Codex/Pi 激励迭代' if run['agent_proposal_tasks'] else '初始或独立运行'}</td>"
        f"<td><progress max='100' value='{run['coverage']['percent']:.6f}'></progress> "
        f"{run['coverage']['percent']:.2f}% ({run['coverage']['hit_bins']}/{run['coverage']['total_bins']})</td>"
        f"<td>{(run['checks']['checks'] if isinstance(run['checks'], dict) else run['checks']):,}</td>"
        f"<td>{escape(str(run.get('uvm_errors', '未记录')))} / {escape(str(run.get('uvm_fatals', '未记录')))}</td></tr>"
        for run in accepted_runs)
    planning_text = "；".join(f"{label} {data['planning_tasks'][kind]['done']}/{data['planning_tasks'][kind]['total']}"
                              for kind, label in (("model", "来源解释任务"), ("plan", "测试计划任务")))
    html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>LoopX 芯片验证报告</title>
<style>body{{font:16px system-ui;margin:40px auto;max-width:1100px;padding:0 24px;color:#182b43;background:#f6f8fb}}h1{{font-size:32px}}.cards{{display:flex;gap:16px;flex-wrap:wrap}}.card{{padding:20px;background:white;border:1px solid #dbe3ec;border-radius:12px;min-width:180px}}b{{display:block;font-size:24px;margin-top:8px}}table{{border-collapse:collapse;width:100%;background:white}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #e3e9ef}}p{{line-height:1.7}}.note{{background:#fff4d8;padding:16px;border-radius:8px}}</style>
<h1>LoopX：规格 → UVM → 仿真证据</h1><p>验证状态与 agent session 分离。以下数字来自已验收文件与实际运行。</p>
<div class="cards"><div class="card">规格大小<b>{data['spec_bytes']:,} B</b></div><div class="card">独立会话<b>{len(sessions)}</b></div><div class="card">功能覆盖率<b>{escape(coverage_text)}</b></div><div class="card">最大输入 tokens<b>{data['max_measured_input_tokens']:,}</b></div></div>
<h2>验证范围</h2><p>{escape(status['scope'])}</p><p class="note">功能覆盖率仅对应验收运行的固定功能目标与 DUT 配置。不是 RTL 代码覆盖率或整份文档的全部需求签核。未解决问题仍保留：{len(issues)} 项。</p>
<h2>实际覆盖迭代</h2><table><tr><th>运行来源</th><th>固定功能目标</th><th>检查次数</th><th>UVM error / fatal</th></tr>{run_rows}</table>
<h2>规格与工程进展</h2><p>已分类来源 {data['source_disposition_count']}/{status['source_count']}；{planning_text}。任务完成表示结构验收，规格歧义仍需审查。</p>
<p>通用生成候选 {data['generated_uvm_candidate_components']} 个；通用构建 / 活动验收 {data['generic_builds']} / {data['generic_smokes']}。上方覆盖率属于公开参考 UVM 环境，不能用来证明这些通用候选已运行。</p>
<details><summary>查看全部工作项及历史状态</summary><table><tr><th>阶段</th><th>任务</th><th>状态</th></tr>{rows}</table></details>
<p>详细证据：summary.json；文字报告：REPORT.md。生成于 {escape(data['generated_at'])}。</p></html>"""
    (target / "index.html").write_text(html, encoding="utf-8")
    return {"report": str(target / "REPORT.md"), "html": str(target / "index.html"),
            "summary": str(target / "summary.json"), "benchmark_acceptance": data["benchmark_acceptance"]}
