"""阶段 1：代码扫描（opus，按模块并行，聚焦变更文件 + 边界展开）"""

import logging, json
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


def run_scan(state, project_root, modules):
    """R1 标准扫描：按模块并行调用 claude。

    改进点：
    - scope 从整个 src_dir 缩窄到 git diff 具体变更文件
    - 有跨模块边界时，注入对端文件 + 边界检查指令
    - 有关联 P0 场景时，注入场景信息引导扫描方向

    返回所有 findings 列表。
    """
    from lib.claude import call_claude_bare
    from lib.prompts.scan import (
        SCAN_PROMPT, BOUNDARY_SECTION_TEMPLATE, P0_SECTION_TEMPLATE,
    )
    from lib.schemas.findings import FINDINGS_SCHEMA
    from lib.filters import get_runtime_facts, filter_findings

    # 从 state 获取边界展开信息（scope.py 已写入）
    changed_by_module = getattr(state, "changed_by_module", {})
    boundary_context = getattr(state, "boundary_context", {})
    p0_context = getattr(state, "p0_context", [])

    all_findings = []

    with ThreadPoolExecutor(max_workers=max(1, len(modules))) as pool:
        futures = {}
        for m in modules:
            prompt = _build_scan_prompt(
                m, state, changed_by_module, boundary_context, p0_context,
            )

            timeout = m.estimate_timeout(project_root, task="scan")
            future = pool.submit(
                call_claude_bare,
                prompt=prompt,
                model="opus",
                tools="Read,Glob,Grep",
                output_schema=FINDINGS_SCHEMA,
                max_turns=30,
                cwd=project_root,
                timeout=timeout,
            )
            futures[future] = m

        for future in as_completed(futures):
            m = futures[future]
            try:
                result = future.result()
                findings = _extract_findings(result, m.name)

                # 过滤不可能的 bug，保留审计记录
                findings, filtered_out = filter_findings(findings, m.language)
                if filtered_out:
                    state.filtered_findings.extend(filtered_out)
                    logger.info(
                        "%s: 过滤了 %d 个不可能的 bug: %s",
                        m.name, len(filtered_out),
                        ", ".join(f.get("id", "?") for f in filtered_out),
                    )

                logger.info(f"{m.name}: 发现 {len(findings)} 个问题")
                all_findings.extend(findings)
            except Exception as e:
                logger.error(f"{m.name} 扫描失败: {e}")

    # 更新 finding ID 为全局唯一
    for i, f in enumerate(all_findings, 1):
        f["id"] = f"F{i}"

    state.findings = all_findings
    return all_findings


def _build_scan_prompt(module, state, changed_by_module, boundary_context, p0_context):
    """组装单个模块的扫描 prompt。

    三层信息注入：
    1. 基础层：模块信息 + 运行时约束 + 高频提示 + 变更文件列表
    2. 边界层（可选）：跨模块边界文件对 + 检查指令
    3. P0 层（可选）：关联的 P0 场景
    """
    from lib.filters import get_runtime_facts
    from lib.prompts.scan import (
        SCAN_PROMPT, BOUNDARY_SECTION_TEMPLATE, P0_SECTION_TEMPLATE,
    )

    runtime_facts = get_runtime_facts(module.language)
    high_freq = "\n".join(
        f"- {r}" for r in state.high_freq_rules
    ) if state.high_freq_rules else "无"

    # --- 变更文件列表 ---
    module_files = changed_by_module.get(module.name, [])
    if module_files:
        changed_files_section = "以下是本次变更涉及的文件，请优先精读：\n" + "\n".join(
            f"- {f}" for f in module_files
        )
    else:
        changed_files_section = (
            f"未检测到具体变更文件，请扫描 {module.src_dir} 目录。"
        )

    # --- 边界检查段落 ---
    boundary_info = boundary_context.get(module.name)
    if boundary_info:
        boundary_files = boundary_info.get("boundary_files", [])
        counterpart_files = boundary_info.get("counterpart_files", {})
        protocols = boundary_info.get("protocols", [])

        pairs_lines = []
        for bf in boundary_files:
            cps = counterpart_files.get(bf, [])
            pairs_lines.append(f"本端变更文件：`{bf}`")
            for cp in cps:
                pairs_lines.append(f"  → 对端文件：`{cp}`（必须精读并比对）")

        boundary_section = BOUNDARY_SECTION_TEMPLATE.format(
            protocols=", ".join(protocols) if protocols else "未知",
            boundary_file_pairs="\n".join(pairs_lines),
        )
    else:
        boundary_section = ""

    # --- P0 场景段落 ---
    if p0_context:
        # 只注入与当前模块相关的 P0 场景
        module_p0 = [
            p for p in p0_context
            if not p["scope"] or module.name.lower() in p["scope"].lower()
               or module.src_dir in p["scope"]
        ]
        if module_p0:
            p0_lines = []
            for p in module_p0:
                p0_lines.append(
                    f"- **{p['case_id']}**：关键词 `{p['keyword']}`"
                    + (f"（范围：{p['scope']}）" if p['scope'] else "")
                )
            p0_section = P0_SECTION_TEMPLATE.format(
                p0_cases_text="\n".join(p0_lines),
            )
        else:
            p0_section = ""
    else:
        p0_section = ""

    return SCAN_PROMPT.format(
        module_name=module.name,
        language=module.language,
        src_dir=module.src_dir,
        runtime_constraints=runtime_facts,
        high_freq_hints=high_freq,
        changed_files_section=changed_files_section,
        boundary_section=boundary_section,
        p0_section=p0_section,
    )


def run_deep_r2(state, project_root, modules, r1_findings):
    """R2 深度扫描（/deep 专用）"""
    from lib.claude import call_claude_bare
    from lib.prompts.scan import DEEP_R2_PROMPT, BOUNDARY_SECTION_TEMPLATE
    from lib.schemas.findings import FINDINGS_SCHEMA
    from lib.filters import filter_findings

    changed_by_module = getattr(state, "changed_by_module", {})
    boundary_context = getattr(state, "boundary_context", {})

    r2_findings = []
    r1_summary = _summarize_findings(r1_findings)

    with ThreadPoolExecutor(max_workers=max(1, len(modules))) as pool:
        futures = {}
        for m in modules:
            # 变更文件列表
            module_files = changed_by_module.get(m.name, [])
            if module_files:
                changed_files_section = "\n".join(f"- {f}" for f in module_files)
            else:
                changed_files_section = f"请扫描 {m.src_dir} 目录。"

            # 边界段落（R2 同样需要）
            boundary_info = boundary_context.get(m.name)
            if boundary_info:
                boundary_files = boundary_info.get("boundary_files", [])
                counterpart_files = boundary_info.get("counterpart_files", {})
                protocols = boundary_info.get("protocols", [])
                pairs_lines = []
                for bf in boundary_files:
                    cps = counterpart_files.get(bf, [])
                    pairs_lines.append(f"本端变更文件：`{bf}`")
                    for cp in cps:
                        pairs_lines.append(f"  → 对端文件：`{cp}`")
                boundary_section = BOUNDARY_SECTION_TEMPLATE.format(
                    protocols=", ".join(protocols) if protocols else "未知",
                    boundary_file_pairs="\n".join(pairs_lines),
                )
            else:
                boundary_section = ""

            prompt = DEEP_R2_PROMPT.format(
                module_name=m.name,
                language=m.language,
                r1_summary=r1_summary,
                changed_files_section=changed_files_section,
                boundary_section=boundary_section,
            )
            timeout = m.estimate_timeout(project_root, task="scan")
            future = pool.submit(
                call_claude_bare,
                prompt=prompt,
                model="opus",
                tools="Read,Glob,Grep",
                output_schema=FINDINGS_SCHEMA,
                max_turns=30,
                cwd=project_root,
                timeout=timeout,
            )
            futures[future] = m

        for future in as_completed(futures):
            m = futures[future]
            try:
                result = future.result()
                findings = _extract_findings(result, m.name)
                findings, filtered_out = filter_findings(findings, m.language)
                if filtered_out:
                    state.filtered_findings.extend(filtered_out)

                if len(findings) == 0:
                    logger.info(f"{m.name}: R2 无新发现，提前终止")
                else:
                    logger.info(f"{m.name}: R2 发现 {len(findings)} 个问题")

                r2_findings.extend(findings)
            except Exception as e:
                logger.error(f"{m.name} R2 扫描失败: {e}")

    # 全局 ID，续接 R1
    start = len(state.findings) + 1
    for i, f in enumerate(r2_findings, start):
        f["id"] = f"F{i}"

    state.r2_findings = r2_findings
    state.findings.extend(r2_findings)
    return r2_findings


def _extract_findings(result, module_name):
    """从 claude 返回中提取 findings"""
    if isinstance(result, dict):
        findings = result.get("findings", [])
    elif isinstance(result, str):
        try:
            data = json.loads(result)
            findings = data.get("findings", [])
        except json.JSONDecodeError:
            logger.warning(f"无法解析扫描结果: {result[:200]}")
            findings = []
    else:
        findings = []

    # 标注模块
    for f in findings:
        f["module"] = module_name

    return findings


def _summarize_findings(findings):
    """生成 R1 发现摘要（给 R2 参考）"""
    lines = []
    for f in findings:
        lines.append(f"- [{f['id']}] {f.get('severity','?')} {f.get('file','')}:{f.get('line','')} — {f.get('description','')}")
    return "\n".join(lines) if lines else "R1 未发现任何问题"
