"""阶段 C-1：gate 规则 + helper + preflight"""

import logging
import subprocess
import json

logger = logging.getLogger(__name__)


def run_infra_c1(state, project_root):
    """Phase C-1：
    1. claude 写 gate 规则和 helper（模式 B，无 Bash）
    2. CLI 跑 preflight（恰好 1 次，最多重试 1 次）
    3. 跨模块约束提取
    """
    from lib.claude import call_claude_session, call_claude_bare
    from lib.prompts.infra import PHASE_C1_PROMPT, CONSTRAINTS_PROMPT
    from lib.schemas.verify import CONSTRAINTS_SCHEMA
    from lib.config import load_config

    verified = [
        f for f in state.findings
        if state.get_result_status(f["id"]) == "verified"
    ]

    if not verified:
        logger.info("无已验证 bug，跳过 C-1")
        state.phase_c1_done = True
        return

    config = load_config(project_root)

    # 1. claude 写 gate 规则（模式 B，无 Bash）
    call_claude_session(
        prompt=PHASE_C1_PROMPT.format(
            verified_bugs_json=json.dumps(verified, ensure_ascii=False, indent=2),
            config_json=json.dumps(config, ensure_ascii=False, indent=2),
        ),
        model="opus",
        tools="Read,Glob,Grep,Edit,Write",
        max_turns=30,
        cwd=project_root,
    )

    # 2. CLI 跑 preflight
    preflight_ok = _run_preflight(project_root)

    if not preflight_ok:
        # 让 claude 修一次
        logger.info("preflight 失败，尝试修复")
        call_claude_session(
            prompt="preflight 检查失败，请读取错误输出并修复 gate 规则。不要改业务代码。",
            model="opus",
            tools="Read,Glob,Grep,Edit,Write",
            max_turns=15,
            cwd=project_root,
        )
        preflight_ok = _run_preflight(project_root)
        if not preflight_ok:
            logger.warning("preflight 第二次仍失败，继续")

    # 3. 跨模块约束
    try:
        constraints = call_claude_bare(
            prompt=CONSTRAINTS_PROMPT.format(
                verified_bugs_json=json.dumps(verified, ensure_ascii=False, indent=2),
            ),
            model="opus",
            tools="",
            output_schema=CONSTRAINTS_SCHEMA,
            max_turns=5,
        )
        if isinstance(constraints, dict):
            state.constraints = constraints.get("constraints", [])
    except Exception as e:
        logger.error(f"约束提取失败: {e}")

    # commit + push
    from lib.git import git_commit, git_push
    try:
        git_commit("Phase C-1: gate 规则 + helper", cwd=project_root)
        git_push(cwd=project_root)
    except Exception as e:
        logger.warning(f"C-1 提交/推送失败: {e}")

    state.phase_c1_done = True
    logger.info("Phase C-1 完成")


def _run_preflight(project_root):
    """跑 preflight 检查"""
    try:
        result = subprocess.run(
            ["bash", "scripts/test-governance-gate.sh", "preflight"],
            cwd=project_root, capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            logger.info("preflight 通过")
            return True
        else:
            logger.warning(f"preflight 失败:\n{result.stdout[-500:]}\n{result.stderr[-500:]}")
            return False
    except Exception as e:
        logger.error(f"preflight 执行异常: {e}")
        return False
