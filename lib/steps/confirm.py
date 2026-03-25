"""阶段 2：展示确认清单，等待用户确认"""

import logging, sys

logger = logging.getLogger(__name__)

def run_confirm(state, project_root):
    """展示盲区和 bug 清单，等待用户确认。

    返回确认的 finding IDs 列表。如果用户 q 退出，返回空列表。
    """
    from lib.report import generate_confirm_report

    report = generate_confirm_report(state.gaps, state.findings)
    print(report)

    total_bugs = len(state.findings)
    total_gaps = len(state.gaps)

    print(f"\n以上 {total_gaps} 个盲区（涉及 {total_bugs} 个 bug），确认后直接执行。")

    # 非交互模式（stdin 关闭，如后台运行）自动全部确认
    if not sys.stdin.isatty():
        logger.info("非交互模式，自动全部确认")
        return [f["id"] for f in state.findings]

    try:
        user_input = input("Enter 全部确认 / 输入 F 编号排除（如 F1,F3）/ q 退出: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消")
        return []

    if user_input.lower() == 'q':
        return []

    if user_input == '':
        # 全部确认
        return [f["id"] for f in state.findings]

    # 排除指定的 finding
    excluded = set(x.strip().upper() for x in user_input.split(','))
    confirmed = [f["id"] for f in state.findings if f["id"] not in excluded]

    logger.info(f"确认 {len(confirmed)} 个 bug（排除 {len(excluded)} 个）")
    return confirmed
