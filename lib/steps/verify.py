"""阶段 A：红绿验证 — 每个 bug 独立验证，支持并行"""

import logging
import subprocess
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# 每模块最多验证 10 个 bug
MAX_BUGS_PER_MODULE = 10
# 并行 worker 数
MAX_WORKERS = 3
# 保护 state.results 并发写入
_results_lock = threading.Lock()


def run_verify(state, project_root, confirmed_ids, modules_by_name):
    """对所有已确认的 bug 执行红绿验证。

    按模块分组，每个模块创建 worktree，模块间并行。
    同模块内，不同文件的 bug 并行，同文件的 bug 串行。

    Args:
        confirmed_ids: 确认的 finding ID 列表
        modules_by_name: {name: ModuleConfig} 映射
    """
    from lib.worktree import plan_worktrees, commit_in_worktree

    # 按模块分组
    bugs_by_module = {}
    for f in state.findings:
        if f["id"] in confirmed_ids:
            mod = f.get("module", "unknown")
            bugs_by_module.setdefault(mod, []).append(f)

    # 创建 worktrees
    active_modules = [modules_by_name[name] for name in bugs_by_module if name in modules_by_name]
    worktrees = plan_worktrees(active_modules, project_root)
    state.worktrees = {name: {"path": wt.path, "branch": wt.branch} for name, wt in worktrees.items()}

    if not bugs_by_module:
        logger.info("无 bug 需要验证")
        state.advance("verify")
        return state.results

    # 按模块并行验证
    with ThreadPoolExecutor(max_workers=max(1, len(bugs_by_module))) as pool:
        futures = {}
        for mod_name, bugs in bugs_by_module.items():
            if mod_name not in worktrees:
                logger.warning(f"模块 {mod_name} 无 worktree，跳过")
                continue
            wt = worktrees[mod_name]
            module = modules_by_name.get(mod_name)
            if not module:
                continue

            future = pool.submit(
                _verify_module, state, project_root, mod_name, bugs, wt, module
            )
            futures[future] = mod_name

        for future in as_completed(futures):
            mod_name = futures[future]
            try:
                future.result()
                logger.info(f"模块 {mod_name} 验证完成")
            except Exception as e:
                logger.error(f"模块 {mod_name} 验证失败: {e}")

    state.advance("verify")
    return state.results


def _verify_module(state, project_root, mod_name, bugs, wt, module):
    """验证单个模块的所有 bug"""
    from lib.worktree import commit_in_worktree

    # 效率约束：最多 10 个
    active_bugs = bugs[:MAX_BUGS_PER_MODULE]
    overflow = bugs[MAX_BUGS_PER_MODULE:]

    for bug in overflow:
        with _results_lock:
            state.results[bug["id"]] = {"status": "skipped", "reason": "超出模块上限"}
            state.overflow.append(bug["id"])

    # 按文件分组：同文件串行，不同文件并行
    by_file = {}
    for bug in active_bugs:
        by_file.setdefault(bug.get("file", ""), []).append(bug)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for file_path, file_bugs in by_file.items():
            # 同文件的 bug 串行处理
            future = pool.submit(
                _verify_file_bugs, file_bugs, wt, module, project_root
            )
            futures[future] = file_path

        for future in as_completed(futures):
            file_path = futures[future]
            try:
                results = future.result()
                with _results_lock:
                    for bug_id, result in results.items():
                        state.results[bug_id] = result
            except Exception as e:
                logger.error(f"文件 {file_path} 验证失败: {e}")
                with _results_lock:
                    for bug in by_file[file_path]:
                        state.results[bug["id"]] = {"status": "unverified", "reason": str(e)}

    # 模块验证完成后：CLI 跑一次 lint + 已有测试
    _run_module_checks(wt, module, project_root)

    # commit worktree（只含 verified 和 unverified 的改动）
    has_verified = any(
        state.get_result_status(b["id"]) == "verified"
        for b in active_bugs
    )
    if has_verified:
        commit_in_worktree(wt, f"evo-review: {mod_name} 红绿验证修复")


def _verify_file_bugs(bugs, wt, module, project_root):
    """串行验证同一文件的多个 bug"""
    results = {}
    for bug in bugs:
        result = _verify_single_bug(bug, wt, module, project_root)
        results[bug["id"]] = result
    return results


def _verify_single_bug(bug, wt, module, project_root):
    """单个 bug 的红绿验证。独立 claude 调用。

    流程：
    1. 写测试（模式 B，无 Bash）
    2. CLI 跑测试
    3. 测试通过 -> 幻觉，回滚
    4. 测试失败 -> 检查失败原因是否相关
    5. 不相关 -> 幻觉，回滚
    6. 相关 -> 写修复
    7. CLI 跑测试
    8. 通过 -> verified
    9. 失败 -> 重试一次
    10. 仍失败 -> fix_failed，回滚
    """
    from lib.claude import call_claude_session, call_claude_bare
    from lib.prompts.verify import (
        WRITE_TEST_PROMPT, WRITE_FIX_PROMPT,
        RETRY_FIX_PROMPT, CHECK_REASON_PROMPT,
    )
    from lib.schemas.verify import CHECK_REASON_SCHEMA

    bug_id = bug["id"]
    verify_timeout = module.estimate_timeout(project_root, task="verify")
    logger.info(f"[{bug_id}] 开始验证: {bug.get('file')}:{bug.get('line')} — {bug.get('description', '')[:60]}")

    # 1. 写测试
    test_prompt = WRITE_TEST_PROMPT.format(
        bug_id=bug_id,
        bug_file=bug.get("file", ""),
        bug_line=bug.get("line", 0),
        bug_description=bug.get("description", ""),
        bug_severity=bug.get("severity", "MEDIUM"),
        test_strategy=bug.get("test_strategy", "behavior"),
    )

    try:
        call_claude_session(
            prompt=test_prompt,
            model="opus",
            tools="Read,Glob,Grep,Edit,Write",
            max_turns=15,
            cwd=wt.path,
            timeout=verify_timeout,
        )
    except Exception as e:
        logger.error(f"[{bug_id}] 写测试失败: {e}")
        return {"status": "unverified", "reason": f"写测试失败: {e}"}

    # 2. CLI 跑测试
    test_result = _run_test(wt.path, bug, module)

    # 3. 测试通过 -> 幻觉
    if test_result["exit_code"] == 0:
        _revert_changes(wt.path, bug)
        logger.info(f"[{bug_id}] 幻觉 — 测试直接通过")
        return {"status": "hallucination", "reason": "测试直接通过，bug 不存在"}

    # 4. 检查失败原因
    output_tail = _tail(test_result["output"], 50)
    try:
        reason = call_claude_bare(
            prompt=CHECK_REASON_PROMPT.format(
                bug_id=bug_id,
                bug_description=bug.get("description", ""),
                test_output=output_tail,
            ),
            model="opus",
            tools="",
            output_schema=CHECK_REASON_SCHEMA,
            max_turns=3,
        )
    except Exception:
        reason = {"related": True, "reason": "无法判断，假设相关"}

    if isinstance(reason, str):
        try:
            reason = json.loads(reason)
        except json.JSONDecodeError:
            reason = {"related": True, "reason": reason}

    if not reason.get("related", True):
        _revert_changes(wt.path, bug)
        logger.info(f"[{bug_id}] 幻觉 — {reason.get('reason', '')}")
        return {"status": "hallucination", "reason": reason.get("reason", "测试失败与 bug 无关")}

    # 5. 红灯确认，写修复
    logger.info(f"[{bug_id}] 红灯确认，开始写修复")
    fix_prompt = WRITE_FIX_PROMPT.format(
        bug_id=bug_id,
        bug_file=bug.get("file", ""),
        bug_line=bug.get("line", 0),
        bug_description=bug.get("description", ""),
    )

    try:
        call_claude_session(
            prompt=fix_prompt,
            model="opus",
            tools="Read,Glob,Grep,Edit,Write",
            max_turns=15,
            cwd=wt.path,
            timeout=verify_timeout,
        )
    except Exception as e:
        logger.error(f"[{bug_id}] 写修复失败: {e}")
        _revert_changes(wt.path, bug)
        return {"status": "fix_failed", "reason": f"写修复失败: {e}"}

    # 6. CLI 跑测试
    fix_result = _run_test(wt.path, bug, module)

    if fix_result["exit_code"] == 0:
        logger.info(f"[{bug_id}] 验证通过")
        return {"status": "verified", "test_file": _guess_test_file(bug, module)}

    # 7. 重试一次
    logger.info(f"[{bug_id}] 修复后测试仍失败，重试")
    retry_prompt = RETRY_FIX_PROMPT.format(
        bug_id=bug_id,
        bug_file=bug.get("file", ""),
        bug_line=bug.get("line", 0),
        error_output=_tail(fix_result["output"], 30),
    )

    try:
        call_claude_session(
            prompt=retry_prompt,
            model="opus",
            tools="Read,Glob,Grep,Edit,Write",
            max_turns=15,
            cwd=wt.path,
            timeout=verify_timeout,
        )
    except Exception as e:
        _revert_changes(wt.path, bug)
        return {"status": "fix_failed", "reason": f"重试失败: {e}"}

    retry_result = _run_test(wt.path, bug, module)
    if retry_result["exit_code"] == 0:
        logger.info(f"[{bug_id}] 重试后验证通过")
        return {"status": "verified", "test_file": _guess_test_file(bug, module)}

    _revert_changes(wt.path, bug)
    logger.info(f"[{bug_id}] 修复失败")
    return {"status": "fix_failed", "reason": "重试后测试仍失败"}


def _run_test(wt_path, bug, module):
    """在 worktree 中跑测试。

    worktree 是完整仓库的副本，测试路径需要包含模块前缀。
    例如 bug 文件 togo-agent/src/ws/server.ts → 测试 togo-agent/src/__tests__/server.test.ts
    """
    test_file = _guess_test_file(bug, module)

    if module.language == "typescript":
        # 在模块目录下执行 vitest
        module_dir = os.path.join(wt_path, module.src_dir.rstrip("/").rsplit("/", 1)[0])
        # test_file 是相对于模块根目录的路径，引号保护防止空格/特殊字符
        cmd = f'cd "{module_dir}" && npx vitest run "{test_file}"'
    elif module.language == "go":
        pkg = os.path.dirname(bug.get("file", ""))
        cmd = f'cd "{wt_path}" && go test -race ./{pkg}/'
    elif module.language == "swift":
        cmd = _build_module_cmd(module.unit_command, wt_path, module) if module.unit_command else None
    else:
        cmd = _build_module_cmd(module.unit_command, wt_path, module) if module.unit_command else None

    if not cmd:
        return {"exit_code": -1, "output": "无可用测试命令"}

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=120
        )
        return {
            "exit_code": result.returncode,
            "output": result.stdout + result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "output": "测试超时（120s）"}
    except Exception as e:
        return {"exit_code": -1, "output": str(e)}


def _run_module_checks(wt, module, project_root):
    """模块级 lint + 已有测试"""
    from lib.claude import call_claude_session
    from lib.prompts.verify import FIX_REGRESSION_PROMPT

    checks_ok = True

    if module.lint_command:
        cmd = _build_module_cmd(module.lint_command, wt.path, module)
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=120,
        )
        if result.returncode != 0:
            checks_ok = False
            logger.warning(f"Lint 失败: {_tail(result.stdout + result.stderr, 20)}")

    if module.unit_command:
        cmd = _build_module_cmd(module.unit_command, wt.path, module)
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=300,
        )
        if result.returncode != 0:
            checks_ok = False
            logger.warning("单元测试失败")

    if not checks_ok:
        error_output = "lint/test 回归，请修复"
        try:
            call_claude_session(
                prompt=FIX_REGRESSION_PROMPT.format(error_output=error_output),
                model="opus",
                tools="Read,Glob,Grep,Edit,Write",
                max_turns=15,
                cwd=wt.path,
            )
        except Exception as e:
            logger.error(f"修复回归失败: {e}")


def _revert_changes(wt_path, bug=None):
    """回滚 worktree 中的改动。

    如果有 bug 信息，只回滚该 bug 相关的文件（精确回滚）；
    否则回滚所有未提交改动（全量回滚）。

    精确回滚避免并发验证时互相干扰——不同文件的 bug 并行执行，
    一个 bug 失败不应影响其他 bug 的改动。
    """
    if bug:
        # 精确回滚：只回滚 bug 相关的文件
        # 获取当前未提交的改动文件
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=wt_path, capture_output=True, text=True,
        )
        changed = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()

        # 获取新增的未跟踪文件
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=wt_path, capture_output=True, text=True,
        )
        untracked = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()

        # 推断 bug 相关的文件模式
        bug_file = bug.get("file", "")
        bug_base = os.path.splitext(os.path.basename(bug_file))[0]

        files_to_revert = []
        files_to_remove = []

        for f in changed:
            if bug_base and bug_base in f:
                files_to_revert.append(f)
        for f in untracked:
            if bug_base and bug_base in f:
                files_to_remove.append(f)

        if files_to_revert:
            subprocess.run(
                ["git", "checkout", "--"] + files_to_revert,
                cwd=wt_path, capture_output=True, text=True,
            )
        for f in files_to_remove:
            full = os.path.join(wt_path, f)
            if os.path.exists(full):
                os.remove(full)

        if not files_to_revert and not files_to_remove:
            # 找不到特定文件就全量回滚（兜底）
            _revert_all(wt_path)
    else:
        _revert_all(wt_path)


def _revert_all(wt_path):
    """全量回滚——回滚所有未提交改动和未跟踪文件"""
    subprocess.run(
        ["git", "checkout", "--", "."], cwd=wt_path,
        capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "clean", "-fd"], cwd=wt_path,
        capture_output=True, text=True,
    )


def _guess_test_file(bug, module=None):
    """根据 bug 文件猜测对应的测试文件。

    返回相对于模块根目录的路径（不含模块名前缀）。
    例如 bug 文件 togo-agent/src/ws/server.ts → src/__tests__/server.test.ts
    """
    file_path = bug.get("file", "")

    # 去掉模块前缀（如 togo-agent/）
    if module and module.src_dir:
        module_prefix = module.src_dir.rstrip("/").rsplit("/", 1)[0] + "/"
        if file_path.startswith(module_prefix):
            file_path = file_path[len(module_prefix):]

    # TypeScript: src/foo/bar.ts -> src/__tests__/bar.test.ts
    if file_path.endswith(".ts"):
        base = os.path.basename(file_path).replace(".ts", ".test.ts")
        return f"src/__tests__/{base}"
    # Go: pkg/foo/bar.go -> pkg/foo/bar_test.go
    if file_path.endswith(".go"):
        return file_path.replace(".go", "_test.go")
    return file_path


def _build_module_cmd(cmd_template, wt_path, module):
    """将 config.yaml 中的命令模板适配到 worktree 路径。

    处理 'cd module-name && ...' 格式的命令，将 cd 目标替换为 worktree 中的模块目录。
    """
    import re
    # 匹配 'cd xxx &&' 或 'cd xxx;' 前缀
    match = re.match(r'^cd\s+(\S+)\s*(&&|;)\s*(.+)$', cmd_template)
    if match:
        original_dir = match.group(1)
        separator = match.group(2)
        rest = match.group(3)
        # 在 worktree 中，模块目录的位置不变，引号保护路径
        new_dir = os.path.join(wt_path, original_dir)
        return f'cd "{new_dir}" {separator} {rest}'

    # 没有 cd 前缀，直接在 worktree 中执行
    module_dir = os.path.join(wt_path, module.src_dir.rstrip("/").rsplit("/", 1)[0])
    return f'cd "{module_dir}" && {cmd_template}'


def _tail(data, n=50):
    """取最后 n 行"""
    if isinstance(data, dict):
        text = data.get("output", "")
    else:
        text = str(data)
    lines = text.strip().split('\n')
    return '\n'.join(lines[-n:])
