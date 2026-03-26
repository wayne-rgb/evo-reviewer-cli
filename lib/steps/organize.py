"""阶段 1.5：将 findings 按测试体系缺口归类"""

import logging, json

logger = logging.getLogger(__name__)

# 单批最多 15 个 findings，防止 prompt 过大导致 opus 推理超时
_BATCH_SIZE = 15


def run_organize(state, project_root):
    """调用 claude 将 findings 归类为 gaps。

    超过 _BATCH_SIZE 个 findings 时自动分批，跨批同名 gap 合并。
    """
    from lib.claude import call_claude_bare
    from lib.prompts.organize import ORGANIZE_PROMPT
    from lib.schemas.gaps import GAPS_SCHEMA

    if not state.findings:
        logger.info("无 findings，跳过归类")
        state.gaps = []
        return []

    findings = state.findings

    if len(findings) <= _BATCH_SIZE:
        gaps = _call_organize(findings)
    else:
        all_gaps = []
        for i in range(0, len(findings), _BATCH_SIZE):
            batch = findings[i:i + _BATCH_SIZE]
            batch_num = i // _BATCH_SIZE + 1
            total_batches = (len(findings) + _BATCH_SIZE - 1) // _BATCH_SIZE
            logger.info("organize 分批 %d/%d（%d 个 findings）", batch_num, total_batches, len(batch))
            batch_gaps = _call_organize(batch)
            all_gaps.extend(batch_gaps)

        # 跨批合并同名 gap
        gaps = _merge_gaps(all_gaps)
        logger.info("分批归类后合并：%d → %d 个盲区", len(all_gaps), len(gaps))

    state.gaps = gaps
    logger.info(f"归类为 {len(gaps)} 个盲区")
    return gaps


def _call_organize(findings):
    """对一批 findings 调用 claude 归类"""
    from lib.claude import call_claude_bare
    from lib.prompts.organize import ORGANIZE_PROMPT
    from lib.schemas.gaps import GAPS_SCHEMA

    findings_json = json.dumps(findings, ensure_ascii=False, indent=2)

    # 动态超时：基础 120s + 每个 finding 10s，下限 300s 上限 900s
    # 宁可多等，超时失败 = 100% token 浪费
    timeout = min(900, max(300, 120 + len(findings) * 10))
    logger.info("organize 超时: %ds（%d 个 findings）", timeout, len(findings))

    result = call_claude_bare(
        prompt=ORGANIZE_PROMPT.format(findings_json=findings_json),
        model="opus",
        tools="",
        output_schema=GAPS_SCHEMA,
        max_turns=5,
        timeout=timeout,
    )

    if isinstance(result, dict):
        return result.get("gaps", [])
    elif isinstance(result, str):
        try:
            return json.loads(result).get("gaps", [])
        except json.JSONDecodeError:
            logger.warning("无法解析归类结果")
            return []
    return []


def _merge_gaps(gaps):
    """合并同名 gap：相同 gap_name 的 evidence_finding_ids 合并"""
    merged = {}
    for g in gaps:
        key = (g.get("module", ""), g.get("gap_name", ""))
        if key in merged:
            existing = merged[key]
            existing_ids = set(existing.get("evidence_finding_ids", []))
            new_ids = set(g.get("evidence_finding_ids", []))
            existing["evidence_finding_ids"] = sorted(existing_ids | new_ids)
        else:
            merged[key] = dict(g)

    result = []
    for i, g in enumerate(merged.values(), 1):
        g["id"] = f"G{i}"
        result.append(g)
    return result
