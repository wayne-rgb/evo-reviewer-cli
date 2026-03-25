"""R5：轻量化交叉检验（/deep 专用）"""

import logging
import json
import time

logger = logging.getLogger(__name__)

# R5 预算
R5_MAX_MINUTES = 20
R5_MAX_TURNS = 20
R5_MAX_FIXES = 3


def run_cross_validate(state, project_root, modules_by_name):
    """R5 交叉检验：不开 worktree，直接在 main 上检查。

    扫描修复是否引入新问题、测试是否有漏洞、类似模式是否存在。
    """
    from lib.claude import call_claude_bare
    from lib.prompts.infra import CROSS_SCAN_PROMPT
    from lib.schemas.findings import FINDINGS_SCHEMA
    from lib.filters import is_impossible

    start_time = time.time()

    # 已验证的 bug 摘要
    verified = [
        f for f in state.findings
        if state.get_result_status(f["id"]) == "verified"
    ]
    verified_summary = "\n".join(
        f"- [{f['id']}] {f.get('file')}:{f.get('line')} — {f.get('description', '')}"
        for f in verified
    ) or "无已验证修复"

    # 新增测试模式
    test_patterns = "\n".join(
        f"- {state.get_result_field(f['id'], 'test_file', '?')}"
        for f in verified
        if state.get_result_field(f["id"], "test_file")
    ) or "无新增测试"

    prompt = CROSS_SCAN_PROMPT.format(
        verified_summary=verified_summary,
        test_patterns=test_patterns,
    )

    try:
        result = call_claude_bare(
            prompt=prompt,
            model="opus",
            tools="Read,Glob,Grep",
            output_schema=FINDINGS_SCHEMA,
            max_turns=R5_MAX_TURNS,
            cwd=project_root,
        )
    except Exception as e:
        logger.error(f"R5 扫描失败: {e}")
        return []

    # 提取 findings
    if isinstance(result, dict):
        findings = result.get("findings", [])
    elif isinstance(result, str):
        try:
            findings = json.loads(result).get("findings", [])
        except json.JSONDecodeError:
            findings = []
    else:
        findings = []

    if not findings:
        logger.info("R5 无新发现")
        return []

    # 按每个 finding 所属模块的语言过滤不可能的 bug
    findings = [
        f for f in findings
        if f.get("module") not in modules_by_name
        or not is_impossible(f, modules_by_name[f["module"]].language)
    ]

    logger.info(f"R5 发现 {len(findings)} 个问题")

    # 预算检查
    elapsed = (time.time() - start_time) / 60
    if elapsed > R5_MAX_MINUTES:
        logger.warning(f"R5 已超时 ({elapsed:.1f}min > {R5_MAX_MINUTES}min)")
        state.r5_findings = findings
        return findings

    # R5 用轻量 worktree 保护 main 分支（方案说不开 worktree，但直接在 main 上改太危险）
    from lib.steps.verify import _verify_single_bug
    from lib.worktree import create_worktree, remove_worktree, commit_in_worktree, merge_worktree

    try:
        r5_wt = create_worktree("r5-cross", project_root)
    except Exception as e:
        logger.error(f"R5 创建 worktree 失败: {e}")
        state.r5_findings = findings
        return findings

    try:
        bugs_to_verify = findings
        if len(findings) > R5_MAX_FIXES:
            # 多发现：只取 top 3 HIGH
            high_bugs = [f for f in findings if f.get("severity") == "HIGH"][:R5_MAX_FIXES]
            overflow = [f for f in findings if f not in high_bugs]
            state.overflow.extend(f["id"] for f in overflow)
            bugs_to_verify = high_bugs

        for bug in bugs_to_verify:
            if (time.time() - start_time) / 60 > R5_MAX_MINUTES:
                logger.warning("R5 预算耗尽")
                break
            module = modules_by_name.get(bug.get("module"))
            if module:
                result = _verify_single_bug(bug, r5_wt, module, project_root)
                state.results[bug["id"]] = result

        # 有 verified 的修复就合并
        has_verified = any(
            state.get_result_status(f["id"]) == "verified"
            for f in bugs_to_verify
        )
        if has_verified:
            commit_in_worktree(r5_wt, "evo-review R5: 交叉检验修复")
            merge_worktree(r5_wt, project_root)
        else:
            remove_worktree(r5_wt.path, project_root)
    except Exception as e:
        logger.error(f"R5 验证异常: {e}")
        remove_worktree(r5_wt.path, project_root)

    state.r5_findings = findings
    return findings
