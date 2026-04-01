"""cover 命令：分析跨模块测试覆盖缺口，自动生成集成测试

流程：
Phase 1: 覆盖分析 — 构建覆盖矩阵（模块边界对 × 6 测试维度）
Phase 2: 缺口排序 — P0 > 边界无测试 > 缺维度，结合 trend 弱点
Phase 3: 确认 — 展示计划，用户确认后继续
Phase 4: 测试生成 — worktree 内并行生成，每个绿灯验证
Phase 5: 合并 + 报告 — 合并回主分支，跑 cross 测试，输出报告
"""

import logging
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# 并行生成测试的 worker 数
MAX_WORKERS = 3
# 保护并发写入
_lock = threading.Lock()

# 6 个测试维度
DIMENSIONS = [
    "happy_path",
    "cleanup",
    "concurrency",
    "error_recovery",
    "security_boundary",
    "fault_tolerance",
]

DIMENSION_LABELS = {
    "happy_path": "正常路径",
    "cleanup": "副作用清理",
    "concurrency": "并发安全",
    "error_recovery": "错误恢复",
    "security_boundary": "安全边界",
    "fault_tolerance": "故障后可用",
}


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

    # === Phase 1: 覆盖分析 ===
    print("\n=== Phase 1：覆盖分析 ===\n")
    analysis = _analyze_coverage(project_root, modules)
    if analysis is None:
        return False

    gaps = analysis["gaps"]
    matrix = analysis.get("coverage_matrix", [])
    summary = analysis.get("coverage_summary", {})

    # 打印覆盖矩阵
    _print_coverage_matrix(matrix, summary)

    if not gaps:
        print("\n跨模块测试覆盖完整，无需补充。")
        return True

    # === Phase 2: 缺口排序 ===
    print("\n=== Phase 2：缺口排序 ===\n")
    gaps = _prioritize_gaps(gaps, project_root)
    _print_gap_plan(gaps)

    # === Phase 3: 确认 ===
    print("\n=== Phase 3：确认 ===\n")
    if not _confirm_plan(gaps):
        print("已取消。")
        return True

    # === Phase 4: 测试生成 ===
    print(f"\n=== Phase 4：测试生成（{len(gaps)} 个） ===\n")
    results = _generate_tests(project_root, modules, gaps)

    success = sum(1 for r in results.values() if r["status"] == "ok")
    failed = sum(1 for r in results.values() if r["status"] == "failed")

    # === Phase 5: 合并 + 报告 ===
    if success > 0:
        print(f"\n=== Phase 5：合并 + 验证 ===\n")
        _merge_and_verify(project_root, modules)

    _print_report(results, gaps)
    return True


# ==================== Phase 1: 覆盖分析 ====================

def _analyze_coverage(project_root, modules):
    """Phase 1：分析覆盖缺口。

    一次 Claude bare 调用（opus），读已有测试 + 源码边界 + P0 + trend，
    输出覆盖矩阵和缺口清单。
    """
    from lib.claude import call_claude_bare
    from lib.prompts.cover import ANALYZE_COVERAGE_PROMPT
    from lib.schemas.cover import COVERAGE_GAPS_SCHEMA

    modules_info = "\n".join(
        f"- **{m.name}**（{m.language}）: `{m.src_dir}` | 测试: `{m.test_dir}`"
        for m in modules
    )
    topology_summary = _read_topology(project_root)
    p0_cases = _read_p0_cases(project_root)
    existing_tests = _extract_existing_tests(project_root, modules)
    helpers_summary = _extract_helpers(project_root, modules)
    trend_weaknesses = _read_trend_weaknesses(project_root)

    prompt = ANALYZE_COVERAGE_PROMPT.format(
        modules_info=modules_info,
        topology_summary=topology_summary,
        p0_cases=p0_cases if p0_cases else "无 P0 场景定义",
        existing_tests=existing_tests if existing_tests else "无现有跨模块测试",
        helpers_summary=helpers_summary if helpers_summary else "无测试 helper",
        trend_weaknesses=trend_weaknesses if trend_weaknesses else "无历史趋势数据",
    )

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
        return None

    gaps = result.get("gaps", [])
    # 全局重编号
    for i, g in enumerate(gaps, 1):
        g["id"] = f"G{i}"

    return result


# ==================== Phase 2: 缺口排序 ====================

def _prioritize_gaps(gaps, project_root):
    """Phase 2：结合 trend 数据对缺口排序。

    排序规则：
    1. P0 > P1 > P2
    2. 同优先级内，trend 弱项 category 优先
    3. 同优先级同 category，error_recovery/concurrency/fault_tolerance 维度优先
    """
    priority_order = {"P0": 0, "P1": 1, "P2": 2}
    # 高价值维度排前面
    dimension_order = {
        "error_recovery": 0,
        "concurrency": 1,
        "fault_tolerance": 2,
        "security_boundary": 3,
        "cleanup": 4,
        "happy_path": 5,
    }

    # 从 trend 读取弱项 category（幻觉率高的排前面）
    weak_categories = _get_weak_categories(project_root)

    def sort_key(gap):
        p = priority_order.get(gap.get("priority", "P2"), 2)
        # 弱项 category boost（在 weak_categories 中的排前面）
        cat_boost = 0 if gap.get("module_pair", "") in weak_categories else 1
        d = dimension_order.get(gap.get("dimension", "happy_path"), 5)
        return (p, cat_boost, d)

    gaps.sort(key=sort_key)
    return gaps


def _get_weak_categories(project_root):
    """从 history.jsonl 读取弱项（幻觉率 > 50% 的 category）。"""
    try:
        from lib.steps.history import load_history
        entries = load_history(project_root)
        if not entries:
            return set()

        from collections import defaultdict
        cat_stats = defaultdict(lambda: {"verified": 0, "hallucination": 0})
        for e in entries[-20:]:  # 最近 20 次
            for cat, stats in e.get("by_category", {}).items():
                cat_stats[cat]["verified"] += stats.get("verified", 0)
                cat_stats[cat]["hallucination"] += stats.get("hallucination", 0)

        weak = set()
        for cat, stats in cat_stats.items():
            total = stats["verified"] + stats["hallucination"]
            if total >= 3 and stats["hallucination"] / total > 0.5:
                weak.add(cat)
        return weak
    except Exception:
        return set()


# ==================== Phase 3: 确认 ====================

def _confirm_plan(gaps):
    """Phase 3：展示计划，等待用户确认。

    在 Claude Code 的 skill 调用中，stdin 不可用，此时自动确认。
    """
    print(f"共 {len(gaps)} 个缺口将生成集成测试。")
    print("确认后开始生成（每个缺口约 2-5 分钟）。\n")

    # 检测是否在非交互模式（如被 Claude skill 调用）
    if not sys.stdin.isatty():
        print("（非交互模式，自动确认）")
        return True

    try:
        answer = input("继续？[Y/n] ").strip().lower()
        return answer in ("", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


# ==================== Phase 4: 测试生成 ====================

def _generate_tests(project_root, modules, gaps):
    """Phase 4：为每个缺口生成测试（并行）。"""
    from lib.worktree import create_worktree, commit_in_worktree

    test_module = _find_cross_test_module(project_root, modules)
    if not test_module:
        logger.error("未找到跨模块测试目录")
        return {}

    wt = create_worktree("cover", project_root)
    logger.info("worktree 已创建：%s", wt.path)

    test_pattern = _read_test_pattern(project_root, modules)
    helpers_available = _extract_helpers(project_root, modules)

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

    has_ok = any(r["status"] == "ok" for r in results.values())
    if has_ok:
        commit_in_worktree(wt, "evo-cover: 新增跨模块集成测试")

    return results


def _generate_single_test(gap, wt, test_module, project_root, test_pattern, helpers_available):
    """为单个缺口生成测试文件。

    流程：写测试 → 跑绿灯 → 失败修一次 → 仍失败删除
    """
    from lib.claude import call_claude_session
    from lib.prompts.cover import GENERATE_TEST_PROMPT, FIX_TEST_PROMPT

    gap_id = gap["id"]
    timeout = test_module.estimate_timeout(project_root, task="verify")

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

    test_file = _find_new_test_file(wt.path, gap_id, test_module.language)
    if not test_file:
        return {"status": "failed", "reason": "未找到生成的测试文件"}

    test_result = _run_single_test(wt.path, test_file, test_module)
    if test_result["exit_code"] == 0:
        return {"status": "ok", "test_file": test_file}

    # 修一次
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

    retry_result = _run_single_test(wt.path, test_file, test_module)
    if retry_result["exit_code"] == 0:
        return {"status": "ok", "test_file": test_file}

    # 删除失败的测试文件
    abs_path = os.path.join(wt.path, test_file)
    if os.path.exists(abs_path):
        os.remove(abs_path)
        logger.info("[%s] 已删除失败的测试文件: %s", gap_id, test_file)

    return {"status": "failed", "reason": "修复后测试仍失败", "test_file": test_file}


# ==================== Phase 5: 合并 + 报告 ====================

def _merge_and_verify(project_root, modules):
    """Phase 5：合并 worktree，跑一次 cross 测试确认不破坏已有。"""
    from lib.worktree import merge_worktree, Worktree

    wt_path = os.path.join(project_root, ".evo-review", "worktrees", "cover")
    if not os.path.exists(wt_path):
        logger.warning("cover worktree 不存在，跳过合并")
        return

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


def _print_coverage_matrix(matrix, summary):
    """打印覆盖矩阵。"""
    if not matrix:
        if summary:
            print(f"覆盖概况：{summary.get('existing_test_count', '?')} 个现有测试，"
                  f"{summary.get('covered_pairs', '?')}/{summary.get('total_boundary_pairs', '?')} 个边界对已覆盖")
            dim_cov = summary.get("dimension_coverage", {})
            if dim_cov:
                print("维度覆盖：" + "  ".join(
                    f"{DIMENSION_LABELS.get(d, d)}={n}" for d, n in dim_cov.items()
                ))
        return

    # 打印矩阵表格
    print("覆盖矩阵（已覆盖/未覆盖）：\n")

    # 表头
    dim_short = ["正常", "清理", "并发", "错误", "安全", "容错"]
    header = f"  {'模块边界对':<30s}  " + "  ".join(f"{d:>4s}" for d in dim_short)
    print(header)
    print("  " + "-" * (len(header) - 2))

    for row in matrix:
        pair = row.get("module_pair", "?")[:30]
        cells = []
        for dim in DIMENSIONS:
            covered = row.get("dimensions", {}).get(dim, False)
            cells.append("  ++" if covered else "  --")
        print(f"  {pair:<30s}{''.join(cells)}")

    print()
    if summary:
        total = summary.get("total_boundary_pairs", 0)
        covered = summary.get("covered_pairs", 0)
        if total > 0:
            pct = covered * 100 // total
            print(f"  边界对覆盖率：{covered}/{total}（{pct}%）")


def _print_gap_plan(gaps):
    """打印排序后的缺口计划。"""
    by_priority = {"P0": [], "P1": [], "P2": []}
    for g in gaps:
        by_priority.get(g.get("priority", "P2"), by_priority["P2"]).append(g)

    for pri in ("P0", "P1", "P2"):
        items = by_priority[pri]
        if not items:
            continue
        print(f"\n{pri}（{len(items)} 个）：")
        for g in items:
            dim_label = DIMENSION_LABELS.get(g.get("dimension", ""), g.get("dimension", ""))
            print(f"  [{g['id']}] {g['module_pair']} | {dim_label}")
            print(f"         {g['scenario']}")


def _print_report(results, gaps):
    """打印最终报告。"""
    success = sum(1 for r in results.values() if r["status"] == "ok")
    failed = sum(1 for r in results.values() if r["status"] == "failed")

    print(f"\n{'='*60}")
    print(f"  Cover 完成：{success} 个测试生成成功，{failed} 个失败")
    print(f"{'='*60}")

    if success > 0:
        print("\n新增的测试文件：")
        for gap_id, r in results.items():
            if r["status"] == "ok":
                # 找到对应的 gap 描述
                gap_desc = ""
                for g in gaps:
                    if g["id"] == gap_id:
                        gap_desc = g.get("scenario", "")[:60]
                        break
                print(f"  [{gap_id}] {r.get('test_file', '?')}")
                if gap_desc:
                    print(f"         {gap_desc}")

    if failed > 0:
        print("\n失败的缺口（下次 cover 会重新尝试）：")
        for gap_id, r in results.items():
            if r["status"] == "failed":
                print(f"  [{gap_id}] {r.get('reason', '?')}")


# ==================== 数据读取函数 ====================

def _read_topology(project_root):
    """读取 cross-module-topology.md"""
    topo_path = os.path.join(project_root, "test-governance", "cross-module-topology.md")
    if os.path.isfile(topo_path):
        with open(topo_path, "r", encoding="utf-8") as f:
            return f.read()[:5000]
    return "未找到 cross-module-topology.md"


def _read_p0_cases(project_root):
    """读取 p0-cases.tsv"""
    p0_path = os.path.join(project_root, "test-governance", "p0-cases.tsv")
    if os.path.isfile(p0_path):
        with open(p0_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def _read_trend_weaknesses(project_root):
    """从 history.jsonl 读取 trend 弱点信息，供分析阶段参考。"""
    try:
        from lib.steps.history import load_history
        entries = load_history(project_root)
        if not entries:
            return ""

        from collections import defaultdict
        cat_stats = defaultdict(lambda: {"verified": 0, "hallucination": 0, "total": 0})
        for e in entries[-20:]:
            for cat, stats in e.get("by_category", {}).items():
                cat_stats[cat]["verified"] += stats.get("verified", 0)
                cat_stats[cat]["hallucination"] += stats.get("hallucination", 0)
                cat_stats[cat]["total"] += stats.get("total", 0)

        lines = ["## 历史 Review 趋势（category 弱点）\n"]
        lines.append("以下 category 在历次 review 中幻觉率较高，生成测试时应优先覆盖：\n")
        for cat, stats in sorted(cat_stats.items(), key=lambda x: -x[1]["hallucination"]):
            decidable = stats["verified"] + stats["hallucination"]
            if decidable >= 2:
                hallu_rate = stats["hallucination"] / decidable
                lines.append(
                    f"- {cat}: {hallu_rate*100:.0f}% 幻觉率 "
                    f"({stats['verified']}/{decidable} verified)"
                )
        return "\n".join(lines)
    except Exception:
        return ""


def _extract_existing_tests(project_root, modules):
    """提取现有集成测试的场景描述（轻量，不读实现）。

    按语言选择 grep 模式：
    - TypeScript/JS：提取 describe()/it() 行
    - Go：提取 func Test* 行
    - Python：提取 def test_* 和 class Test* 行
    - Swift：提取 func test* 行
    """
    test_dirs = _find_integration_test_dirs(project_root, modules)
    if not test_dirs:
        return ""

    # 根据项目语言构造 grep 模式
    languages = {m.language for m in modules}
    patterns = []
    if languages & {"typescript", "javascript"}:
        patterns.append(r"(describe|it|test)\(")
    if "go" in languages:
        patterns.append(r"^func Test")
    if "python" in languages:
        patterns.append(r"(def test_|class Test)")
    if "swift" in languages:
        patterns.append(r"func test")

    if not patterns:
        patterns = [r"(describe|it|test)\(", r"^func Test", r"def test_"]

    grep_pattern = "|".join(patterns)

    lines = []
    for test_dir in test_dirs:
        try:
            result = subprocess.run(
                ["grep", "-rn", "-E", grep_pattern, test_dir],
                capture_output=True, text=True, timeout=15,
            )
            if result.stdout:
                current_file = None
                for line in result.stdout.strip().split("\n"):
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        fpath = os.path.relpath(parts[0], project_root)
                        if fpath != current_file:
                            current_file = fpath
                            lines.append(f"\n### {current_file}")
                        content = parts[2].strip()
                        lines.append(f"  {content}")
        except Exception as e:
            logger.debug("提取测试描述失败: %s", e)

    return "\n".join(lines) if lines else ""


def _extract_helpers(project_root, modules):
    """提取测试 helper 的函数签名摘要。

    按语言选择 grep 模式，不硬编码 TypeScript。
    """
    helper_dirs = _find_helper_dirs(project_root, modules)
    if not helper_dirs:
        return ""

    languages = {m.language for m in modules}
    # 按语言构造导出函数的 grep 模式
    patterns = []
    if languages & {"typescript", "javascript"}:
        patterns.append(r"^export (async )?function |^export const \w+ =")
    if "go" in languages:
        patterns.append(r"^func [A-Z]")  # Go 导出函数以大写开头
    if "python" in languages:
        patterns.append(r"^def [a-z]")
    if "swift" in languages:
        patterns.append(r"^(public |open )?func ")

    if not patterns:
        patterns = [r"^export (async )?function |^func [A-Z]|^def [a-z]"]

    grep_pattern = "|".join(patterns)

    # 常见源码扩展名
    source_exts = {".ts", ".js", ".go", ".py", ".swift"}

    lines = []
    for hdir in helper_dirs:
        if not os.path.isdir(hdir):
            continue
        for fname in sorted(os.listdir(hdir)):
            ext = os.path.splitext(fname)[1]
            if ext not in source_exts:
                continue
            fpath = os.path.join(hdir, fname)
            rel = os.path.relpath(fpath, project_root)
            lines.append(f"\n### {rel}")
            try:
                result = subprocess.run(
                    ["grep", "-n", "-E", grep_pattern, fpath],
                    capture_output=True, text=True, timeout=5,
                )
                if result.stdout:
                    for line in result.stdout.strip().split("\n"):
                        lines.append(f"  {line.strip()}")
            except Exception:
                pass

    return "\n".join(lines) if lines else ""


# ==================== 文件查找函数 ====================

# 各语言的测试文件后缀
_TEST_SUFFIXES = {
    "typescript": (".test.ts", ".test.js", ".spec.ts", ".spec.js"),
    "javascript": (".test.js", ".spec.js"),
    "go": ("_test.go",),
    "python": (".py",),  # Python 用前缀 test_ 判断
    "swift": ("Tests.swift", "Test.swift"),
}

# 各语言的单文件测试命令模板
_SINGLE_TEST_CMD = {
    "typescript": "npx vitest run {test_file}",
    "javascript": "npx vitest run {test_file}",
    "go": "go test -race -run . ./{test_pkg}/",
    "python": "python -m pytest {test_file} -v",
    "swift": None,  # Swift 通过 xcodebuild，需要特殊处理
}


def _is_test_file(filename, language=None):
    """判断文件是否是测试文件。

    如果指定了 language 按该语言判断，否则按所有语言尝试。
    """
    basename = os.path.basename(filename)

    if language:
        langs = [language]
    else:
        langs = list(_TEST_SUFFIXES.keys())

    for lang in langs:
        suffixes = _TEST_SUFFIXES.get(lang, ())
        if lang == "python":
            if basename.startswith("test_") and basename.endswith(".py"):
                return True
            if basename.endswith("_test.py"):
                return True
        else:
            for suffix in suffixes:
                if filename.endswith(suffix):
                    return True
    return False


def _find_integration_test_dirs(project_root, modules):
    """查找集成测试目录。

    优先从 config.yaml 的 test_dir 获取，fallback 到自动扫描。
    """
    dirs = set()

    # 策略 1：从模块配置获取 test_dir
    for m in modules:
        if m.test_dir:
            test_path = os.path.join(project_root, m.test_dir)
            if os.path.isdir(test_path):
                dirs.add(test_path)
                for subdir in ("integration", "cross", "e2e", "cross-module"):
                    sub_path = os.path.join(test_path, subdir)
                    if os.path.isdir(sub_path):
                        dirs.add(sub_path)

    # 策略 2：fallback 扫描
    if not dirs:
        _SKIP = {"node_modules", ".git", ".evo-review", "dist", "build",
                 ".build", "DerivedData", "vendor", "__pycache__"}
        for root, dirnames, files in os.walk(project_root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP]
            basename = os.path.basename(root)
            if basename in ("integration", "cross", "e2e", "__tests__", "tests", "test"):
                dirs.add(root)

    return list(dirs)


def _find_helper_dirs(project_root, modules):
    """查找测试 helper 目录。

    从模块配置的 helper_dir 获取，fallback 到自动扫描。
    """
    dirs = set()

    for m in modules:
        if hasattr(m, "helper_dir") and m.helper_dir:
            helper_path = os.path.join(project_root, m.helper_dir)
            if os.path.isdir(helper_path):
                dirs.add(helper_path)

    if not dirs:
        _SKIP = {"node_modules", ".git", ".evo-review", "dist", "build",
                 ".build", "DerivedData", "vendor"}
        for root, dirnames, files in os.walk(project_root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP]
            if os.path.basename(root) == "helpers":
                parent = os.path.basename(os.path.dirname(root))
                if parent in ("__tests__", "tests", "test", "integration", "e2e"):
                    dirs.add(root)

    return list(dirs)


def _find_cross_test_module(project_root, modules):
    """找到跨模块测试所在的模块（有 cross_command 的模块）。"""
    for m in modules:
        if m.cross_command:
            return m
    for m in modules:
        if m.test_dir:
            return m
    return modules[0] if modules else None


def _read_test_pattern(project_root, modules):
    """读一个现有集成测试文件作为模式参考。

    按文件大小选择（跳过太短或太长的），不硬编码文件名。
    """
    test_dirs = _find_integration_test_dirs(project_root, modules)
    best = None
    best_size = 0

    for td in test_dirs:
        try:
            for f in sorted(os.listdir(td)):
                fpath = os.path.join(td, f)
                if not os.path.isfile(fpath):
                    continue
                if not _is_test_file(f):
                    continue
                size = os.path.getsize(fpath)
                if 1000 < size < 20000 and size > best_size:
                    best = fpath
                    best_size = size
        except Exception:
            continue

    if best:
        try:
            with open(best, "r", encoding="utf-8") as fh:
                return fh.read()[:4000]
        except Exception:
            pass
    return ""


def _find_new_test_file(wt_path, gap_id, language):
    """在 worktree 中查找新生成的测试文件。"""
    gap_lower = gap_id.lower()

    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=wt_path, capture_output=True, text=True,
    )
    new_files = [f for f in result.stdout.strip().split("\n") if f]

    # 优先找包含 gap_id 或 cover 的测试文件
    for f in new_files:
        if _is_test_file(f, language) and ("cover" in f.lower() or gap_lower in f.lower()):
            return f

    # fallback：任何新的测试文件
    for f in new_files:
        if _is_test_file(f, language):
            return f

    # 策略 2：修改的文件
    result = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=wt_path, capture_output=True, text=True,
    )
    for f in result.stdout.strip().split("\n"):
        if f and _is_test_file(f, language):
            return f

    return None


def _run_single_test(wt_path, test_file, module):
    """在 worktree 中运行单个测试文件。

    根据模块语言选择测试命令，不硬编码特定框架。
    """
    language = getattr(module, "language", "")
    mod_dir = wt_path
    if module.src_dir:
        mod_root = module.src_dir.rstrip("/").split("/")[0]
        candidate = os.path.join(wt_path, mod_root)
        if os.path.isdir(candidate):
            mod_dir = candidate

    # 构造单文件测试命令
    cmd_template = _SINGLE_TEST_CMD.get(language)
    if not cmd_template:
        if module.unit_command:
            cmd = module.unit_command
        else:
            return {"exit_code": 1, "output": f"不支持语言 {language} 的单文件测试"}
    elif language == "go":
        test_pkg = os.path.dirname(test_file) or "."
        cmd = cmd_template.format(test_file=test_file, test_pkg=test_pkg)
    else:
        cmd = cmd_template.format(test_file=test_file)

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
