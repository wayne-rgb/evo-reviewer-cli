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
    # 兼容旧版 state JSON（可能缺少 filtered_findings 字段）
    if not hasattr(state, "filtered_findings") or state.filtered_findings is None:
        state.filtered_findings = []

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
    """R2 跨模块业务流扫描（/deep 专用）——单 session，全模块视野。

    从用户场景出发追踪数据跨模块流转，在模块交界处找不一致。
    单个 Claude session，cwd 为项目根目录，可 Read 所有模块文件。
    """
    from lib.claude import call_claude_bare
    from lib.prompts.scan import DEEP_R2_CROSS_MODULE_PROMPT
    from lib.schemas.findings import FINDINGS_SCHEMA
    from lib.filters import filter_findings
    from lib.steps.scope import extract_all_boundaries

    # 1. 提取全量边界（不依赖 git diff）
    boundaries = extract_all_boundaries(project_root)

    # 2. 构建模块信息
    modules_info = "\n".join(
        f"- **{m.name}**（{m.language}）: `{m.src_dir}`"
        for m in modules
    )

    # 3. 构建边界文件对文本
    boundary_pairs = _format_boundary_pairs(boundaries)

    # 4. R1 摘要
    r1_summary = _summarize_findings(r1_findings)

    # 5. 计算 timeout（所有模块之和 × 1.5，上限 30 分钟）
    total_timeout = sum(m.estimate_timeout(project_root, task="scan") for m in modules)
    timeout = min(int(total_timeout * 1.5), 1800)

    # 6. 单 session 调用
    prompt = DEEP_R2_CROSS_MODULE_PROMPT.format(
        topology_summary=boundaries.get("topology_summary", "未检测到模块间通信拓扑"),
        modules_info=modules_info,
        boundary_pairs=boundary_pairs if boundary_pairs else "未检测到边界文件对，请基于代码自行追踪模块间调用。",
        r1_summary=r1_summary,
    )

    try:
        result = call_claude_bare(
            prompt=prompt,
            model="opus",
            tools="Read,Glob,Grep",
            output_schema=FINDINGS_SCHEMA,
            max_turns=60,
            cwd=project_root,
            timeout=timeout,
        )
    except Exception as e:
        logger.error(f"R2 跨模块扫描失败: {e}")
        state.r2_findings = []
        return []

    # 7. 提取 findings — 根据 file 路径推断模块归属
    r2_findings = _extract_cross_module_findings(result, modules)

    # 8. 过滤（跨模块 finding 跳过语言级过滤，避免误判）
    kept_findings = []
    for f in r2_findings:
        f_modules = f.get("modules", [])
        if len(f_modules) > 1:
            # 跨模块 finding：涉及多种语言，跳过语言级过滤
            kept_findings.append(f)
        else:
            lang = _infer_language(f.get("file", ""), modules)
            kept, filtered_out = filter_findings([f], lang)
            kept_findings.extend(kept)
            if filtered_out:
                state.filtered_findings.extend(filtered_out)
    r2_findings = kept_findings

    # 9. 去重
    before_dedup = len(r2_findings)
    r2_findings = _dedup_findings(r1_findings, r2_findings)
    dedup_count = before_dedup - len(r2_findings)
    if dedup_count > 0:
        logger.info("R2 去重：移除了 %d 个与 R1 重复的 finding", dedup_count)

    if r2_findings:
        logger.info(f"R2 跨模块扫描发现 {len(r2_findings)} 个问题")
    else:
        logger.info("R2 跨模块扫描无新发现")

    # 10. 全局 ID，续接 R1
    start = len(state.findings) + 1
    for i, f in enumerate(r2_findings, start):
        f["id"] = f"F{i}"

    state.r2_findings = r2_findings
    state.findings.extend(r2_findings)
    return r2_findings


def _extract_cross_module_findings(result, modules):
    """从跨模块扫描的 Claude 返回中提取 findings。

    如果 Claude 填了 modules 数组，取第一个作为 module（向后兼容）。
    如果没填 module，根据 file 路径推断所属模块。
    """
    if isinstance(result, dict):
        findings = result.get("findings", [])
    elif isinstance(result, str):
        try:
            data = json.loads(result)
            findings = data.get("findings", [])
        except json.JSONDecodeError:
            logger.warning(f"无法解析 R2 扫描结果: {result[:200]}")
            findings = []
    else:
        findings = []

    modules_by_prefix = {}
    for m in modules:
        # src_dir 如 "togo-agent/src/" → 前缀 "togo-agent/"
        prefix = m.src_dir.rstrip("/").split("/")[0] + "/"
        modules_by_prefix[prefix] = m.name

    for f in findings:
        # 如果有 modules 数组，取第一个作为 module
        if f.get("modules") and not f.get("module"):
            f["module"] = f["modules"][0]

        # 如果没有 module，从 file 路径推断
        if not f.get("module"):
            file_path = f.get("file", "")
            for prefix, mod_name in modules_by_prefix.items():
                if file_path.startswith(prefix):
                    f["module"] = mod_name
                    break
            else:
                f["module"] = "unknown"

    return findings


def _format_boundary_pairs(boundaries):
    """格式化边界文件对为可读文本"""
    module_pairs = boundaries.get("module_pairs", [])
    if not module_pairs:
        return ""

    lines = []
    for mp in module_pairs:
        mods = " ↔ ".join(mp["modules"])
        protos = ", ".join(mp["protocols"]) if mp["protocols"] else "未知"
        lines.append(f"### {mods}（{protos}）")

        shared_files = mp.get("shared_files", {})
        for src_file, dst_files in shared_files.items():
            lines.append(f"- `{src_file}`")
            for df in dst_files:
                lines.append(f"  ↔ `{df}`")
        lines.append("")

    return "\n".join(lines)


def _infer_language(file_path, modules):
    """根据文件路径推断语言"""
    for m in modules:
        prefix = m.src_dir.rstrip("/").split("/")[0] + "/"
        if file_path.startswith(prefix):
            return m.language
    # fallback by extension
    if file_path.endswith(".ts") or file_path.endswith(".js"):
        return "typescript"
    if file_path.endswith(".swift"):
        return "swift"
    if file_path.endswith(".go"):
        return "go"
    return "unknown"


def _dedup_findings(existing, new_findings):
    """模糊去重：移除 new_findings 中与 existing 重复的条目。

    重复判定：同文件 + 行号差 ≤ 5 + 描述词集重叠率 > 0.6
    """
    kept = []
    for nf in new_findings:
        nf_file = nf.get("file", "")
        nf_line = nf.get("line", 0)
        nf_words = set(nf.get("description", "").lower().split())

        is_dup = False
        for ef in existing:
            if ef.get("file", "") != nf_file:
                continue
            if abs(ef.get("line", 0) - nf_line) > 5:
                continue
            ef_words = set(ef.get("description", "").lower().split())
            if not nf_words or not ef_words:
                continue
            overlap = len(nf_words & ef_words) / max(len(nf_words | ef_words), 1)
            if overlap > 0.6:
                is_dup = True
                logger.debug(
                    "去重: R2 %s:%d 与 R1 %s:%d 重复（overlap=%.2f）",
                    nf_file, nf_line, ef.get("file"), ef.get("line"), overlap,
                )
                break
        if not is_dup:
            kept.append(nf)
    return kept


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
