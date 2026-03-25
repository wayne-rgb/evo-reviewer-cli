"""阶段 B 后半：确认并合并 worktree"""

import logging

logger = logging.getLogger(__name__)


def run_merge(state, project_root):
    """展示验证结果，确认后合并 worktree 到主分支。"""
    from lib.report import generate_verify_report
    from lib.worktree import merge_worktree, Worktree

    report = generate_verify_report(state.results, state.findings)
    print(report)

    # 统计
    verified_count = sum(
        1 for fid in state.results
        if state.get_result_status(fid) == "verified"
    )

    if verified_count == 0:
        print("\n无已验证修复，跳过合并。")
        return False

    print(f"\n以上 {verified_count} 个已验证修复在 worktree 分支中。")

    # 非交互模式（后台运行）自动确认合并
    import sys
    if not sys.stdin.isatty():
        logger.info("非交互模式，自动确认合并")
    else:
        try:
            user_input = input("确认合并？[y/n/选择编号]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return False

        if user_input.lower() not in ('y', 'yes', ''):
            print("已取消合并")
            return False

    # 执行合并（同一 Xcode 项目的多个模块共享 worktree，去重防止重复合并）
    merge_failed = False
    merged_branches = set()
    for mod_name, wt_info in state.worktrees.items():
        branch = wt_info["branch"]
        if branch in merged_branches:
            logger.info(f"跳过 {mod_name}（分支 {branch} 已合并）")
            continue
        wt = Worktree(
            path=wt_info["path"],
            branch=branch,
            modules=[mod_name],
        )
        try:
            merge_worktree(wt, project_root)
            merged_branches.add(branch)
            logger.info(f"合并 worktree: {mod_name}")
        except Exception as e:
            logger.error(f"合并 {mod_name} 失败（可能有冲突）: {e}")
            merge_failed = True

    if merge_failed:
        print("\n⚠️ 部分 worktree 合并失败。请手动检查 git 状态。")
        return False

    # push 统一在 _run_finalize 最后执行，此处不再单独 push
    return True
