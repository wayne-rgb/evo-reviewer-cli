"""cover 命令：分析跨模块测试覆盖缺口，自动生成集成测试

流程：
Phase 1: 分析覆盖缺口（一次 Claude bare 调用）
Phase 2: 逐个生成测试（并行 Claude session 调用，worktree 内工作）
Phase 3: 合并 worktree + 跑一次 cross 测试确认不破坏已有
"""

import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# 并行生成测试的 worker 数
MAX_WORKERS = 3
# 保护并发写入
_lock = threading.Lock()


def run_cover(project_root, module_filter=None):
    """cover 命令主入口。

    Args:
        project_root: 项目根目录
        module_filter: 可选，限定模块列表（如 ['togo-agent', 'agentapi']）

    Returns:
        True 成功，False 有失败
    """
    from lib.config import get_modules

    modules = get_modules(project_root)
    if module_filter:
        filter_set = set(module_filter)
        modules = [m for m in modules if m.name in filter_set]
        if not modules:
            print(f"指定的模块未找到：{module_filter}")
            return False

    print(f"分析模块：{', '.join(m.name for m in modules)}")

    # === Phase 1: 分析覆盖缺口 ===
    print("\n=== Phase 1：覆盖分析 ===\n")
    gaps = _analyze_coverage(project_root, modules)

    if not gaps:
        print("跨模块测试覆盖完整，无需补充。")
        return True

    print(f"发现 {len(gaps)} 个覆盖缺口：")
    for g in gaps:
        print(f"  [{g['id']}] {g['priority']} | {g['module_pair']} | {g['dimension']}")
        print(f"         {g['scenario']}")

    # === Phase 2: 生成测试 ===
    print(f"\n=== Phase 2：生成测试（{len(gaps)} 个） ===\n")
    results = _generate_tests(project_root, modules, gaps)

    # 统计
    success = sum(1 for r in results.values() if r["status"] == "ok")
    failed = sum(1 for r in results.values() if r["status"] == "failed")
    skipped = sum(1 for r in results.values() if r["status"] == "skipped")

    # === Phase 3: 合并 + 验证 ===
    if success > 0:
        print(f"\n=== Phase 3：合并 + 验证 ===\n")
        _merge_and_verify(project_root, modules)

    # === 报告 ===
    print(f"\n{'='*60}")
    print(f"  Cover 完成：{success} 个测试生成成功，{failed} 失败，{skipped} 跳过")
    print(f"{'='*60}")

    if success > 0:
        print("\n新增的测试文件：")
        for gap_id, r in results.items():
            if r["status"] == "ok":
                print(f"  {r.get('test_file', '?')}")

    if failed > 0:
        print("\n失败的缺口：")
        for gap_id, r in results.items():
            if r["status"] == "failed":
                print(f"  [{gap_id}] {r.get('reason', '?')}")

    return failed == 0


def _analyze_coverage(project_root, modules):
    """Phase 1：分析覆盖缺口。

    一次 Claude bare 调用（opus），读已有测试 + 源码边界，输出缺口清单。
    """
    from lib.claude import call_claude_bare
    from lib.prompts.cover import ANALYZE_COVERAGE_PROMPT
    from lib.schemas.cover import COVERAGE_GAPS_SCHEMA

    # 1. 构建模块信息
    modules_info = "\n".join(
        f"- **{m.name}**（{m.language}）: `{m.src_dir}` | 测试: `{m.test_dir}`"
        for m in modules
    )

    # 2. 读拓扑
    topology_summary = _read_topology(project_root)

    # 3. 读 P0 场景
    p0_cases = _read_p0_cases(project_root)

    # 4. 提取现有跨模块测试的场景描述（轻量：只读 describe/it，不读实现）
    existing_tests = _extract_existing_tests(project_root)

    # 5. 提取 helper 能力摘要
    helpers_summary = _extract_helpers(project_root)

    prompt = ANALYZE_COVERAGE_PROMPT.format(
        modules_info=modules_info,
        topology_summary=topology_summary,
        p0_cases=p0_cases if p0_cases else "无 P0 场景定义",
        existing_tests=existing_tests if existing_tests else "无现有跨模块测试",
        helpers_summary=helpers_summary if helpers_summary else "无测试 helper",
    )

    # 估算 timeout：模块数 × 基数
    timeout = min(max(600, len(modules) * 300), 1800)

    try:
        result = call_claude_bare(
            prompt=prompt,
            model="opus",
            tools="Read,Glob,Grep",
            output_schema=COVERAGE_GAPS_SCHEMA,
            max_turns=40,
            cwd=project_root,
            timeout=timeout,
        )
    except Exception as e:
        logger.error("覆盖分析失败: %s", e)
        return []

    gaps = result.get("gaps", [])
    summary = result.get("coverage_summary", {})

    if summary:
        print(f"覆盖概况：{summary.get('existing_test_count', '?')} 个现有测试，"
              f"{summary.get('covered_pairs', '?')}/{summary.get('total_boundary_pairs', '?')} 个边界对已覆盖")
        dim_cov = summary.get("dimension_coverage", {})
        if dim_cov:
            print("维度覆盖：" + "  ".join(f"{d}={n}" for d, n in dim_cov.items()))

    # 全局重编号
    for i, g in enumerate(gaps, 1):
        g["id"] = f"G{i}"

    return gaps


def _generate_tests(project_root, modules, gaps):
    """Phase 2：为每个缺口生成测试（并行）。

    在 worktree 中工作，每个缺口一次 Claude session 调用。
    """
    from lib.worktree import create_worktree, commit_in_worktree

    # 找到跨模块测试所在的模块（通常是主模块，如 togo-agent）
    test_module = _find_cross_test_module(project_root, modules)
    if not test_module:
        logger.error("未找到跨模块测试目录")
        return {}

    # 创建 worktree
    wt = create_worktree("cover", project_root)
    logger.info("worktree 已创建：%s", wt.path)

    # 读一个现有测试作为模式参考
    test_pattern = _read_test_pattern(project_root, test_module)
    helpers_available = _extract_helpers(project_root)

    results = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for gap in gaps:
            future = pool.submit(
                _generate_single_test,
                gap, wt, test_module, project_root,
                test_pattern, helpers_available,
            )
            futures[future] = gap["id"]

        for future in as_completed(futures):
            gap_id = futures[future]
            try:
                result = future.result()
                with _lock:
                    results[gap_id] = result
                status = result["status"]
                if status == "ok":
                    print(f"  [{gap_id}] 生成成功")
                else:
                    print(f"  [{gap_id}] {status}: {result.get('reason', '')[:80]}")
            except Exception as e:
                logger.error("[%s] 异常: %s", gap_id, e)
                with _lock:
                    results[gap_id] = {"status": "failed", "reason": str(e)}

    # 提交所有成功的测试
    has_ok = any(r["status"] == "ok" for r in results.values())
    if has_ok:
        commit_in_worktree(wt, "evo-cover: 新增跨模块集成测试")

    return results


def _generate_single_test(gap, wt, test_module, project_root, test_pattern, helpers_available):
    """为单个缺口生成测试文件。

    流程：
    1. Claude session 写测试
    2. 跑测试验证绿灯
    3. 失败则修一次
    4. 仍失败则跳过
    """
    from lib.claude import call_claude_session
    from lib.prompts.cover import GENERATE_TEST_PROMPT, FIX_TEST_PROMPT

    gap_id = gap["id"]
    timeout = test_module.estimate_timeout(project_root, task="verify")

    # 1. 生成测试
    prompt = GENERATE_TEST_PROMPT.format(
        gap_id=gap_id,
        module_pair=gap.get("module_pair", ""),
        scenario=gap.get("scenario", ""),
        dimension=gap.get("dimension", ""),
        priority=gap.get("priority", ""),
        test_hint=gap.get("test_hint", ""),
        test_pattern_example=test_pattern[:3000] if test_pattern else "无参考",
        helpers_available=helpers_available[:2000] if helpers_available else "无",
    )

    try:
        call_claude_session(
            prompt=prompt,
            model="opus",
            tools="Read,Glob,Grep,Edit,Write",
            max_turns=20,
            cwd=wt.path,
            timeout=timeout,
        )
    except Exception as e:
        return {"status": "failed", "reason": f"生成失败: {e}"}

    # 2. 找到新写的测试文件
    test_file = _find_new_test_file(wt.path, gap_id, test_module)
    if not test_file:
        return {"status": "failed", "reason": "未找到生成的测试文件"}

    # 3. 跑测试
    test_result = _run_single_test(wt.path, test_file, test_module)
    if test_result["exit_code"] == 0:
        return {"status": "ok", "test_file": test_file}

    # 4. 修一次
    logger.info("[%s] 测试失败，尝试修复", gap_id)
    fix_prompt = FIX_TEST_PROMPT.format(
        gap_id=gap_id,
        scenario=gap.get("scenario", ""),
        error_output=_tail(test_result["output"], 40),
    )

    try:
        call_claude_session(
            prompt=fix_prompt,
            model="opus",
            tools="Read,Glob,Grep,Edit,Write",
            max_turns=10,
            cwd=wt.path,
            timeout=timeout,
        )
    except Exception as e:
        return {"status": "failed", "reason": f"修复失败: {e}", "test_file": test_file}

    # 5. 重跑
    retry_result = _run_single_test(wt.path, test_file, test_module)
    if retry_result["exit_code"] == 0:
        return {"status": "ok", "test_file": test_file}

    # 仍失败 → 删除测试文件，避免合并坏测试
    abs_path = os.path.join(wt.path, test_file)
    if os.path.exists(abs_path):
        os.remove(abs_path)
        logger.info("[%s] 已删除失败的测试文件: %s", gap_id, test_file)

    return {"status": "failed", "reason": "修复后测试仍失败", "test_file": test_file}


def _merge_and_verify(project_root, modules):
    """Phase 3：合并 worktree，跑一次 cross 测试确认不破坏已有。"""
    from lib.worktree import merge_worktree, Worktree

    wt_path = os.path.join(project_root, ".evo-review", "worktrees", "cover")
    if not os.path.exists(wt_path):
        logger.warning("cover worktree 不存在，跳过合并")
        return

    # 读取 worktree 分支名
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=wt_path,
            capture_output=True, text=True, check=True,
        )
        branch = result.stdout.strip()
    except Exception:
        branch = "unknown"

    wt = Worktree(path=wt_path, branch=branch, modules=["cover"])
    merge_worktree(wt, project_root)
    print("worktree 已合并")

    # 跑 cross 测试验证
    test_module = _find_cross_test_module(project_root, modules)
    if test_module and test_module.cross_command:
        print(f"运行 cross 测试验证：{test_module.cross_command}")
        result = subprocess.run(
            test_module.cross_command,
            shell=True, cwd=project_root,
            capture_output=True, text=True,
            timeout=600,
        )
        if result.returncode == 0:
            print("cross 测试全部通过")
        else:
            output = (result.stdout + result.stderr).strip().split("\n")
            for line in output[-15:]:
                print(f"  {line}")
            print(f"cross 测试有失败（exit {result.returncode}）")


# ==================== 辅助函数 ====================

def _read_topology(project_root):
    """读取 cross-module-topology.md"""
    topo_path = os.path.join(project_root, "test-governance", "cross-module-topology.md")
    if os.path.isfile(topo_path):
        with open(topo_path, "r", encoding="utf-8") as f:
            return f.read()[:5000]  # 截断防止过长
    return "未找到 cross-module-topology.md"


def _read_p0_cases(project_root):
    """读取 p0-cases.tsv"""
    p0_path = os.path.join(project_root, "test-governance", "p0-cases.tsv")
    if os.path.isfile(p0_path):
        with open(p0_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def _extract_existing_tests(project_root):
    """提取现有跨模块测试的 describe/it 描述（轻量，不读实现）。

    用 grep 提取 describe() 和 it() 行，每个文件列出测试场景名。
    """
    # 查找所有 cross-module 测试文件
    test_dirs = _find_cross_test_dirs(project_root)
    if not test_dirs:
        return ""

    lines = []
    for test_dir in test_dirs:
        try:
            result = subprocess.run(
                ["grep", "-rn", "-E", r"(describe|it)\(", test_dir,
                 "--include=*cross-module*", "--include=*cross-operation*"],
                capture_output=True, text=True, timeout=15,
            )
            if result.stdout:
                # 按文件分组
                current_file = None
                for line in result.stdout.strip().split("\n"):
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        fpath = os.path.relpath(parts[0], project_root)
                        if fpath != current_file:
                            current_file = fpath
                            lines.append(f"\n### {current_file}")
                        # 只保留 describe/it 名称
                        content = parts[2].strip()
                        lines.append(f"  {content}")
        except Exception as e:
            logger.debug("提取测试描述失败: %s", e)

    return "\n".join(lines) if lines else ""


def _extract_helpers(project_root):
    """提取测试 helper 的函数签名摘要。"""
    helper_dirs = _find_helper_dirs(project_root)
    if not helper_dirs:
        return ""

    lines = []
    for hdir in helper_dirs:
        if not os.path.isdir(hdir):
            continue
        for fname in sorted(os.listdir(hdir)):
            if not fname.endswith((".ts", ".js")):
                continue
            fpath = os.path.join(hdir, fname)
            rel = os.path.relpath(fpath, project_root)
            lines.append(f"\n### {rel}")
            try:
                result = subprocess.run(
                    ["grep", "-n", "-E",
                     r"^export (async )?function |^export const \w+ =",
                     fpath],
                    capture_output=True, text=True, timeout=5,
                )
                if result.stdout:
                    for line in result.stdout.strip().split("\n"):
                        lines.append(f"  {line.strip()}")
            except Exception:
                pass

    return "\n".join(lines) if lines else ""


def _find_cross_test_dirs(project_root):
    """查找包含跨模块测试文件的目录。"""
    dirs = []
    for root, dirnames, files in os.walk(project_root):
        # 跳过常见无关目录
        dirnames[:] = [d for d in dirnames if d not in (
            "node_modules", ".git", ".evo-review", "dist", "build",
            ".build", "DerivedData", "vendor",
        )]
        for f in files:
            if "cross-module" in f and f.endswith((".test.ts", ".test.js")):
                dirs.append(root)
                break
    return dirs


def _find_helper_dirs(project_root):
    """查找测试 helper 目录。"""
    dirs = []
    for root, dirnames, files in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in (
            "node_modules", ".git", ".evo-review", "dist", "build",
        )]
        if os.path.basename(root) == "helpers" and "__tests__" in root:
            dirs.append(root)
    return dirs


def _find_cross_test_module(project_root, modules):
    """找到跨模块测试所在的模块（有 cross_command 的模块）。"""
    for m in modules:
        if m.cross_command:
            return m
    # fallback：第一个有 test_dir 的模块
    for m in modules:
        if m.test_dir:
            return m
    return modules[0] if modules else None


def _read_test_pattern(project_root, test_module):
    """读一个现有跨模块测试文件作为模式参考。"""
    test_dirs = _find_cross_test_dirs(project_root)
    for td in test_dirs:
        for f in sorted(os.listdir(td)):
            if "cross-module" in f and f.endswith(".test.ts"):
                fpath = os.path.join(td, f)
                try:
                    with open(fpath, "r", encoding="utf-8") as fh:
                        content = fh.read()
                    if len(content) > 500:  # 跳过太短的
                        return content[:4000]  # 只取前 4000 字符作为参考
                except Exception:
                    continue
    return ""


def _find_new_test_file(wt_path, gap_id, test_module):
    """在 worktree 中查找新生成的测试文件。"""
    # 策略 1：查找 gap_id 命名的文件
    gap_lower = gap_id.lower()
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=wt_path, capture_output=True, text=True,
    )
    new_files = [f for f in result.stdout.strip().split("\n") if f]

    # 优先找包含 gap_id 或 cover 的测试文件
    for f in new_files:
        if f.endswith((".test.ts", ".test.js")) and ("cover" in f or gap_lower in f.lower()):
            return f

    # fallback：任何新的测试文件
    for f in new_files:
        if f.endswith((".test.ts", ".test.js")) and "cross-module" in f:
            return f

    # 策略 2：查找修改的文件
    result = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=wt_path, capture_output=True, text=True,
    )
    for f in result.stdout.strip().split("\n"):
        if f and f.endswith((".test.ts", ".test.js")) and "cross-module" in f:
            return f

    return None


def _run_single_test(wt_path, test_file, module):
    """在 worktree 中运行单个测试文件。"""
    # 确定模块目录
    mod_dir = wt_path
    if module.src_dir:
        # src_dir 如 "togo-agent/src/" → 模块根目录 "togo-agent"
        mod_root = module.src_dir.rstrip("/").split("/")[0]
        candidate = os.path.join(wt_path, mod_root)
        if os.path.isdir(candidate):
            mod_dir = candidate

    cmd = f"npx vitest run {test_file}"
    logger.info("运行测试: cd %s && %s", mod_dir, cmd)

    try:
        result = subprocess.run(
            cmd, shell=True, cwd=mod_dir,
            capture_output=True, text=True,
            timeout=120,
        )
        return {
            "exit_code": result.returncode,
            "output": result.stdout + result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": 1, "output": "测试超时（120s）"}
    except Exception as e:
        return {"exit_code": 1, "output": str(e)}


def _tail(text, n=30):
    """取文本最后 n 行。"""
    lines = text.strip().split("\n")
    return "\n".join(lines[-n:])
