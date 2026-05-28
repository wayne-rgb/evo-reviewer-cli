"""R5 交叉检验三件套 (重写自 v2.5)

旧 R5 一次性给 opus 大 prompt 在 main 分支扫描,实战中超时 / 退出码 1 直接挂,
0 价值。本版本拆三件套,每个子任务独立失败不阻塞其他:

  R5-A 静态调用方影响:0 LLM,git diff 提 symbol + grep 调用方
  R5-B 同模式候选:每候选独立 30s LLM 调用,失败一个不影响其他
  R5-C adversarial 输入:每 verified bug 独立 30s,schema 强制具体值

R5 不再产生新 findings 进主流程 — 只产出独立报告写盘,用户自行 cherry-pick。
"""

import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# R5 总预算(分钟):超时则截断剩余子任务
R5_BUDGET_MINUTES = 15
# R5-B 每 verified bug 最多扫几个候选位置
R5B_MAX_CANDIDATES_PER_BUG = 5
# R5-A grep 调用方时的常用代码后缀
CODE_EXTENSIONS = (".go", ".ts", ".tsx", ".js", ".jsx", ".swift", ".py", ".java", ".kt")
# 应排除的目录(用 path 包含判断,简单粗暴)
EXCLUDE_DIR_SEGMENTS = (
    "vendor/", "node_modules/", ".evo-review/", "__pycache__/",
    "dist/", "build/", ".git/", "coverage/",
)


def run_cross_validate(state, project_root, modules_by_name):
    """R5 三件套调度入口。

    Args:
        state: ReviewState,需要 state.findings + state.results + state.worktrees
        project_root: 项目根目录
        modules_by_name: {name: ModuleConfig} 映射

    Side effects:
        - 写 .evo-review/r5-report-{session_id}.md
        - 写 state.r5_report_path 供 finalize 引用
    """
    verified = [
        f for f in state.findings
        if state.get_result_status(f["id"]) == "verified"
    ]
    if not verified:
        logger.info("R5: 无 verified bug,跳过三件套")
        return

    deadline = time.time() + R5_BUDGET_MINUTES * 60
    report = [
        f"# R5 交叉检验报告",
        f"",
        f"- 会话: `{state.session_id}`",
        f"- verified findings: {len(verified)}",
        f"- 预算: {R5_BUDGET_MINUTES} 分钟",
        f"",
    ]

    # R5-A:静态,先跑,绝对可靠
    try:
        report.extend(_r5_a_callgraph_impact(verified, state, project_root, modules_by_name))
    except Exception as e:
        logger.error(f"R5-A 调用方分析失败: {e}")
        report.append(f"\n## R5-A 调用方影响\n\n❌ 失败: {e}\n")

    # R5-B:微 LLM × N,失败一个不阻塞
    if time.time() < deadline:
        try:
            report.extend(_r5_b_similar_patterns(verified, state, project_root, modules_by_name, deadline))
        except Exception as e:
            logger.error(f"R5-B 同模式失败: {e}")
            report.append(f"\n## R5-B 同模式候选\n\n❌ 失败: {e}\n")
    else:
        report.append(f"\n## R5-B 同模式候选\n\n⏱ R5-A 已耗尽预算,跳过\n")

    # R5-C:微 LLM × verified,失败一个不阻塞
    if time.time() < deadline:
        try:
            report.extend(_r5_c_adversarial_inputs(verified, state, project_root, deadline))
        except Exception as e:
            logger.error(f"R5-C adversarial 失败: {e}")
            report.append(f"\n## R5-C adversarial 测试输入\n\n❌ 失败: {e}\n")
    else:
        report.append(f"\n## R5-C adversarial 测试输入\n\n⏱ R5-A/B 已耗尽预算,跳过\n")

    # 写盘
    state_dir = state.state_dir(project_root)
    os.makedirs(state_dir, exist_ok=True)
    report_path = os.path.join(state_dir, f"r5-report-{state.session_id}.md")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(report))
        state.r5_report_path = report_path
        logger.info(f"R5 报告写入: {report_path}")
        print(f"\n📄 R5 报告: {report_path}")
    except Exception as e:
        logger.error(f"R5 报告写盘失败: {e}")


# ============================================================
# R5-A 静态调用方影响 (0 LLM)
# ============================================================

def _r5_a_callgraph_impact(verified, state, project_root, modules_by_name):
    """对每个 verified bug,提取 git diff 改动的 symbol,grep 全项目调用方。"""
    lines = ["", "## R5-A 调用方影响(静态分析)", ""]

    for bug in verified:
        bug_id = bug["id"]
        bug_file = bug.get("file", "")

        symbols = _extract_modified_symbols(state, project_root, bug)
        if not symbols:
            lines.append(f"### [{bug_id}] {bug_file}")
            lines.append("⚠️  未能从 worktree diff 中提取出修改的 symbol(可能是非函数级改动)")
            lines.append("")
            continue

        callers = _grep_callers(symbols, project_root, modules_by_name, exclude_file=bug_file)

        lines.append(f"### [{bug_id}] {bug_file}")
        lines.append(f"修改的 symbol: `{', '.join(symbols)}`")

        if not callers:
            lines.append("调用方: **无**(internal/private 改动 — 影响范围限本文件)")
            lines.append("")
            continue

        # 按模块分组
        by_module = {}
        for c in callers:
            by_module.setdefault(c["module"], []).append(c)

        bug_module = bug.get("module", "?")
        same_mod = [c for c in callers if c["module"] == bug_module]
        cross_mod = [c for c in callers if c["module"] != bug_module]

        lines.append(f"调用方共 {len(callers)} 处:**同模块 {len(same_mod)}** / **跨模块 {len(cross_mod)}**")

        if cross_mod:
            lines.append("")
            lines.append("**⚠️ 跨模块调用方(可能受影响):**")
            for c in cross_mod[:15]:
                recent = "🕐" if c["recent"] else "  "
                lines.append(f"- {recent} `{c['file']}:{c['line']}` ({c['module']}) → `{c['symbol']}`")
        if same_mod:
            lines.append("")
            lines.append("同模块调用方:")
            for c in same_mod[:10]:
                recent = "🕐" if c["recent"] else "  "
                lines.append(f"- {recent} `{c['file']}:{c['line']}` → `{c['symbol']}`")

        if any(c["recent"] for c in callers):
            lines.append("")
            lines.append("(🕐 = 最近 30 天有改动 — 优先 review)")
        lines.append("")

    return lines


def _extract_modified_symbols(state, project_root, bug):
    """用 git diff 解析 worktree 上的改动,提取被修改的函数/方法名。

    依赖 state.worktrees[module]['branch']。失败则返回空,不抛异常。
    """
    bug_file = bug.get("file", "")
    module = bug.get("module")
    if not bug_file or not module:
        return []

    wt_info = state.worktrees.get(module)
    if not wt_info:
        return []
    branch = wt_info.get("branch") if isinstance(wt_info, dict) else None
    if not branch:
        return []

    # 找该分支相对 main 的 diff,带函数上下文
    try:
        result = subprocess.run(
            ["git", "diff", "-W", "--no-color", f"main...{branch}", "--", bug_file],
            cwd=project_root, capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return []
    diff = result.stdout
    if not diff:
        return []

    # @@ 行后面跟函数签名,各语言提取
    symbols = set()
    # 各语言的函数定义模式
    patterns = [
        # go: func (X) FooBar(  / func FooBar(
        re.compile(r"\bfunc\s+(?:\([^)]+\)\s+)?(\w+)\s*\("),
        # ts/js: function fooBar  / async fooBar  / fooBar(): T {
        re.compile(r"\bfunction\s+(\w+)\s*\("),
        re.compile(r"\basync\s+(\w+)\s*\("),
        # python: def foo
        re.compile(r"\bdef\s+(\w+)\s*\("),
        # swift: func foo
        re.compile(r"\bfunc\s+(\w+)\s*[\(<]"),
        # ts class method: methodName(args): T {  ; methodName(args) {
        re.compile(r"^\s*(?:public|private|protected|static|async)?\s*(\w+)\s*\([^)]*\)\s*[:{]", re.M),
    ]

    for line in diff.splitlines():
        if line.startswith("@@"):
            # hunk header 的尾部是函数上下文
            ctx_match = re.match(r"@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@\s*(.*)", line)
            if not ctx_match:
                continue
            ctx = ctx_match.group(1)
            for p in patterns:
                m = p.search(ctx)
                if m:
                    name = m.group(1)
                    # 过滤明显的关键字
                    if name not in {"if", "for", "while", "return", "switch", "func", "function"}:
                        symbols.add(name)

    # 限制 5 个,优先长名(短的 ambiguity 太高)
    sorted_syms = sorted(symbols, key=lambda s: (-len(s), s))[:5]
    return sorted_syms


def _grep_callers(symbols, project_root, modules_by_name, exclude_file=None):
    """grep 各 symbol 在项目中的调用位置,带模块归属。"""
    if not symbols:
        return []

    results = []
    for sym in symbols:
        # 用单词边界,避免命中子串
        try:
            cmd = ["grep", "-rn", "-w"]
            for ext in CODE_EXTENSIONS:
                cmd += ["--include", f"*{ext}"]
            cmd += [sym, project_root]
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
        except Exception:
            continue

        for raw_line in out.stdout.splitlines():
            # 格式: /abs/path:lineno:content
            try:
                file_abs, lineno, _content = raw_line.split(":", 2)
            except ValueError:
                continue

            try:
                rel = os.path.relpath(file_abs, project_root)
            except ValueError:
                continue

            # 跳过排除目录
            if any(seg in rel for seg in EXCLUDE_DIR_SEGMENTS):
                continue
            # 跳过 bug 文件自身
            if exclude_file and rel == exclude_file:
                continue
            # 跳过测试文件(降低噪声)
            if "_test." in rel or rel.endswith("_test.go") or ".test." in rel or ".spec." in rel:
                continue

            # 找它属于哪个模块
            module = "(unknown)"
            best_match_len = 0
            for mn, m in modules_by_name.items():
                src_dir = getattr(m, "src_dir", "") or ""
                src_dir = src_dir.rstrip("/")
                if src_dir and rel.startswith(src_dir + "/"):
                    if len(src_dir) > best_match_len:
                        module = mn
                        best_match_len = len(src_dir)

            results.append({
                "symbol": sym,
                "file": rel,
                "line": lineno,
                "module": module,
                "recent": False,  # 下面统一查
            })

    # 去重 + 标记 recently_changed
    seen = set()
    unique = []
    for c in results:
        key = (c["file"], c["line"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    # 批量查 git log 标 recent(只查 distinct files,避免重复)
    files = list({c["file"] for c in unique})
    recent_files = _batch_recently_changed(files, project_root, days=30)
    for c in unique:
        c["recent"] = c["file"] in recent_files

    # 限 50 条避免报告过长
    return unique[:50]


def _batch_recently_changed(files, project_root, days=30):
    """一次性查多个文件是否最近改过。返回 set of recent files。"""
    if not files:
        return set()
    recent = set()
    # 拆批跑(参数过长会失败)
    BATCH = 30
    for i in range(0, len(files), BATCH):
        batch = files[i:i+BATCH]
        try:
            out = subprocess.run(
                ["git", "log", f"--since={days} days ago", "--name-only", "--format=", "--"] + batch,
                cwd=project_root, capture_output=True, text=True, timeout=15,
            )
        except Exception:
            continue
        for line in out.stdout.splitlines():
            line = line.strip()
            if line and line in files:
                recent.add(line)
    return recent


# ============================================================
# R5-B 同模式候选检测 (微 LLM × 候选)
# ============================================================

def _r5_b_similar_patterns(verified, state, project_root, modules_by_name, deadline):
    """每个 verified bug,在同目录找 5 个候选,逐个独立 LLM 判定。"""
    from lib.claude import call_claude_bare
    from lib.prompts.r5 import SIMILAR_PATTERN_PROMPT
    from lib.schemas.r5 import SIMILAR_PATTERN_SCHEMA

    lines = ["", "## R5-B 同模式候选检测", ""]
    any_output = False

    for bug in verified:
        if time.time() >= deadline:
            lines.append("⏱ 预算耗尽,跳过剩余 verified bug")
            break

        bug_id = bug["id"]
        candidates = _find_pattern_candidates(bug, project_root)
        if not candidates:
            continue

        bug_lines = [f"### [{bug_id}] {bug.get('description', '')[:80]}"]
        bug_lines.append(f"扫 {len(candidates)} 个同目录候选:")

        # 候选并行 LLM 调用(失败一个不影响其他)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {}
            for cand in candidates:
                if time.time() >= deadline:
                    break
                fut = pool.submit(
                    _r5b_eval_candidate,
                    bug, cand,
                    call_claude_bare, SIMILAR_PATTERN_PROMPT, SIMILAR_PATTERN_SCHEMA,
                )
                futures[fut] = cand

            for fut in as_completed(futures):
                cand = futures[fut]
                try:
                    verdict, reason = fut.result()
                except Exception as e:
                    verdict, reason = "uncertain", f"调用失败: {e}"
                mark = {"yes": "⚠️", "no": "✓ ", "uncertain": "? "}.get(verdict, "? ")
                bug_lines.append(f"- {mark} `{cand['file']}` — **{verdict}** {reason[:120]}")

        # 至少有 yes 或 uncertain 才输出(避免噪声)
        has_signal = any("⚠️" in l or "? " in l for l in bug_lines)
        if has_signal:
            lines.extend(bug_lines)
            lines.append("")
            any_output = True

    if not any_output:
        lines.append("所有 verified bug 在同模块未发现可疑同模式候选。")
        lines.append("")
    return lines


def _r5b_eval_candidate(bug, cand, call_claude_bare, prompt_tmpl, schema):
    """单候选独立 LLM 判定。返回 (verdict, reason)。"""
    prompt = prompt_tmpl.format(
        bug_id=bug["id"],
        bug_category=bug.get("category", "?"),
        bug_file=bug.get("file", "?"),
        bug_line=bug.get("line", "?"),
        bug_description=bug.get("description", "")[:400],
        candidate_file=cand["file"],
        candidate_snippet=cand["snippet"],
    )
    result = call_claude_bare(
        prompt=prompt,
        model="opus",
        tools="",
        output_schema=schema,
        max_turns=3,
        timeout=30,
    )
    if isinstance(result, dict):
        return result.get("verdict", "uncertain"), result.get("reason", "")
    return "uncertain", "返回格式异常"


def _find_pattern_candidates(bug, project_root):
    """启发式找候选位置:同目录下其他同语言文件的前 60 行。"""
    bug_file = bug.get("file", "")
    if not bug_file:
        return []

    abs_bug = os.path.join(project_root, bug_file)
    dir_path = os.path.dirname(abs_bug)
    ext = os.path.splitext(bug_file)[1]
    if not ext or not os.path.isdir(dir_path):
        return []

    candidates = []
    try:
        for fname in sorted(os.listdir(dir_path)):
            if not fname.endswith(ext):
                continue
            cand_abs = os.path.join(dir_path, fname)
            if cand_abs == abs_bug:
                continue
            # 跳过测试文件
            if "_test." in fname or ".test." in fname or ".spec." in fname:
                continue
            try:
                with open(cand_abs, "r", encoding="utf-8", errors="ignore") as f:
                    head = "".join(f.readlines()[:60])
            except Exception:
                continue
            rel = os.path.relpath(cand_abs, project_root)
            candidates.append({
                "file": rel,
                "line": 1,
                "snippet": head[:3000],  # 限长防 prompt 爆炸
            })
            if len(candidates) >= R5B_MAX_CANDIDATES_PER_BUG:
                break
    except OSError:
        pass
    return candidates


# ============================================================
# R5-C adversarial 测试输入 (微 LLM × verified bug)
# ============================================================

def _r5_c_adversarial_inputs(verified, state, project_root, deadline):
    """每个 verified bug,读其红绿测试文件,opus 输出 3 个绕过输入。"""
    from lib.claude import call_claude_bare
    from lib.prompts.r5 import ADVERSARIAL_PROMPT
    from lib.schemas.r5 import ADVERSARIAL_SCHEMA

    lines = ["", "## R5-C adversarial 测试输入", ""]
    any_output = False

    for bug in verified:
        if time.time() >= deadline:
            lines.append("⏱ 预算耗尽,跳过剩余 verified bug")
            break

        bug_id = bug["id"]
        test_file = state.get_result_field(bug_id, "test_file", "")
        if not test_file:
            continue

        # 测试文件可能在 worktree(若已合并到 main) 或 main
        test_content = _read_test_file(test_file, bug, state, project_root)
        if not test_content:
            lines.append(f"### [{bug_id}] 跳过:测试文件未找到 `{test_file}`")
            lines.append("")
            continue

        try:
            result = call_claude_bare(
                prompt=ADVERSARIAL_PROMPT.format(
                    bug_id=bug_id,
                    bug_file=bug.get("file", "?"),
                    bug_line=bug.get("line", "?"),
                    bug_description=bug.get("description", "")[:400],
                    test_file=test_file,
                    test_content=test_content[:5000],
                ),
                model="opus",
                tools="",
                output_schema=ADVERSARIAL_SCHEMA,
                max_turns=3,
                timeout=30,
            )
        except Exception as e:
            lines.append(f"### [{bug_id}] adversarial 调用失败: {e}")
            lines.append("")
            continue

        inputs = []
        if isinstance(result, dict):
            inputs = result.get("adversarial_inputs", [])
        if not inputs:
            continue

        lines.append(f"### [{bug_id}] {bug.get('description', '')[:80]}")
        lines.append(f"测试文件: `{test_file}`")
        lines.append("")
        for i, inp in enumerate(inputs[:3], 1):
            lines.append(f"**{i}. {inp.get('label', '?')}**")
            lines.append(f"```")
            lines.append(inp.get("input", ""))
            lines.append(f"```")
            lines.append(f"绕过原因:{inp.get('why_bypass', '?')}")
            lines.append("")
        any_output = True

    if not any_output:
        lines.append("所有 verified bug 的测试文件均未生成 adversarial 输入(或文件缺失)。")
        lines.append("")
    return lines


def _read_test_file(test_file, bug, state, project_root):
    """尝试从 main / worktree 找到测试文件并读取。"""
    candidates = []

    # 1. main 项目根
    candidates.append(os.path.join(project_root, test_file))

    # 2. worktree(测试可能还在 worktree 没合并)
    wt_info = state.worktrees.get(bug.get("module"))
    if wt_info:
        wt_path = wt_info.get("path") if isinstance(wt_info, dict) else None
        if wt_path:
            candidates.append(os.path.join(wt_path, test_file))
            # test_file 可能是相对模块根的路径,试拼前缀
            module = bug.get("module", "")
            if module:
                candidates.append(os.path.join(wt_path, module, test_file))

    # 3. 直接是绝对路径
    candidates.append(test_file)

    for path in candidates:
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read()
            except Exception:
                continue
    return ""
