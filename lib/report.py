"""
报告生成

纯 CLI 端报告生成，不调用 Claude。
提供三个阶段的报告和统计信息：
- generate_confirm_report: 阶段 2（确认清单）
- generate_verify_report: 阶段 B（验证结果）
- generate_final_report: 阶段 D（最终报告）
- generate_stats: 统计摘要
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ==================== 阶段 2：确认清单 ====================

def generate_confirm_report(gaps: list, findings: list) -> str:
    """
    生成阶段 2 确认清单报告。

    展示按模块分组的测试盲区，每个盲区附带佐证 bug 列表。
    用户可以选择排除某些盲区，或全部确认后进入执行阶段。

    参数:
        gaps: 测试覆盖缺口列表，每个元素期望包含：
            - module: 模块名
            - language: 模块语言
            - gap_name: 盲区名称
            - infra_plan: 基础设施补强计划
            - finding_ids: 关联的 bug ID 列表
        findings: 扫描发现列表，每个元素期望包含：
            - id: bug 标识
            - severity: 严重级别
            - file: 文件路径
            - line: 行号
            - description: 描述

    返回:
        格式化的确认清单字符串
    """
    if not gaps:
        return "未发现测试盲区，无需确认。\n"

    # 按模块分组
    modules = {}
    for gap in gaps:
        mod = gap.get("module", "unknown")
        modules.setdefault(mod, []).append(gap)

    # 构建 finding 索引（按 ID 查找）
    finding_index = {f.get("id"): f for f in findings if f.get("id")}

    lines = []
    total_gaps = 0
    total_bugs = set()

    for mod, mod_gaps in modules.items():
        lines.append(f"## 模块：{mod}")
        lines.append("")

        for i, gap in enumerate(mod_gaps, 1):
            total_gaps += 1
            gap_name = gap.get("gap_name", "未命名盲区")
            infra_plan = gap.get("infra_plan", "无计划")
            finding_ids = gap.get("evidence_finding_ids", gap.get("finding_ids", []))

            lines.append(f"### 盲区 {total_gaps}：{gap_name}（{len(finding_ids)} 个同类问题）")
            lines.append(f"  - 缺口：{infra_plan}")
            lines.append(f"  - 佐证 bug：")

            for fid in finding_ids:
                total_bugs.add(fid)
                f = finding_index.get(fid, {})
                severity = f.get("severity", "?")
                file = f.get("file", "?")
                line_num = f.get("line", "?")
                desc = f.get("description", "无描述")
                lines.append(f"    [{fid}] {severity} {file}:{line_num} — {desc}")

            lines.append("")

    # 汇总行（操作提示由 confirm.py 的 input() 负责，报告不重复）
    lines.append(f"以上 {total_gaps} 个盲区，涉及 {len(total_bugs)} 个 bug。")

    return "\n".join(lines)


# ==================== 阶段 B：验证结果 ====================

def generate_verify_report(results: dict, findings: list) -> str:
    """
    生成阶段 B 验证结果报告。

    展示每个 bug 的验证状态（已验证/幻觉/修复失败/未验证），
    以及建议写入 CLAUDE.md 的架构约束和幻觉记录。

    参数:
        results: 验证结果字典 {finding_id: BugResult}，BugResult 包含：
            - status: 验证状态（verified/hallucination/fix_failed/unverified/skipped）
            - reason: 原因说明
            - test_file: 关联的测试文件
        findings: 扫描发现列表

    返回:
        格式化的验证结果字符串
    """
    # 构建 finding 索引
    finding_index = {f.get("id"): f for f in findings if f.get("id")}

    # 状态标记映射
    status_marks = {
        "verified": "pass",
        "hallucination": "fail",
        "fix_failed": "warn",
        "unverified": "unknown",
        "skipped": "skip",
    }

    lines = []
    lines.append("## 验证结果")
    lines.append("")
    lines.append("| # | 标记 | 模块 | bug | 修复 |")
    lines.append("|---|------|------|-----|------|")

    hallucinations = []
    constraints = []
    idx = 0

    for fid, result in results.items():
        idx += 1
        f = finding_index.get(fid, {})
        # 从 result 中提取状态（支持 dict 和 dataclass 两种格式）
        status = _get_result_field(result, "status", "unknown")
        reason = _get_result_field(result, "reason", "")
        test_file = _get_result_field(result, "test_file", "")

        mark = _status_to_mark(status)
        module = f.get("module", "?")
        desc = f.get("description", fid)
        fix_info = test_file if test_file else reason

        lines.append(f"| {idx} | {mark} | {module} | {desc} | {fix_info} |")

        # 收集幻觉记录
        if status == "hallucination":
            hallucinations.append({
                "id": fid,
                "description": desc,
                "reason": reason,
            })

        # 已验证的 bug 可能产生架构约束建议
        if status == "verified" and f.get("constraint"):
            constraints.append({
                "constraint": f.get("constraint"),
                "source_bug": fid,
                "detail": f.get("constraint_detail", ""),
            })

    # 架构约束建议
    if constraints:
        lines.append("")
        lines.append("### 建议写入 CLAUDE.md 的架构约束")
        lines.append("")
        lines.append("| # | 约束 | 来源 bug | 详情 |")
        lines.append("|---|------|----------|------|")
        for i, c in enumerate(constraints, 1):
            lines.append(f"| {i} | {c['constraint']} | {c['source_bug']} | {c['detail']} |")

    # 幻觉记录
    if hallucinations:
        lines.append("")
        lines.append("### 幻觉记录")
        lines.append("")
        lines.append("| # | 声称的 bug | 实际情况 |")
        lines.append("|---|-----------|----------|")
        for i, h in enumerate(hallucinations, 1):
            lines.append(f"| {i} | {h['description']} | {h['reason']} |")

    return "\n".join(lines)


# ==================== 阶段 D：最终报告 ====================

def generate_final_report(state) -> str:
    """
    生成阶段 D 最终报告。

    汇总整个 review 流程的成果：测试体系强化、基础设施更新、
    bug 修复、验证统计、架构约束建议、幻觉记录。

    参数:
        state: ReviewState 实例，包含 findings/results/gaps 等完整状态

    返回:
        格式化的最终报告字符串
    """
    findings = _get_state_field(state, "findings", [])
    results = _get_state_field(state, "results", {})
    gaps = _get_state_field(state, "gaps", [])

    # 构建索引
    finding_index = {f.get("id"): f for f in findings if f.get("id")}
    stats = generate_stats(state)

    lines = []
    lines.append("## Review 报告")
    lines.append("")

    # ---- 测试体系强化 ----
    lines.append("### 测试体系强化")
    lines.append("")
    lines.append("| 模块 | 新增基础设施 | 能自动抓住的问题类型 | 验证结果 |")
    lines.append("|------|-------------|---------------------|----------|")

    # 按模块汇总 gaps 中的基础设施信息
    module_infra = {}
    for gap in gaps:
        mod = gap.get("module", "unknown")
        module_infra.setdefault(mod, []).append(gap)

    for mod, mod_gaps in module_infra.items():
        for gap in mod_gaps:
            infra = gap.get("infra_plan", "—")
            catch_type = gap.get("gap_name", "—")
            # 统计该 gap 关联 bug 的验证结果
            gap_fids = gap.get("evidence_finding_ids", gap.get("finding_ids", []))
            verified_count = sum(
                1 for fid in gap_fids
                if _get_result_field(results.get(fid, {}), "status", "") == "verified"
            )
            total_count = len(gap_fids)
            verify_label = f"{verified_count}/{total_count} 已验证" if total_count > 0 else "—"
            lines.append(f"| {mod} | {infra} | {catch_type} | {verify_label} |")

    if not module_infra:
        lines.append("| — | — | — | — |")

    # ---- 基础设施更新（Phase C） ----
    lines.append("")
    lines.append("### 基础设施更新（Phase C）")
    lines.append("")
    lines.append("| 类型 | 新增项 | 说明 |")
    lines.append("|------|-------|------|")

    phase_c1 = _get_state_field(state, "phase_c1_done", False)
    phase_c2 = _get_state_field(state, "phase_c2_done", False)

    if phase_c1:
        lines.append("| 回归测试 | 已完成 | 针对已验证 bug 的回归测试 |")
    if phase_c2:
        lines.append("| gate 规则 | 已完成 | test-governance-gate 新增规则 |")

    high_freq = _get_state_field(state, "high_freq_rules", [])
    for rule in high_freq:
        # high_freq_rules 存的是字符串（规则 ID），不是 dict
        if isinstance(rule, dict):
            lines.append(f"| 高频规则 | {rule.get('name', '?')} | {rule.get('description', '')} |")
        else:
            lines.append(f"| 高频规则 | {rule} | ≥10 次触发 |")

    if not phase_c1 and not phase_c2 and not high_freq:
        lines.append("| — | — | — |")

    # ---- 附带修复的 bug ----
    lines.append("")
    lines.append("### 附带修复的 bug")
    lines.append("")
    lines.append("| # | 标记 | 模块 | bug | 修复 |")
    lines.append("|---|------|------|-----|------|")

    idx = 0
    hallucinations = []
    constraints = []

    for fid, result in results.items():
        idx += 1
        f = finding_index.get(fid, {})
        status = _get_result_field(result, "status", "unknown")
        reason = _get_result_field(result, "reason", "")
        test_file = _get_result_field(result, "test_file", "")

        mark = _status_to_mark(status)
        module = f.get("module", "?")
        desc = f.get("description", fid)
        fix_info = test_file if test_file else reason

        lines.append(f"| {idx} | {mark} | {module} | {desc} | {fix_info} |")

        if status == "hallucination":
            hallucinations.append({
                "id": fid,
                "description": desc,
                "reason": reason,
            })

        if status == "verified" and f.get("constraint"):
            constraints.append({
                "constraint": f.get("constraint"),
                "source_bug": fid,
            })

    if idx == 0:
        lines.append("| — | — | — | — | — |")

    # ---- 验证统计 ----
    lines.append("")
    lines.append("### 验证统计")
    lines.append("")
    lines.append("| 发现数 | 已验证 | 幻觉 | 未验证 |")
    lines.append("|--------|--------|------|--------|")
    lines.append(
        f"| {stats['total']} "
        f"| {stats['verified']} "
        f"| {stats['hallucination']} "
        f"| {stats['unverified']} |"
    )

    # ---- 架构约束建议 ----
    lines.append("")
    lines.append("### 架构约束建议")
    lines.append("")
    if constraints:
        lines.append("| # | 约束 | 来源 |")
        lines.append("|---|------|------|")
        for i, c in enumerate(constraints, 1):
            lines.append(f"| {i} | {c['constraint']} | {c['source_bug']} |")
    else:
        lines.append("无新增架构约束建议。")

    # ---- 幻觉记录 ----
    lines.append("")
    lines.append("### 幻觉记录")
    lines.append("")
    if hallucinations:
        lines.append("| # | 声称的 bug | 实际情况 |")
        lines.append("|---|-----------|----------|")
        for i, h in enumerate(hallucinations, 1):
            lines.append(f"| {i} | {h['description']} | {h['reason']} |")
    else:
        lines.append("无幻觉记录。")

    # ---- 被语言过滤器过滤的 findings（审计） ----
    filtered = _get_state_field(state, "filtered_findings", [])
    if filtered:
        lines.append("")
        lines.append("### 被语言运行时过滤器排除的 findings（审计）")
        lines.append("")
        lines.append("| # | 模块 | 类别 | 文件 | 描述 |")
        lines.append("|---|------|------|------|------|")
        for i, f in enumerate(filtered, 1):
            lines.append(
                f"| {i} | {f.get('module', '?')} | {f.get('category', '?')} "
                f"| {f.get('file', '?')}:{f.get('line', '?')} "
                f"| {f.get('description', '?')[:80]} |"
            )

    return "\n".join(lines)


# ==================== 统计信息 ====================

def generate_stats(state) -> dict:
    """
    生成验证统计摘要。

    参数:
        state: ReviewState 实例或任何含 findings/results 字段的对象

    返回:
        dict，包含以下键：
            - total: 总发现数
            - verified: 已验证数
            - hallucination: 幻觉数
            - fix_failed: 修复失败数
            - unverified: 未验证数
            - skipped: 跳过数
    """
    findings = _get_state_field(state, "findings", [])
    results = _get_state_field(state, "results", {})

    total = len(findings)
    counts = {
        "verified": 0,
        "hallucination": 0,
        "fix_failed": 0,
        "unverified": 0,
        "skipped": 0,
    }

    for fid, result in results.items():
        status = _get_result_field(result, "status", "unverified")
        if status in counts:
            counts[status] += 1

    # 没有 result 记录的 finding 算 unverified
    result_ids = set(results.keys())
    finding_ids = {f.get("id") for f in findings if f.get("id")}
    missing = finding_ids - result_ids
    counts["unverified"] += len(missing)

    return {
        "total": total,
        **counts,
    }


# ==================== 内部辅助函数 ====================

def _status_to_mark(status: str) -> str:
    """将验证状态转换为可读标记"""
    marks = {
        "verified": "v",
        "hallucination": "x",
        "fix_failed": "!",
        "unverified": "?",
        "skipped": "-",
    }
    return marks.get(status, "?")


def _get_result_field(result, field_name: str, default=None):
    """从 BugResult（dataclass 或 dict）中安全提取字段"""
    if isinstance(result, dict):
        return result.get(field_name, default)
    return getattr(result, field_name, default)


def _get_state_field(state, field_name: str, default=None):
    """从 ReviewState（dataclass 或 dict）中安全提取字段"""
    if isinstance(state, dict):
        return state.get(field_name, default)
    return getattr(state, field_name, default)
