"""CI 验证 — 测试 + 可选的 auto_fix 修复循环

默认行为（auto_fix=False）：跑测试，报结果，不修。
auto_fix=True 时：测试失败 → worktree 内 AI 修复 → 回归 → 循环 3 轮。
修不好的写入 pending_file，worktree 丢弃，主分支不受影响。
"""

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# auto_fix 修复循环的最大重试轮数
MAX_FIX_ROUNDS = 3
# 同一失败连续修不好的容忍次数（达到后写入 pending 并跳过）
MAX_CONSECUTIVE_FAIL = 2


def run_ci(project_root, auto_fix=False, pending_file=None, diff_base=None):
    """根据改动范围运行检查，可选自动修复。

    Args:
        project_root: 项目根目录
        auto_fix: 是否开启修复循环（测试失败 → AI 修复 → 回归）
        pending_file: 待决问题输出文件路径（auto_fix=True 时使用）
        diff_base: diff 基准（默认 HEAD~1），用于确定改动范围

    Returns:
        auto_fix=False 时：bool（全部通过 / 有失败）
        auto_fix=True 时：(bool, list[dict])（通过与否, pending 问题列表）
    """
    from lib.git import git_diff_files, files_to_modules
    from lib.config import get_modules

    if diff_base is None:
        changed = git_diff_files(n=1, cwd=project_root)
    else:
        changed = _diff_files_from_base(diff_base, project_root)

    if not changed:
        print("无文件改动，跳过 CI")
        return True if not auto_fix else (True, [])

    # 检查是否全是文档
    all_docs = all(
        f.endswith(('.md', '.txt', '.yaml', '.yml', '.json', '.tsv'))
        for f in changed
    )

    if all_docs:
        print("仅文档改动，只跑 preflight")
        ok = _run_cmd("bash scripts/test-governance-gate.sh preflight", project_root)
        return ok if not auto_fix else (ok, [])

    all_modules = get_modules(project_root)
    module_files = files_to_modules(changed, all_modules)
    modules_by_name = {m.name: m for m in all_modules}
    affected = [modules_by_name[mod_name] for mod_name in module_files if mod_name in modules_by_name]

    if not affected:
        print("改动文件未匹配到已知模块，只跑 preflight")
        ok = _run_cmd("bash scripts/test-governance-gate.sh preflight", project_root)
        return ok if not auto_fix else (ok, [])

    print(f"受影响模块：{', '.join(m.name for m in affected)}")

    # === 阶段 1：preflight ===
    preflight_ok = _run_cmd(
        "bash scripts/test-governance-gate.sh preflight", project_root,
    )

    # === 阶段 2：静态检查 + 测试（一次跑完，带 capture）===
    static_failures = _collect_static_failures(affected, project_root)
    test_failures = _run_all_tests(affected, project_root)

    # 显示各项结果
    for f in static_failures:
        print(f"  失败: [{f['module']}] {f['command']}")
    for f in test_failures:
        print(f"  失败: [{f['module']}] {f['command']}")

    all_failures = static_failures + test_failures
    all_ok = preflight_ok and not all_failures

    if all_ok:
        print("\nCI 全部通过")
        return True if not auto_fix else (True, [])

    if not auto_fix:
        print("\nCI 有失败项")
        return False

    # preflight 失败不能靠 AI 修（治理门禁问题），直接写入 pending
    pending_from_preflight = []
    if not preflight_ok:
        pending_from_preflight.append({
            "type": "preflight",
            "module": "_governance",
            "command": "bash scripts/test-governance-gate.sh preflight",
            "output": "治理门禁失败，需要人工检查",
            "reason": "preflight 治理门禁失败，非代码问题，无法自动修复",
        })

    # === 阶段 3：auto_fix 修复循环 ===
    print(f"\n{'='*60}")
    print("CI 有失败项，进入 auto_fix 修复循环")
    print(f"{'='*60}")

    if all_failures:
        pending_items = _fix_loop(
            project_root, affected, all_failures, modules_by_name,
        )
    else:
        pending_items = []

    # 合入 preflight pending
    pending_items = pending_from_preflight + pending_items

    # 写入 pending 文件
    if pending_items and pending_file:
        _write_pending(pending_items, pending_file)

    final_ok = len(pending_items) == 0
    if final_ok:
        print("\nauto_fix 修复循环完成，CI 全部通过")
    else:
        print(f"\nauto_fix 完成，{len(pending_items)} 个问题未能自动修复（已写入 pending）")

    return (final_ok, pending_items)


# ==================== 静态检查 ====================

def _collect_static_failures(affected, project_root):
    """重新跑静态检查，收集失败的命令和错误输出。"""
    failures = []
    for m in affected:
        for cmd_attr in ("lint_command", "typecheck_command"):
            cmd = getattr(m, cmd_attr, "")
            if not cmd:
                continue
            ok, output = _run_cmd_capture(cmd, project_root)
            if not ok:
                failures.append({
                    "type": "static",
                    "module": m.name,
                    "command": cmd,
                    "output": output,
                })
    return failures


# ==================== 测试执行 ====================

def _run_all_tests(affected, project_root):
    """跑所有受影响模块的测试，返回失败列表。"""
    failures = []

    for m in affected:
        if m.unit_command:
            ok, output = _run_cmd_capture(m.unit_command, project_root)
            if not ok:
                failures.append({
                    "type": "unit_test",
                    "module": m.name,
                    "command": m.unit_command,
                    "output": output,
                })

    # 跨模块检查
    if len(affected) > 1:
        for m in affected:
            if m.cross_command:
                ok, output = _run_cmd_capture(m.cross_command, project_root)
                if not ok:
                    failures.append({
                        "type": "cross_test",
                        "module": m.name,
                        "command": m.cross_command,
                        "output": output,
                    })

    return failures


# ==================== auto_fix 修复循环 ====================

def _fix_loop(project_root, affected, initial_failures, modules_by_name):
    """修复循环主逻辑。

    在 worktree 中修复，绿了合回主分支。修不好的丢弃 worktree，记入 pending。

    Returns:
        pending 问题列表（空 = 全部修好）
    """
    from lib.worktree import create_worktree, merge_worktree, remove_worktree
    from lib.worktree import commit_in_worktree

    pending = []
    # 追踪每个失败的连续修复失败次数，key = (module, command)
    consecutive_fail_count = {}

    # 创建修复用 worktree
    wt = create_worktree("ci-fix", project_root)
    # 手动触发受影响模块的环境预检（create_worktree 的内置预检
    # 只检查 name="ci-fix" 对应的目录，找不到真实模块的 package.json / go.mod）
    _precheck_affected_modules(wt, affected)
    logger.info("创建 ci-fix worktree: %s", wt.path)

    current_failures = initial_failures
    has_any_fix = False

    for round_num in range(1, MAX_FIX_ROUNDS + 1):
        if not current_failures:
            break

        print(f"\n--- auto_fix 第 {round_num}/{MAX_FIX_ROUNDS} 轮（{len(current_failures)} 个失败）---")

        round_fixed_any = False

        for failure in current_failures:
            fail_key = (failure["module"], failure["command"])

            # 检查连续失败次数
            if consecutive_fail_count.get(fail_key, 0) >= MAX_CONSECUTIVE_FAIL:
                logger.info("跳过 %s（连续 %d 次修不好）", fail_key, MAX_CONSECUTIVE_FAIL)
                continue

            print(f"\n  修复: [{failure['module']}] {failure['command']}")

            # AI 修复
            fix_ok = _ai_fix_in_worktree(wt, failure, project_root)

            if not fix_ok:
                consecutive_fail_count[fail_key] = consecutive_fail_count.get(fail_key, 0) + 1
                logger.info("AI 修复失败: %s（第 %d 次）", fail_key, consecutive_fail_count[fail_key])
                continue

            # 修完后在 worktree 内重跑该命令验证
            verify_ok, _ = _run_cmd_capture(failure["command"], wt.path)
            if not verify_ok:
                consecutive_fail_count[fail_key] = consecutive_fail_count.get(fail_key, 0) + 1
                logger.info("修复后验证仍失败: %s（第 %d 次）", fail_key, consecutive_fail_count[fail_key])
                continue

            # 单项修复成功，重置连续失败计数
            consecutive_fail_count[fail_key] = 0
            round_fixed_any = True
            has_any_fix = True
            print(f"  修复成功: [{failure['module']}] {failure['command']}")

        if not round_fixed_any:
            logger.info("第 %d 轮无任何修复成功，停止循环", round_num)
            break

        # 本轮有修复，commit 并回归全量受影响模块测试
        commit_in_worktree(wt, f"evo-ci-fix: 第 {round_num} 轮自动修复")

        print(f"\n  回归测试（全量受影响模块）...")
        regression_failures = _run_regression_in_worktree(wt, affected)

        if not regression_failures:
            print("  回归全绿")
            current_failures = []
            break

        # 检测连锁破坏：回归出现了之前没有的新失败
        old_keys = {(f["module"], f["command"]) for f in current_failures}
        new_keys = {(f["module"], f["command"]) for f in regression_failures}
        chain_break = new_keys - old_keys

        if chain_break:
            print(f"\n  检测到连锁破坏：修复引入了 {len(chain_break)} 个新失败")
            for mod, cmd in chain_break:
                print(f"    - [{mod}] {cmd}")
            print("  停止修复循环，整包写入 pending")
            # 连锁破坏 → 丢弃本轮所有修改
            _reset_worktree(wt)
            # 所有失败（包括原有和新增）都写入 pending
            for f in regression_failures:
                pending.append({
                    "reason": "连锁破坏，自动修复引入新回归",
                    **f,
                })
            current_failures = []
            has_any_fix = False  # 标记无有效修复，不合并
            break

        current_failures = regression_failures

    # 循环结束，收集剩余未修好的
    for failure in current_failures:
        fail_key = (failure["module"], failure["command"])
        pending.append({
            "reason": f"连续 {consecutive_fail_count.get(fail_key, 0)} 次修复失败",
            **failure,
        })

    # 合并或丢弃 worktree
    # 安全策略：只有全量回归通过（pending 为空）才合并，否则整包丢弃。
    # 部分修复的 commit 可能包含引入退化的代码，合并到主分支不安全。
    if has_any_fix and not pending:
        # 全部修好，回归全绿，合并回主分支
        commit_in_worktree(wt, "evo-ci-fix: 自动修复完成")
        try:
            merge_worktree(wt, project_root)
            print("  修复已合并到主分支")
        except Exception as e:
            logger.error("合并 ci-fix worktree 失败: %s", e)
            pending.append({
                "reason": f"worktree 合并失败: {e}",
                "type": "merge_conflict",
                "module": "ci-fix",
                "command": "",
                "output": str(e),
            })
    else:
        # 有 pending 或完全没修好 → 丢弃 worktree，不合并任何代码到主分支
        remove_worktree(wt.path, project_root)
        if has_any_fix:
            logger.info("有部分修复但回归未全绿，丢弃 ci-fix worktree（不合并不安全的代码）")
        else:
            logger.info("无有效修复，已丢弃 ci-fix worktree")

    return pending


def _ai_fix_in_worktree(wt, failure, project_root):
    """在 worktree 中调用 AI 修复一个失败。

    Returns:
        True 表示 AI 修复调用成功（不代表测试一定通过），False 表示调用本身失败。
    """
    from lib.claude import call_claude_session

    # 构建修复 prompt
    error_output = failure.get("output", "")
    # 截断过长的错误输出
    if len(error_output) > 4000:
        error_output = error_output[:2000] + "\n...(截断)...\n" + error_output[-2000:]

    prompt = _build_fix_prompt(failure, error_output)

    # 估算超时
    timeout = 600  # 默认 10 分钟

    try:
        call_claude_session(
            prompt=prompt,
            model="opus",
            tools="Read,Glob,Grep,Edit,Write",
            max_turns=25,
            cwd=wt.path,
            timeout=timeout,
        )
        return True
    except Exception as e:
        logger.error("AI 修复调用失败: %s", e)
        return False


def _build_fix_prompt(failure, error_output):
    """构建修复 prompt，给 AI 足够上下文让它自己判断修复方向。"""
    fail_type = failure.get("type", "unknown")
    module = failure.get("module", "unknown")
    command = failure.get("command", "")

    type_desc = {
        "static": "静态检查（lint/typecheck）",
        "unit_test": "单元测试",
        "cross_test": "跨模块集成测试",
    }.get(fail_type, fail_type)

    return f"""你是代码修复专家。CI 中 [{module}] 模块的{type_desc}失败了，需要你修复。

## 失败命令
```
{command}
```

## 错误输出
```
{error_output}
```

## 修复要求

1. **先读代码再修**：阅读错误涉及的源码文件和测试文件，理解上下文。
2. **自己判断修复方向**：
   - 如果是生产代码有 bug → 改生产代码
   - 如果是测试本身写错了（断言不合理、mock 过时等）→ 改测试
   - 如果是 lint/type 错误 → 改对应代码
   不要猜，读完代码后根据理解判断。
3. **最小改动原则**：只改必要的代码，不要顺手重构或"改进"其他部分。
4. **不要删测试**：除非测试本身在测一个已删除的功能，否则不能通过删测试来"修复"。
5. **修完后不需要跑测试**（外层会自动回归验证）。

开始修复。"""


def _run_regression_in_worktree(wt, affected):
    """在 worktree 中跑全量受影响模块测试，返回失败列表。"""
    failures = []

    for m in affected:
        # 静态检查
        for cmd_attr in ("lint_command", "typecheck_command"):
            cmd = getattr(m, cmd_attr, "")
            if not cmd:
                continue
            ok, output = _run_cmd_capture(cmd, wt.path)
            if not ok:
                failures.append({
                    "type": "static",
                    "module": m.name,
                    "command": cmd,
                    "output": output,
                })

        # 单元测试
        if m.unit_command:
            ok, output = _run_cmd_capture(m.unit_command, wt.path)
            if not ok:
                failures.append({
                    "type": "unit_test",
                    "module": m.name,
                    "command": m.unit_command,
                    "output": output,
                })

    # 跨模块
    if len(affected) > 1:
        for m in affected:
            if m.cross_command:
                ok, output = _run_cmd_capture(m.cross_command, wt.path)
                if not ok:
                    failures.append({
                        "type": "cross_test",
                        "module": m.name,
                        "command": m.cross_command,
                        "output": output,
                    })

    return failures


def _reset_worktree(wt):
    """重置 worktree 到干净状态，回退 commit + 丢弃改动。"""
    try:
        # 回退到 worktree 创建时的基准 commit（即主分支 HEAD）
        subprocess.run(
            ["git", "reset", "--hard", "HEAD~"],
            cwd=wt.path,
            capture_output=True, text=True,
            # reset 失败（如无 commit 可回退）不致命，继续 checkout
        )
        subprocess.run(
            ["git", "checkout", "."],
            cwd=wt.path,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=wt.path,
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("已重置 worktree: %s", wt.path)
    except Exception as e:
        logger.error("重置 worktree 失败: %s", e)


def _precheck_affected_modules(wt, affected):
    """为受影响模块执行环境预检（npm install / go list 等）。

    create_worktree("ci-fix") 的内置预检按 name="ci-fix" 查找目录，
    找不到真实模块的 package.json / go.mod，所以这里按实际模块名逐个预检。
    """
    from lib.worktree import _precheck_single_module
    for m in affected:
        try:
            _precheck_single_module(wt.path, m.name)
        except Exception as e:
            logger.warning("预检模块 %s 异常（不阻塞）: %s", m.name, e)


# ==================== pending 输出 ====================

def _write_pending(pending_items, pending_file):
    """将 pending 问题写入 markdown 文件。"""
    from datetime import datetime

    os.makedirs(os.path.dirname(pending_file), exist_ok=True)

    lines = [
        f"# CI auto_fix 待决问题",
        f"",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"共 {len(pending_items)} 个问题未能自动修复。",
        f"",
    ]

    for i, item in enumerate(pending_items, 1):
        lines.append(f"## Q{i}: [{item.get('module', '?')}] {item.get('type', '?')}")
        lines.append(f"")
        lines.append(f"- **原因**: {item.get('reason', '未知')}")
        lines.append(f"- **命令**: `{item.get('command', '?')}`")
        lines.append(f"- **错误输出**（最后 30 行）:")
        lines.append(f"```")
        output = item.get("output", "")
        output_lines = output.strip().split("\n")
        for line in output_lines[-30:]:
            lines.append(line)
        lines.append(f"```")
        lines.append(f"")

    with open(pending_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info("pending 问题已写入: %s", pending_file)


# ==================== 工具函数 ====================

def _run_cmd(cmd, cwd):
    """执行命令并显示结果（兼容原有行为）。"""
    print(f"  > {cmd}")
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            print("  通过")
            return True
        else:
            output = (result.stdout + result.stderr).strip().split('\n')
            for line in output[-20:]:
                print(f"    {line}")
            print(f"  失败 (exit {result.returncode})")
            return False
    except subprocess.TimeoutExpired:
        print("  超时")
        return False
    except Exception as e:
        print(f"  异常: {e}")
        return False


def _run_cmd_capture(cmd, cwd):
    """执行命令，返回 (成功与否, 完整输出)。"""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=600,
        )
        output = (result.stdout + result.stderr).strip()
        return (result.returncode == 0, output)
    except subprocess.TimeoutExpired:
        return (False, "命令超时（600s）")
    except Exception as e:
        return (False, f"异常: {e}")


def _diff_files_from_base(base, project_root):
    """从指定 base 获取变更文件列表。"""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base],
            cwd=project_root,
            capture_output=True, text=True, check=True,
        )
        return [line for line in result.stdout.strip().splitlines() if line.strip()]
    except Exception:
        return []
