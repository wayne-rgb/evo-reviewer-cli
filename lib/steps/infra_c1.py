"""阶段 C-1:gate 规则 + helper + preflight

v2.5 之前:一次性给 opus 大 prompt(N 个 finding × 4 个任务),实战 300s 超时,
state 半截、worktree 残留。

本版本(分子化):每个 verified finding 独立调用 opus,失败一个跳过继续,
最后统一跑 preflight。即使部分 finding 失败,gate 文件状态仍可读、可提交。
"""

import logging
import json
import subprocess
import time

logger = logging.getLogger(__name__)

# 每个 finding 的预算 — 比旧版的 300s default 短得多
PER_FINDING_TIMEOUT_SEC = 180
PER_FINDING_MAX_TURNS = 10
# preflight 修复的预算 — 单次轻量
PREFLIGHT_FIX_TIMEOUT_SEC = 120
PREFLIGHT_FIX_MAX_TURNS = 8


def run_infra_c1(state, project_root):
    """Phase C-1 分子化:

    1. 每个 verified bug 独立调用 opus 生成 gate 规则(失败跳过)
    2. 跑 preflight,失败时再调一次轻量修复
    3. 跨模块约束提取(轻量,保留不变)

    任何 finding 失败不阻塞其他,任何阶段失败不阻塞 commit。
    """
    from lib.claude import call_claude_session, call_claude_bare
    from lib.prompts.infra import (
        PHASE_C1_SINGLE_PROMPT,
        PHASE_C1_PREFLIGHT_FIX_PROMPT,
        CONSTRAINTS_PROMPT,
    )
    from lib.schemas.verify import CONSTRAINTS_SCHEMA
    from lib.config import load_config

    verified = [
        f for f in state.findings
        if state.get_result_status(f["id"]) == "verified"
    ]

    if not verified:
        logger.info("无已验证 bug,跳过 C-1")
        state.phase_c1_done = True
        return

    config = load_config(project_root)
    config_json = json.dumps(config, ensure_ascii=False, indent=2)

    # ============================================================
    # 1. 分子化:每个 verified finding 独立生成 gate 规则
    # ============================================================
    print(f"\nC-1 分子阶段:对 {len(verified)} 个 verified finding 逐个生成 gate 规则")
    successes = []
    failures = []

    for i, bug in enumerate(verified, 1):
        bug_id = bug["id"]
        bug_json = json.dumps(bug, ensure_ascii=False, indent=2)
        t0 = time.time()
        print(f"  [{i}/{len(verified)}] {bug_id} {bug.get('file','?')}:{bug.get('line','?')}", flush=True)

        try:
            call_claude_session(
                prompt=PHASE_C1_SINGLE_PROMPT.format(
                    bug_json=bug_json,
                    config_json=config_json,
                ),
                model="opus",
                tools="Read,Glob,Grep,Edit,Write",
                max_turns=PER_FINDING_MAX_TURNS,
                cwd=project_root,
                timeout=PER_FINDING_TIMEOUT_SEC,
            )
            elapsed = time.time() - t0
            successes.append(bug_id)
            logger.info(f"C-1 [{bug_id}] gate 规则生成完成({elapsed:.0f}s)")
            print(f"    ✓ ({elapsed:.0f}s)")
        except Exception as e:
            elapsed = time.time() - t0
            err_msg = str(e)[:150]
            failures.append((bug_id, err_msg))
            logger.warning(f"C-1 [{bug_id}] 失败({elapsed:.0f}s),跳过: {err_msg}")
            print(f"    ✗ ({elapsed:.0f}s) — 跳过,继续下一个")

    print(f"\n  小结:{len(successes)} 成功 / {len(failures)} 失败")
    if failures:
        for fid, err in failures:
            print(f"    ⚠️  [{fid}] {err[:80]}")

    # 记录 C-1 失败列表到 state(供 finalize 报告用)
    state.c1_failures = [{"bug_id": fid, "error": err} for fid, err in failures]

    # ============================================================
    # 2. preflight 验证 + 一次轻量修复
    # ============================================================
    preflight_ok = _run_preflight(project_root)

    if not preflight_ok:
        logger.info("preflight 失败,尝试一次轻量修复(180s 预算上限)")
        try:
            call_claude_session(
                prompt=PHASE_C1_PREFLIGHT_FIX_PROMPT,
                model="opus",
                tools="Read,Glob,Grep,Edit,Write",
                max_turns=PREFLIGHT_FIX_MAX_TURNS,
                cwd=project_root,
                timeout=PREFLIGHT_FIX_TIMEOUT_SEC,
            )
            preflight_ok = _run_preflight(project_root)
        except Exception as e:
            logger.error(f"preflight 修复调用失败: {e}")
        if not preflight_ok:
            logger.warning("preflight 第二次仍失败,记录到 state 后继续(不阻塞 commit)")

    state.c1_preflight_ok = preflight_ok

    # ============================================================
    # 3. 跨模块约束提取(已是轻量调用,保留)
    # ============================================================
    try:
        constraints = call_claude_bare(
            prompt=CONSTRAINTS_PROMPT.format(
                verified_bugs_json=json.dumps(verified, ensure_ascii=False, indent=2),
            ),
            model="opus",
            tools="",
            output_schema=CONSTRAINTS_SCHEMA,
            max_turns=5,
            timeout=120,
        )
        if isinstance(constraints, dict):
            state.constraints = constraints.get("constraints", [])
    except Exception as e:
        logger.error(f"约束提取失败,跳过: {e}")

    # ============================================================
    # 4. commit(即使部分 finding 失败,已成功的产出也提交)
    # ============================================================
    from lib.git import git_commit
    try:
        msg = "Phase C-1: gate 规则 + helper"
        if failures:
            msg += f"(分子化:{len(successes)}/{len(verified)} 成功)"
        git_commit(msg, cwd=project_root)
    except Exception as e:
        logger.warning(f"C-1 提交失败: {e}")

    state.phase_c1_done = True
    logger.info(f"Phase C-1 完成({len(successes)} 成功,{len(failures)} 失败,preflight={'✓' if preflight_ok else '✗'})")


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
