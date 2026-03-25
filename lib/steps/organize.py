"""阶段 1.5：将 findings 按测试体系缺口归类"""

import logging, json

logger = logging.getLogger(__name__)

def run_organize(state, project_root):
    """调用 claude 将 findings 归类为 gaps"""
    from lib.claude import call_claude_bare
    from lib.prompts.organize import ORGANIZE_PROMPT
    from lib.schemas.gaps import GAPS_SCHEMA

    if not state.findings:
        logger.info("无 findings，跳过归类")
        state.gaps = []
        return []

    findings_json = json.dumps(state.findings, ensure_ascii=False, indent=2)

    result = call_claude_bare(
        prompt=ORGANIZE_PROMPT.format(findings_json=findings_json),
        model="opus",
        tools="",
        output_schema=GAPS_SCHEMA,
        max_turns=5,
    )

    if isinstance(result, dict):
        gaps = result.get("gaps", [])
    elif isinstance(result, str):
        try:
            gaps = json.loads(result).get("gaps", [])
        except json.JSONDecodeError:
            logger.warning(f"无法解析归类结果")
            gaps = []
    else:
        gaps = []

    state.gaps = gaps
    logger.info(f"归类为 {len(gaps)} 个盲区")
    return gaps
