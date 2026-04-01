"""evo-review CLI 路由 — argparse 命令分发

支持 --until 分阶段执行：
  evo-cli review --until scan        扫描+归类后停，输出确认清单
  evo-cli resume --until verify      红绿验证后停
  evo-cli resume --confirmed F1,F2   跳过交互确认，用指定 ID 继续
  evo-cli resume                     跑完剩余阶段
"""

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

# --until 可用的阶段名（review 和 deep 共享）
VALID_STAGES = ("scan", "confirm", "evaluate", "verify", "done")


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


def _should_stop(until, current_stage):
    """检查是否应该在当前阶段停止。"""
    if not until:
        return False
    return until == current_stage


def _print_scan_summary(state, include_confirm_report=False):
    """扫描完成后的摘要。

    include_confirm_report: 为 True 时输出完整确认清单（--until scan 时使用，
    因为不会再进入 confirm 阶段，需要在这里输出供 Claude 展示）。
    """
    print(f"\n{'='*60}")
    print(f"[STAGE_COMPLETE] scan")
    print(f"扫描完成：{len(state.findings)} 个问题，{len(state.gaps)} 个盲区")
    print(f"{'='*60}\n")

    if include_confirm_report and state.findings:
        from lib.report import generate_confirm_report
        report = generate_confirm_report(state.gaps, state.findings)
        print(report)
        print(f"\n所有 finding ID：{', '.join(f['id'] for f in state.findings)}")


def _print_verify_summary(state):
    """验证完成后的摘要。"""
    def _count_status(status_name):
        return sum(1 for r in state.results.values()
                   if (r.get("status") if isinstance(r, dict) else getattr(r, "status", "")) == status_name)

    verified = _count_status("verified")
    hallucination = _count_status("hallucination")
    fix_failed = _count_status("fix_failed")
    eval_skipped = _count_status("eval_skipped")
    other = len(state.results) - verified - hallucination - fix_failed - eval_skipped

    print(f"\n{'='*60}")
    print(f"[STAGE_COMPLETE] verify")
    parts = [f"{verified} verified", f"{hallucination} hallucination", f"{fix_failed} fix_failed"]
    if eval_skipped:
        parts.append(f"{eval_skipped} eval_skipped")
    if other:
        parts.append(f"{other} other")
    print(f"验证完成：{' / '.join(parts)}")
    print(f"{'='*60}")


def _print_evaluate_summary(state):
    """R3 深度评估完成后的摘要，展示每个 finding 的评估结果。

    从 evaluate.py 的 run_evaluate 返回后，state.results 中包含 eval_skipped 的 findings，
    而 evaluate 内部的 results dict 包含所有评估详情。我们需要从 state 的 evaluate_details
    或 findings-all.json 获取完整评估信息。

    当前实现：eval_skipped 的有详情（存入 state.results），must_fix/verify 的只有 verdict。
    为展示 must_fix/verify 的详情，从 evaluate_details 属性读取（如果有）。
    """
    # 收集所有评估结果
    eval_skipped_ids = set()
    eval_details = {}
    for fid, result in state.results.items():
        r = result if isinstance(result, dict) else {"status": getattr(result, "status", "")}
        if r.get("status") == "eval_skipped":
            eval_skipped_ids.add(fid)
            eval_details[fid] = r

    # evaluate_details 属性存储了完整的 R3 评估结果（包括 must_fix/verify）
    full_details = getattr(state, "evaluate_details", {})
    for fid, detail in full_details.items():
        if fid not in eval_details:
            eval_details[fid] = detail

    # 分类统计
    all_ids = {f["id"] for f in state.findings}
    to_verify_ids = all_ids - eval_skipped_ids
    must_fix_ids = {fid for fid, d in full_details.items() if d.get("verdict") == "must_fix"}
    verify_ids = to_verify_ids - must_fix_ids

    print(f"\n{'='*60}")
    print(f"[STAGE_COMPLETE] evaluate")
    print(f"R3 深度评估完成：{len(must_fix_ids)} must_fix / {len(verify_ids)} verify / {len(eval_skipped_ids)} skip")
    print(f"{'='*60}")

    # 按判定分组展示
    def _print_finding(f, verdict, detail):
        fid = f["id"]
        severity = f.get("severity", "?")
        file_loc = f"{f.get('file', '?')}:{f.get('line', '?')}"
        actual_sev = detail.get("actual_severity", severity) if detail else severity
        trigger = detail.get("trigger_probability", "?") if detail else "?"
        reason = detail.get("reason", "") if detail else ""

        print(f"  [{fid}] **{verdict}** ({severity}→{actual_sev}, 触发={trigger})")
        print(f"    {file_loc}")
        if reason:
            print(f"    理由：{reason}")
        print()

    if must_fix_ids:
        print("\n### must_fix（必须修复，直接进入 R4）\n")
        for f in state.findings:
            if f["id"] in must_fix_ids:
                _print_finding(f, "must_fix", eval_details.get(f["id"]))

    if verify_ids:
        print("\n### verify（需红绿验证确认）\n")
        for f in state.findings:
            if f["id"] in verify_ids:
                _print_finding(f, "verify", eval_details.get(f["id"]))

    if eval_skipped_ids:
        print("\n### skip（不进入 R4，已跳过）\n")
        for f in state.findings:
            if f["id"] in eval_skipped_ids:
                _print_finding(f, "skip", eval_details.get(f["id"]))

    print(f"共 {len(to_verify_ids)} 个 finding 将进入 R4 红绿验证（{len(eval_skipped_ids)} 个已跳过）。")


def _run_finalize(state, project_root):
    """merge + infra + report + self-check（review 和 deep 共用的收尾流程）。

    每个子阶段推进 phase 并保存，确保崩溃后 resume 不重复已完成的步骤。
    """
    from lib.steps.merge import run_merge
    from lib.steps.infra_c1 import run_infra_c1
    from lib.steps.infra_c2 import run_infra_c2
    from lib.report import generate_final_report

    # merge（仅未执行时运行）
    if state.phase not in ("merge", "infra_c1", "infra_c2", "report", "done"):
        print("\n=== 阶段 B：验证结果 ===\n")
        merged = run_merge(state, project_root)
        state.advance("merge")
        state.save(state.state_file(project_root))
    else:
        # 已经跑过 merge，根据 phase_c1_done 判断是否有 merged 内容
        merged = state.phase_c1_done or any(
            state.get_result_status(f["id"]) == "verified"
            for f in state.findings
        )

    # infra_c1
    if merged and not state.phase_c1_done:
        print("\n=== 阶段 C-1：gate 规则 + helper ===\n")
        run_infra_c1(state, project_root)
        state.advance("infra_c1")
        state.save(state.state_file(project_root))

    # infra_c2
    if merged and not state.phase_c2_done:
        print("\n=== 阶段 C-2：文档 + 趋势治理 ===\n")
        run_infra_c2(state, project_root)
        state.advance("infra_c2")
        state.save(state.state_file(project_root))

    # 统一 push（merge + C1 + C2 的所有 commit 一次推送）
    if merged:
        from lib.git import git_push
        try:
            git_push(cwd=project_root)
            logger.info("统一推送完成")
        except Exception as e:
            logger.error(f"推送失败: {e}")

    # report
    print("\n=== 阶段 D：最终报告 ===\n")
    report = generate_final_report(state)
    print(report)

    _self_check(state)

    # 评估持久化：记录本次 review 的摘要到 history.jsonl
    from lib.steps.history import save_session_summary
    duration = _estimate_duration(state.session_id)
    save_session_summary(state, project_root, duration_minutes=duration)

    state.advance("done")
    state.save(state.state_file(project_root))

    print(f"\n{'='*60}")
    print(f"[STAGE_COMPLETE] done")
    print(f"{'='*60}")


# ==================== review ====================

def cmd_review(args):
    """执行 /review 流程。支持 --until 分阶段停止。"""
    from lib.state import ReviewState
    from lib.steps.bootstrap import run_bootstrap
    from lib.steps.scope import determine_scope
    from lib.steps.scan import run_scan
    from lib.steps.organize import run_organize
    from lib.steps.confirm import run_confirm
    from lib.steps.verify import run_verify

    until = getattr(args, "until", None)
    project_root = _detect_project_root()
    logger.info(f"项目根目录：{project_root}")

    # 创建会话
    state = ReviewState.new_session("review", args.paths or [], project_root)
    start_time = time.time()

    # --- 阶段 0：bootstrap + scope + trend ---
    print("\n=== 阶段 0：前置检查 ===\n")
    run_bootstrap(state, project_root)

    modules, scope_paths = determine_scope(state, project_root, args.paths)
    if not modules:
        print("未找到受影响的模块，退出。")
        return 1

    print(f"审查模块：{', '.join(m.name for m in modules)}")
    print(f"审查范围：{', '.join(scope_paths)}")

    _load_trend(state, project_root)

    # --- 阶段 1：扫描 + 归类 ---
    print("\n=== 阶段 1：代码扫描（opus） ===\n")
    findings = run_scan(state, project_root, modules)
    print(f"发现 {len(findings)} 个潜在问题")
    state.save(state.state_file(project_root))  # 扫描后立即持久化

    if not findings:
        print("未发现问题，审查完成。")
        # 0 findings 也记录到 history（"干净扫描"有信息量）
        from lib.steps.history import save_session_summary
        duration = (time.time() - start_time) / 60
        save_session_summary(state, project_root, duration_minutes=duration)
        state.advance("done")
        state.save(state.state_file(project_root))
        return 0

    print("\n=== 阶段 1.5：盲区归类 ===\n")
    try:
        gaps = run_organize(state, project_root)
    except Exception as e:
        logger.error("盲区归类失败，降级为按模块+类别自动分组: %s", e)
        from collections import defaultdict
        groups = defaultdict(list)
        for f in state.findings:
            key = (f.get("module", "unknown"), f.get("category", "unknown"))
            groups[key].append(f)
        state.gaps = [
            {
                "id": f"G{i}",
                "module": mod,
                "gap_name": f"{mod} — {cat}（{len(findings)} 个发现）",
                "infra_plan": "待定",
                "evidence_finding_ids": [f["id"] for f in findings],
            }
            for i, ((mod, cat), findings) in enumerate(groups.items(), 1)
        ]
        gaps = state.gaps
    print(f"归类为 {len(gaps)} 个盲区")

    state.advance("organize")
    state.save(state.state_file(project_root))

    if _should_stop(until, "scan"):
        _print_scan_summary(state, include_confirm_report=True)
        elapsed = (time.time() - start_time) / 60
        print(f"\n耗时：{elapsed:.1f} 分钟（--until scan 停止）")
        return 0

    _print_scan_summary(state)

    # --- 阶段 2：确认 ---
    print("\n=== 阶段 2：确认清单 ===\n")
    confirmed_ids = run_confirm(state, project_root)
    if not confirmed_ids:
        print("已取消。")
        return 0

    state.advance("confirm")
    state.save(state.state_file(project_root))

    if _should_stop(until, "confirm"):
        elapsed = (time.time() - start_time) / 60
        print(f"\n耗时：{elapsed:.1f} 分钟（--until confirm 停止）")
        return 0

    # --- 阶段 A：红绿验证 ---
    print("\n=== 阶段 A：红绿验证 ===\n")
    modules_by_name = {m.name: m for m in modules}
    run_verify(state, project_root, confirmed_ids, modules_by_name)
    state.advance("verify")
    state.save(state.state_file(project_root))
    _print_verify_summary(state)

    if _should_stop(until, "verify"):
        elapsed = (time.time() - start_time) / 60
        print(f"\n耗时：{elapsed:.1f} 分钟（--until verify 停止）")
        return 0

    # --- 阶段 B-D：收尾 ---
    _run_finalize(state, project_root)

    elapsed = (time.time() - start_time) / 60
    print(f"\n总耗时：{elapsed:.1f} 分钟")
    return 0


# ==================== deep ====================

def cmd_deep(args):
    """执行 /deep 流程。支持 --until 分阶段停止。"""
    from lib.state import ReviewState
    from lib.steps.bootstrap import run_bootstrap
    from lib.steps.scope import determine_scope
    from lib.steps.scan import run_scan, run_deep_r2
    from lib.steps.organize import run_organize
    from lib.steps.confirm import run_confirm
    from lib.steps.verify import run_verify
    from lib.steps.cross_validate import run_cross_validate

    until = getattr(args, "until", None)
    project_root = _detect_project_root()
    state = ReviewState.new_session("deep", args.modules or [], project_root)
    start_time = time.time()

    # --- 阶段 0 ---
    print("\n=== 阶段 0：前置检查 ===\n")
    run_bootstrap(state, project_root)

    if args.modules:
        modules, scope_paths = determine_scope(state, project_root, args.modules)
    else:
        # /deep 默认全模块，但仍调用 determine_scope 获取 changed_by_module 和边界信息
        # 这样扫描时有具体变更文件可以聚焦，而非"请扫描整个目录"
        from lib.config import get_modules
        all_modules = get_modules(project_root)
        scoped_modules, scope_paths = determine_scope(state, project_root, [])
        # 模块列表强制为全部模块（deep 的核心语义）
        scoped_names = {m.name for m in scoped_modules}
        modules = all_modules
        # 补充无变更模块的 hot_files 作为扫描焦点
        for m in modules:
            if m.name not in scoped_names and m.name not in state.changed_by_module:
                hot = [f for f in state.hot_files if f.startswith(m.src_dir)] if state.hot_files else []
                if hot:
                    state.changed_by_module[m.name] = hot
        state.modules = [m.name for m in modules]
        if not scope_paths:
            scope_paths = [m.src_dir for m in modules]
        state.scope = scope_paths

    if not modules:
        print("未找到模块，退出。")
        return 1

    print(f"深度审查模块：{', '.join(m.name for m in modules)}")

    _load_trend(state, project_root)

    # --- R1 + R2：扫描 + 归类 ---
    print("\n=== R1：标准扫描（opus） ===\n")
    r1_findings = run_scan(state, project_root, modules)
    print(f"R1 发现 {len(r1_findings)} 个问题")
    state.save(state.state_file(project_root))  # R1 后立即持久化，防后续崩溃丢数据

    print("\n=== R2：深度扫描（opus） ===\n")
    r2_findings = run_deep_r2(state, project_root, modules, r1_findings)
    print(f"R2 新增 {len(r2_findings)} 个问题")
    print(f"总计 {len(state.findings)} 个问题")
    state.save(state.state_file(project_root))  # R2 后立即持久化

    if not state.findings:
        print("未发现问题，审查完成。")
        from lib.steps.history import save_session_summary
        duration = (time.time() - start_time) / 60
        save_session_summary(state, project_root, duration_minutes=duration)
        state.advance("done")
        state.save(state.state_file(project_root))
        return 0

    print("\n=== 盲区归类 ===\n")
    try:
        gaps = run_organize(state, project_root)
    except Exception as e:
        logger.error("盲区归类失败，降级为按模块+类别自动分组: %s", e)
        from collections import defaultdict
        groups = defaultdict(list)
        for f in state.findings:
            key = (f.get("module", "unknown"), f.get("category", "unknown"))
            groups[key].append(f)
        state.gaps = [
            {
                "id": f"G{i}",
                "module": mod,
                "gap_name": f"{mod} — {cat}（{len(findings)} 个发现）",
                "infra_plan": "待定",
                "evidence_finding_ids": [f["id"] for f in findings],
            }
            for i, ((mod, cat), findings) in enumerate(groups.items(), 1)
        ]

    state.advance("organize")
    state.save(state.state_file(project_root))

    if _should_stop(until, "scan"):
        _print_scan_summary(state, include_confirm_report=True)
        elapsed = (time.time() - start_time) / 60
        print(f"\n耗时：{elapsed:.1f} 分钟（--until scan 停止）")
        return 0

    _print_scan_summary(state)

    # --- 确认 ---
    print("\n=== 确认清单 ===\n")
    confirmed_ids = run_confirm(state, project_root)
    if not confirmed_ids:
        return 0

    state.advance("confirm")
    state.save(state.state_file(project_root))

    if _should_stop(until, "confirm"):
        elapsed = (time.time() - start_time) / 60
        print(f"\n耗时：{elapsed:.1f} 分钟（--until confirm 停止）")
        return 0

    # --- R3：深度评估 ---
    print("\n=== R3：深度评估（opus） ===\n")
    modules_by_name = {m.name: m for m in modules}
    from lib.steps.evaluate import run_evaluate
    ids_to_verify = run_evaluate(state, project_root, confirmed_ids, modules_by_name)
    state.advance("evaluate")
    state.save(state.state_file(project_root))

    if not ids_to_verify:
        print("深度评估认为所有发现均不值得红绿验证，跳到收尾。")
        state.advance("verify")
        state.save(state.state_file(project_root))
        _run_finalize(state, project_root)
        elapsed = (time.time() - start_time) / 60
        print(f"\n总耗时：{elapsed:.1f} 分钟")
        return 0

    if _should_stop(until, "evaluate"):
        _print_evaluate_summary(state)
        elapsed = (time.time() - start_time) / 60
        print(f"\n耗时：{elapsed:.1f} 分钟（--until evaluate 停止）")
        return 0

    # --- R4：红绿验证 ---
    print("\n=== R4：红绿验证 ===\n")
    run_verify(state, project_root, ids_to_verify, modules_by_name)
    state.advance("verify")
    state.save(state.state_file(project_root))

    # --- R5：交叉检验 ---
    print("\n=== R5：交叉检验（轻量） ===\n")
    run_cross_validate(state, project_root, modules_by_name)
    state.advance("cross_validate")
    state.save(state.state_file(project_root))
    _print_verify_summary(state)

    if _should_stop(until, "verify"):
        elapsed = (time.time() - start_time) / 60
        print(f"\n耗时：{elapsed:.1f} 分钟（--until verify 停止）")
        return 0

    # --- 收尾 ---
    _run_finalize(state, project_root)

    elapsed = (time.time() - start_time) / 60
    print(f"\n总耗时：{elapsed:.1f} 分钟")
    return 0


# ==================== resume ====================

def cmd_resume(args):
    """中断恢复。支持 --until 和 --confirmed。

    --confirmed F1,F2,F3  跳过交互确认，用指定 ID 继续
    --until verify        执行到指定阶段后停止
    """
    from lib.state import ReviewState

    until = getattr(args, "until", None)
    confirmed_arg = getattr(args, "confirmed", None)

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

    from lib.config import get_modules

    all_modules = get_modules(project_root)
    modules_by_name = {m.name: m for m in all_modules}
    target_modules = [modules_by_name[n] for n in state.modules if n in modules_by_name]

    phase = state.phase
    start_time = time.time()
    # confirmed_ids 在 organize/confirm 块中设置，跨块传递
    confirmed_ids = None

    # --- 阶段较早，建议重新执行 ---
    if phase in ("bootstrap", "scope", "scan"):
        print("阶段较早，建议重新执行。")
        return 1

    # --- organize 阶段：需要确认后继续 ---
    if phase == "organize":
        if _should_stop(until, "scan"):
            # 已经在 scan 之后了，显示摘要即可
            _print_scan_summary(state, include_confirm_report=True)
            return 0

        # 进入确认
        confirmed_ids = _parse_confirmed(confirmed_arg, state)
        if confirmed_ids is None:
            # 没有 --confirmed，交互确认
            from lib.steps.confirm import run_confirm
            confirmed_ids = run_confirm(state, project_root)
            if not confirmed_ids:
                print("已取消。")
                return 0

        state.advance("confirm")
        state.save(state.state_file(project_root))

        if _should_stop(until, "confirm"):
            return 0

        phase = "confirm"  # fall through

    # --- confirm 阶段：需要验证 ---
    if phase == "confirm":
        # 仅当未从 organize fall through 带入 confirmed_ids 时才重新计算
        if confirmed_ids is None:
            confirmed_ids = _parse_confirmed(confirmed_arg, state)
            if confirmed_ids is None:
                # 没有 --confirmed，用全部 findings
                confirmed_ids = [f["id"] for f in state.findings]

        # deep 命令：先执行 R3 深度评估，过滤低价值 findings
        if state.command == "deep":
            print("\n=== R3：深度评估（opus） ===\n")
            from lib.steps.evaluate import run_evaluate
            confirmed_ids = run_evaluate(state, project_root, confirmed_ids, modules_by_name)
            state.advance("evaluate")
            state.save(state.state_file(project_root))

            if not confirmed_ids:
                print("深度评估认为所有发现均不值得红绿验证，跳到收尾。")
                state.advance("verify")
                state.save(state.state_file(project_root))
                _run_finalize(state, project_root)
                elapsed = (time.time() - start_time) / 60
                print(f"\n总耗时：{elapsed:.1f} 分钟")
                return 0

            if _should_stop(until, "evaluate"):
                _print_evaluate_summary(state)
                elapsed = (time.time() - start_time) / 60
                print(f"\n耗时：{elapsed:.1f} 分钟（--until evaluate 停止）")
                return 0

            phase = "evaluate"  # fall through 到 evaluate 分支

        else:
            # review 命令：直接进入红绿验证
            # 过滤掉已有验证结果的 bug（verify 中途崩溃后 resume 时避免重复验证）
            already_done = {
                fid for fid in confirmed_ids
                if fid in state.results
                and state.get_result_status(fid) not in ("fix_failed", "")
            }
            if already_done:
                print(f"跳过已验证的 {len(already_done)} 个 bug：{', '.join(sorted(already_done))}")
                confirmed_ids = [fid for fid in confirmed_ids if fid not in already_done]

            if not confirmed_ids:
                print("无 bug 需要验证。")
                return 0

            print(f"\n=== 红绿验证（{len(confirmed_ids)} 个 bug） ===\n")
            from lib.steps.verify import run_verify
            run_verify(state, project_root, confirmed_ids, modules_by_name)
            state.advance("verify")
            state.save(state.state_file(project_root))

            _print_verify_summary(state)

            if _should_stop(until, "verify"):
                elapsed = (time.time() - start_time) / 60
                print(f"\n耗时：{elapsed:.1f} 分钟（--until verify 停止）")
                return 0

            phase = "verify"  # fall through

    # --- evaluate 阶段：deep 模式的 R3 深度评估后 resume ---
    if phase == "evaluate":
        if _should_stop(until, "evaluate"):
            _print_evaluate_summary(state)
            return 0

        # --confirmed 覆盖 R3 判定：用户可以把 R3 skip 的 finding 加回来
        override_ids = _parse_confirmed(confirmed_arg, state)
        if override_ids is not None:
            # 用户显式指定了要验证的 ID，撤销这些 ID 的 eval_skipped 状态
            for fid in override_ids:
                if fid in state.results and state.get_result_status(fid) == "eval_skipped":
                    del state.results[fid]
                    logger.info(f"用户覆盖 R3 判定：{fid} 从 eval_skipped 恢复为待验证")
            remaining = override_ids
        else:
            # 默认：排除所有已有结果的 findings（eval_skipped + 已验证的）
            remaining = [
                f["id"] for f in state.findings
                if f["id"] not in state.results
            ]
        skipped_count = sum(
            1 for r in state.results.values()
            if (r.get("status") if isinstance(r, dict) else getattr(r, "status", "")) == "eval_skipped"
        )
        if remaining:
            print(f"\n=== R4：红绿验证（{len(remaining)} 个 bug，{skipped_count} 个已被 R3 跳过） ===\n")
            from lib.steps.verify import run_verify
            run_verify(state, project_root, remaining, modules_by_name)
        else:
            print("所有 findings 已被 R3 评估跳过，无需红绿验证。")

        state.advance("verify")
        state.save(state.state_file(project_root))

        if state.command == "deep":
            print("\n=== R5：交叉检验（轻量） ===\n")
            from lib.steps.cross_validate import run_cross_validate
            run_cross_validate(state, project_root, modules_by_name)
            state.advance("cross_validate")
            state.save(state.state_file(project_root))

        _print_verify_summary(state)

        if _should_stop(until, "verify"):
            elapsed = (time.time() - start_time) / 60
            print(f"\n耗时：{elapsed:.1f} 分钟（--until verify 停止）")
            return 0

        phase = "verify"  # fall through

    # --- verify 阶段：可能有未完成的验证，然后收尾 ---
    if phase in ("verify", "cross_validate"):
        # 检查是否有未验证的 bug
        unverified = [
            f["id"] for f in state.findings
            if f["id"] not in state.results
            or state.get_result_status(f["id"]) == "fix_failed"
        ]
        if unverified:
            print(f"发现 {len(unverified)} 个未完成验证的 bug，继续...")
            from lib.steps.verify import run_verify
            run_verify(state, project_root, unverified, modules_by_name)
            state.save(state.state_file(project_root))
            _print_verify_summary(state)

            if _should_stop(until, "verify"):
                return 0

        # 收尾
        _run_finalize(state, project_root)

        elapsed = (time.time() - start_time) / 60
        print(f"\n总耗时：{elapsed:.1f} 分钟")
        return 0

    # --- merge 及之后：直接收尾 ---
    if phase in ("merge", "infra_c1", "infra_c2", "report"):
        _run_finalize(state, project_root)
        return 0

    # --- done：已完成 ---
    if phase == "done":
        print("会话已完成，无需继续。")
        return 0

    print(f"未知阶段：{phase}")
    return 1


# ==================== 其他命令 ====================

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


# ==================== cover ====================

def cmd_cover(args):
    """执行 evo-cli cover：分析跨模块测试覆盖缺口并生成集成测试。"""
    import time
    from lib.steps.cover import run_cover

    project_root = _detect_project_root()
    logger.info(f"项目根目录：{project_root}")

    module_filter = None
    if hasattr(args, "modules") and args.modules:
        module_filter = [m.strip() for m in args.modules.split(",")]

    start_time = time.time()
    ok = run_cover(project_root, module_filter=module_filter)

    elapsed = (time.time() - start_time) / 60
    print(f"\n总耗时：{elapsed:.1f} 分钟")
    return 0 if ok else 1


# ==================== trend ====================

def cmd_trend(args):
    """执行 evo-cli trend：展示历次 review 的趋势分析。"""
    from lib.steps.history import print_trend

    project_root = _detect_project_root()
    last_n = getattr(args, "last", 20)
    print_trend(project_root, last_n=last_n)
    return 0


# ==================== 工具函数 ====================

def _estimate_duration(session_id):
    """从 session_id（YYYYMMDD-HHMMSS）估算 review 持续时间（分钟）。

    session_id 记录了 review 开始时间，用当前时间减去即可。
    解析失败时返回 0。
    """
    try:
        from datetime import datetime
        start = datetime.strptime(session_id, "%Y%m%d-%H%M%S")
        elapsed = (datetime.now() - start).total_seconds() / 60
        return round(elapsed, 1)
    except (ValueError, TypeError):
        return 0.0


def _parse_confirmed(confirmed_arg, state):
    """解析 --confirmed 参数，返回合法的 finding ID 列表。

    返回 None 表示未指定 --confirmed（需要交互确认或用全部）。
    返回空列表表示指定了但全部无效。
    """
    if not confirmed_arg:
        return None
    ids = [x.strip() for x in confirmed_arg.split(",") if x.strip()]
    valid_ids = {f["id"] for f in state.findings}
    ids = [fid for fid in ids if fid in valid_ids]
    if ids:
        print(f"使用指定确认：{', '.join(ids)}")
    else:
        print("所有指定的 ID 无效。")
    return ids


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


# ==================== 入口 ====================

def main():
    parser = argparse.ArgumentParser(
        prog="evo-cli",
        description="自进化代码审查 CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # review
    p_review = subparsers.add_parser("review", help="范围审查（最近 5 commit）")
    p_review.add_argument("paths", nargs="*", help="指定审查路径")
    p_review.add_argument("--until", choices=VALID_STAGES, help="执行到指定阶段后停止")
    p_review.set_defaults(func=cmd_review)

    # deep
    p_deep = subparsers.add_parser("deep", help="全模块深度审查")
    p_deep.add_argument("modules", nargs="*", help="指定模块")
    p_deep.add_argument("--until", choices=VALID_STAGES, help="执行到指定阶段后停止")
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
    p_resume.add_argument("--until", choices=VALID_STAGES, help="执行到指定阶段后停止")
    p_resume.add_argument("--confirmed", help="确认的 finding ID 列表（逗号分隔，如 F1,F2,F3）")
    p_resume.set_defaults(func=cmd_resume)

    # cover
    p_cover = subparsers.add_parser("cover", help="分析跨模块测试覆盖缺口并生成集成测试")
    p_cover.add_argument("--modules", help="限定模块（逗号分隔，如 togo-agent,agentapi）")
    p_cover.set_defaults(func=cmd_cover)

    # trend
    p_trend = subparsers.add_parser("trend", help="查看历次 review 的趋势分析")
    p_trend.add_argument("--last", type=int, default=20, help="显示最近 N 次（默认 20）")
    p_trend.set_defaults(func=cmd_trend)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)
