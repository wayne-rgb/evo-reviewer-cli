"""evo-review CLI 路由 — argparse 命令分发"""

import argparse
import logging
import os
import subprocess
import sys
import time

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("evo-review")


def _detect_project_root():
    """检测项目根目录（向上查找 .git）"""
    cwd = os.getcwd()
    path = cwd
    while path != "/":
        if os.path.isdir(os.path.join(path, ".git")):
            return path
        path = os.path.dirname(path)
    # 没找到 .git，用 cwd
    return cwd


def cmd_review(args):
    """执行 /review 流程"""
    from lib.state import ReviewState
    from lib.steps.bootstrap import run_bootstrap
    from lib.steps.scope import determine_scope
    from lib.steps.scan import run_scan
    from lib.steps.organize import run_organize
    from lib.steps.confirm import run_confirm
    from lib.steps.verify import run_verify
    from lib.steps.merge import run_merge
    from lib.steps.infra_c1 import run_infra_c1
    from lib.steps.infra_c2 import run_infra_c2
    from lib.report import generate_final_report

    project_root = _detect_project_root()
    logger.info(f"项目根目录：{project_root}")

    # 创建会话
    state = ReviewState.new_session("review", args.paths or [], project_root)

    start_time = time.time()

    # 阶段 0-1：Bootstrap
    print("\n=== 阶段 0：前置检查 ===\n")
    run_bootstrap(state, project_root)

    # 阶段 0-2：确定范围
    modules, scope_paths = determine_scope(state, project_root, args.paths)
    if not modules:
        print("未找到受影响的模块，退出。")
        return 1

    print(f"审查模块：{', '.join(m.name for m in modules)}")
    print(f"审查范围：{', '.join(scope_paths)}")

    # 阶段 0-3：违规趋势
    _load_trend(state, project_root)

    # 阶段 1：扫描
    print("\n=== 阶段 1：代码扫描（opus） ===\n")
    findings = run_scan(state, project_root, modules)
    print(f"发现 {len(findings)} 个潜在问题")

    if not findings:
        print("未发现问题，审查完成。")
        return 0

    # 阶段 1.5：归类
    print("\n=== 阶段 1.5：盲区归类 ===\n")
    gaps = run_organize(state, project_root)
    print(f"归类为 {len(gaps)} 个盲区")

    # 阶段 2：确认
    print("\n=== 阶段 2：确认清单 ===\n")
    confirmed_ids = run_confirm(state, project_root)
    if not confirmed_ids:
        print("已取消。")
        return 0

    # 保存状态（断点恢复用）
    state.save(state.state_file(project_root))

    # 阶段 A：红绿验证
    print("\n=== 阶段 A：红绿验证 ===\n")
    modules_by_name = {m.name: m for m in modules}
    run_verify(state, project_root, confirmed_ids, modules_by_name)
    state.save(state.state_file(project_root))

    # 阶段 B：确认合并
    print("\n=== 阶段 B：验证结果 ===\n")
    merged = run_merge(state, project_root)

    # 阶段 C-1
    if merged:
        print("\n=== 阶段 C-1：gate 规则 + helper ===\n")
        run_infra_c1(state, project_root)
        state.save(state.state_file(project_root))

    # 阶段 C-2
    if merged:
        print("\n=== 阶段 C-2：文档 + 趋势治理 ===\n")
        run_infra_c2(state, project_root)
        state.save(state.state_file(project_root))

    # 阶段 D：最终报告
    print("\n=== 阶段 D：最终报告 ===\n")
    report = generate_final_report(state)
    print(report)

    # 自检
    _self_check(state)

    elapsed = (time.time() - start_time) / 60
    print(f"\n总耗时：{elapsed:.1f} 分钟")

    state.advance("done")
    state.save(state.state_file(project_root))
    return 0


def cmd_deep(args):
    """执行 /deep 流程"""
    from lib.state import ReviewState
    from lib.steps.bootstrap import run_bootstrap
    from lib.steps.scope import determine_scope
    from lib.steps.scan import run_scan, run_deep_r2
    from lib.steps.organize import run_organize
    from lib.steps.confirm import run_confirm
    from lib.steps.verify import run_verify
    from lib.steps.cross_validate import run_cross_validate
    from lib.steps.merge import run_merge
    from lib.steps.infra_c1 import run_infra_c1
    from lib.steps.infra_c2 import run_infra_c2
    from lib.report import generate_final_report

    project_root = _detect_project_root()
    state = ReviewState.new_session("deep", args.modules or [], project_root)
    start_time = time.time()

    # 阶段 0
    print("\n=== 阶段 0：前置检查 ===\n")
    run_bootstrap(state, project_root)

    if args.modules:
        modules, scope_paths = determine_scope(state, project_root, args.modules)
    else:
        # /deep 默认全模块
        from lib.config import get_modules
        modules = get_modules(project_root)
        scope_paths = [m.src_dir for m in modules]
        state.modules = [m.name for m in modules]
        state.scope = scope_paths

    if not modules:
        print("未找到模块，退出。")
        return 1

    print(f"深度审查模块：{', '.join(m.name for m in modules)}")

    _load_trend(state, project_root)

    # R1：标准扫描
    print("\n=== R1：标准扫描（opus） ===\n")
    r1_findings = run_scan(state, project_root, modules)
    print(f"R1 发现 {len(r1_findings)} 个问题")

    # R2：深度扫描
    print("\n=== R2：深度扫描（opus） ===\n")
    r2_findings = run_deep_r2(state, project_root, modules, r1_findings)
    print(f"R2 新增 {len(r2_findings)} 个问题")
    print(f"总计 {len(state.findings)} 个问题")

    if not state.findings:
        print("未发现问题，审查完成。")
        return 0

    # 归类 + 确认
    print("\n=== 盲区归类 ===\n")
    gaps = run_organize(state, project_root)

    print("\n=== 确认清单 ===\n")
    confirmed_ids = run_confirm(state, project_root)
    if not confirmed_ids:
        return 0

    state.save(state.state_file(project_root))

    # R4：红绿验证
    print("\n=== R4：红绿验证 ===\n")
    modules_by_name = {m.name: m for m in modules}
    run_verify(state, project_root, confirmed_ids, modules_by_name)
    state.save(state.state_file(project_root))

    # 合并
    print("\n=== 验证结果 ===\n")
    merged = run_merge(state, project_root)

    # R5：轻量化交叉检验
    print("\n=== R5：交叉检验（轻量） ===\n")
    run_cross_validate(state, project_root, modules_by_name)
    state.save(state.state_file(project_root))

    # Phase C
    if merged:
        print("\n=== Phase C-1 ===\n")
        run_infra_c1(state, project_root)
        print("\n=== Phase C-2 ===\n")
        run_infra_c2(state, project_root)
        state.save(state.state_file(project_root))

    # 最终报告
    print("\n=== 最终报告 ===\n")
    report = generate_final_report(state)
    print(report)

    _self_check(state)

    elapsed = (time.time() - start_time) / 60
    print(f"\n总耗时：{elapsed:.1f} 分钟")

    state.advance("done")
    state.save(state.state_file(project_root))
    return 0


def cmd_test_check(args):
    """执行 /test-check"""
    from lib.steps.test_check import run_test_check

    project_root = _detect_project_root()
    run_test_check(args.path, project_root)
    return 0


def cmd_ci(args):
    """执行 /ci"""
    from lib.steps.ci import run_ci

    project_root = _detect_project_root()
    ok = run_ci(project_root)
    return 0 if ok else 1


def cmd_resume(args):
    """中断恢复"""
    from lib.state import ReviewState

    project_root = _detect_project_root()
    state_path = ReviewState.latest_state_path(project_root)

    if not state_path:
        print("未找到可恢复的会话。")
        return 1

    state = ReviewState.load(state_path)
    print(f"恢复会话：{state.session_id}")
    print(f"命令：{state.command}")
    print(f"阶段：{state.phase}")
    print(f"模块：{', '.join(state.modules)}")
    print(f"发现：{len(state.findings)} 个")
    print(f"结果：{len(state.results)} 个")

    # 从断点继续
    from lib.config import get_modules

    all_modules = get_modules(project_root)
    modules_by_name = {m.name: m for m in all_modules}
    target_modules = [modules_by_name[n] for n in state.modules if n in modules_by_name]

    phase = state.phase

    if phase in ("bootstrap", "scope", "scan", "organize", "confirm"):
        # 重新开始（这些阶段很快）
        print("阶段较早，建议重新执行。")
        return 1

    if phase == "verify":
        print("从红绿验证阶段继续...")
        # 找出未验证的 + 之前失败值得重试的 bug
        confirmed_ids = [
            f["id"] for f in state.findings
            if f["id"] not in state.results
            or state.get_result_status(f["id"]) == "fix_failed"
        ]
        if confirmed_ids:
            from lib.steps.verify import run_verify
            run_verify(state, project_root, confirmed_ids, modules_by_name)

    if phase in ("verify", "merge") and not state.phase_c1_done:
        from lib.steps.merge import run_merge
        from lib.steps.infra_c1 import run_infra_c1
        from lib.steps.infra_c2 import run_infra_c2

        print("\n=== 验证结果 ===\n")
        merged = run_merge(state, project_root)
        if merged:
            print("\n=== Phase C-1 ===\n")
            run_infra_c1(state, project_root)
            print("\n=== Phase C-2 ===\n")
            run_infra_c2(state, project_root)

    # 最终报告
    from lib.report import generate_final_report

    print("\n=== 最终报告 ===\n")
    print(generate_final_report(state))

    state.advance("done")
    state.save(state.state_file(project_root))
    return 0


def _load_trend(state, project_root):
    """加载违规趋势：高频规则 + 问题热点文件"""
    import subprocess

    try:
        result = subprocess.run(
            ["bash", "scripts/test-governance-gate.sh", "trend"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout
        in_rules = False
        in_files = False

        for line in output.split("\n"):
            # 检测区块
            if "按规则统计" in line:
                in_rules = True
                in_files = False
                continue
            if "按文件统计" in line:
                in_rules = False
                in_files = True
                continue
            if "建议" in line or "===" in line:
                in_rules = False
                in_files = False
                continue

            parts = line.split()
            if len(parts) >= 2:
                try:
                    count = int(parts[0])
                except ValueError:
                    continue

                if in_rules and count >= 10:
                    state.high_freq_rules.append(parts[1])
                elif in_files and count >= 3:
                    state.hot_files.append(parts[1])
    except Exception as e:
        logger.debug(f"加载违规趋势失败（非关键）: {e}")


def _self_check(state):
    """最终自检（代码强制）"""
    issues = []

    # 1. 所有 finding 都有验证结果
    for f in state.findings:
        if f["id"] not in state.results:
            issues.append(f"bug {f['id']} 没有验证结果")

    # 2. 所有 worktree 已合并（无残留）
    for mod_name, wt_info in state.worktrees.items():
        wt_path = wt_info.get("path", "") if isinstance(wt_info, dict) else ""
        if wt_path and os.path.exists(wt_path):
            issues.append(f"worktree 未清理: {mod_name} ({wt_path})")

    # 3. Phase C 完成状态
    has_verified = any(
        state.get_result_status(fid) == "verified"
        for fid in state.results
    )
    if has_verified:
        if not state.phase_c1_done:
            issues.append("有已验证修复但 Phase C-1 未完成")
        if not state.phase_c2_done:
            issues.append("有已验证修复但 Phase C-2 未完成")

    # 4. 无未提交改动
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
            cwd=_detect_project_root(),
        )
        if result.stdout.strip():
            issues.append("存在未提交的改动")
    except Exception:
        pass

    if issues:
        print("\n⚠️ 自检发现问题：")
        for i in issues:
            print(f"  - {i}")
    else:
        print("\n✅ 自检通过")


def main():
    parser = argparse.ArgumentParser(
        prog="evo-cli",
        description="自进化代码审查 CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # review
    p_review = subparsers.add_parser("review", help="范围审查（最近 5 commit）")
    p_review.add_argument("paths", nargs="*", help="指定审查路径")
    p_review.set_defaults(func=cmd_review)

    # deep
    p_deep = subparsers.add_parser("deep", help="全模块深度审查")
    p_deep.add_argument("modules", nargs="*", help="指定模块")
    p_deep.set_defaults(func=cmd_deep)

    # test-check
    p_tc = subparsers.add_parser("test-check", help="测试维度检查")
    p_tc.add_argument("path", help="测试文件路径")
    p_tc.set_defaults(func=cmd_test_check)

    # ci
    p_ci = subparsers.add_parser("ci", help="CI 验证")
    p_ci.set_defaults(func=cmd_ci)

    # resume
    p_resume = subparsers.add_parser("resume", help="中断恢复")
    p_resume.set_defaults(func=cmd_resume)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)
