"""评估持久化 + 回归对比

每次 review/deep 完成后，从 ReviewState 提取摘要写入 history.jsonl。
提供趋势分析：整体 verified 率变化、category 级弱点、module 级质量分布。
"""

import json
import logging
import os
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

# history.jsonl 存储位置
_HISTORY_FILE = "history.jsonl"


def save_session_summary(state, project_root, duration_minutes=0.0):
    """从 ReviewState 提取摘要，append 到 history.jsonl。

    在 _run_finalize 末尾调用，无论 review 是否有 verified bug 都记录。
    0 findings 的 review 也记录——用于追踪"干净扫描"的频率。
    """
    # --- 按 status 统计 ---
    by_status = defaultdict(int)
    for fid, result in state.results.items():
        status = _get_status(result)
        by_status[status] += 1

    # --- 按 category 统计 ---
    by_category = defaultdict(lambda: defaultdict(int))
    for f in state.findings:
        cat = f.get("category", "unknown")
        fid = f.get("id", "")
        status = _get_status(state.results.get(fid, {}))
        by_category[cat]["total"] += 1
        by_category[cat][status] += 1

    # --- 按 module 统计 ---
    by_module = defaultdict(lambda: defaultdict(int))
    for f in state.findings:
        mod = f.get("module", "unknown")
        fid = f.get("id", "")
        status = _get_status(state.results.get(fid, {}))
        by_module[mod]["total"] += 1
        by_module[mod][status] += 1

    # --- 按 severity 统计 ---
    by_severity = defaultdict(lambda: defaultdict(int))
    for f in state.findings:
        sev = f.get("severity", "UNKNOWN")
        fid = f.get("id", "")
        status = _get_status(state.results.get(fid, {}))
        by_severity[sev]["total"] += 1
        by_severity[sev][status] += 1

    # --- 计算 verified 率 ---
    verified = by_status.get("verified", 0)
    hallucination = by_status.get("hallucination", 0)
    decidable = verified + hallucination  # 有明确判定的 findings
    verified_rate = round(verified / decidable, 3) if decidable > 0 else None

    # --- 项目名（取 git 仓库目录名）---
    project_name = os.path.basename(os.path.abspath(project_root))

    summary = {
        "session_id": state.session_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "command": state.command,
        "project": project_name,
        "modules": state.modules,
        "scope_files": len(state.scope),
        "duration_minutes": round(duration_minutes, 1),
        "total_findings": len(state.findings),
        "filtered_count": len(getattr(state, "filtered_findings", []) or []),
        "by_status": dict(by_status),
        "by_category": {k: dict(v) for k, v in by_category.items()},
        "by_module": {k: dict(v) for k, v in by_module.items()},
        "by_severity": {k: dict(v) for k, v in by_severity.items()},
        "verified_rate": verified_rate,
    }

    # --- 写入 ---
    history_path = _history_path(project_root)
    os.makedirs(os.path.dirname(history_path), exist_ok=True)

    try:
        with open(history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        logger.info("评估摘要已记录到 %s", history_path)
    except Exception as e:
        # 历史记录写入失败不应阻断主流程
        logger.warning("评估摘要写入失败（非关键）: %s", e)

    return summary


def load_history(project_root):
    """读取 history.jsonl，返回摘要列表（按时间升序）。"""
    history_path = _history_path(project_root)
    if not os.path.isfile(history_path):
        return []

    entries = []
    with open(history_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("history.jsonl 第 %d 行解析失败，跳过", line_num)
    return entries


def print_trend(project_root, last_n=20):
    """输出趋势分析报告。

    三段信息：
    1. 整体趋势：verified 率变化曲线
    2. Category 级弱点：各类别累计精确度排名
    3. Module 级质量：各模块累计精确度排名
    """
    entries = load_history(project_root)

    if not entries:
        print("暂无历史记录。完成至少一次 review 后可查看趋势。")
        return

    # 取最近 N 条
    recent = entries[-last_n:]

    print(f"\n{'='*60}")
    print(f"  评估趋势（共 {len(entries)} 次 review，显示最近 {len(recent)} 次）")
    print(f"{'='*60}")

    # === 1. 整体趋势 ===
    print("\n## 整体趋势\n")

    # verified 率折线（文本版）
    rates = []
    for e in recent:
        vr = e.get("verified_rate")
        if vr is not None:
            rates.append((e["session_id"][:8], vr))  # (日期, 比率)

    if rates:
        print("  verified 率变化：")
        for date, rate in rates:
            bar_len = int(rate * 30)
            bar = "█" * bar_len + "░" * (30 - bar_len)
            print(f"    {date}  {bar}  {rate*100:.0f}%")

        # 趋势判断
        if len(rates) >= 3:
            first_half = [r for _, r in rates[:len(rates)//2]]
            second_half = [r for _, r in rates[len(rates)//2:]]
            avg_first = sum(first_half) / len(first_half)
            avg_second = sum(second_half) / len(second_half)
            diff = avg_second - avg_first
            if diff > 0.05:
                print(f"\n  趋势：上升 (+{diff*100:.0f}pp)，扫描精确度在改善")
            elif diff < -0.05:
                print(f"\n  趋势：下降 ({diff*100:.0f}pp)，扫描精确度在退化，建议检查近期 prompt/filter 改动")
            else:
                print(f"\n  趋势：稳定（波动 {abs(diff)*100:.0f}pp）")
    else:
        print("  暂无足够的 verified 率数据")

    # 汇总数字
    total_findings = sum(e.get("total_findings", 0) for e in recent)
    total_verified = sum(e.get("by_status", {}).get("verified", 0) for e in recent)
    total_hallucination = sum(e.get("by_status", {}).get("hallucination", 0) for e in recent)
    total_fix_failed = sum(e.get("by_status", {}).get("fix_failed", 0) for e in recent)
    total_filtered = sum(e.get("filtered_count", 0) for e in recent)

    print(f"\n  累计：{total_findings} findings, "
          f"{total_verified} verified, "
          f"{total_hallucination} hallucination, "
          f"{total_fix_failed} fix_failed")
    if total_filtered:
        print(f"  运行时过滤：{total_filtered} 个不可能 bug 被 filter 拦截")

    # === 2. Category 级弱点 ===
    print(f"\n## Category 精确度排名（累计）\n")

    cat_totals = defaultdict(lambda: {"total": 0, "verified": 0, "hallucination": 0})
    for e in recent:
        for cat, stats in e.get("by_category", {}).items():
            cat_totals[cat]["total"] += stats.get("total", 0)
            cat_totals[cat]["verified"] += stats.get("verified", 0)
            cat_totals[cat]["hallucination"] += stats.get("hallucination", 0)

    # 按精确度排序
    cat_ranked = []
    for cat, stats in cat_totals.items():
        decidable = stats["verified"] + stats["hallucination"]
        precision = stats["verified"] / decidable if decidable > 0 else None
        cat_ranked.append((cat, stats["total"], stats["verified"], stats["hallucination"], precision))

    cat_ranked.sort(key=lambda x: (x[4] is None, -(x[4] or 0)))

    for cat, total, verified, hallu, prec in cat_ranked:
        if prec is not None:
            prec_str = f"{prec*100:.0f}%"
            marker = "  ← 需要优化" if prec < 0.4 else ""
            print(f"  {cat:25s}  {prec_str:>4s} verified  ({verified}/{verified+hallu})"
                  f"  共 {total} findings{marker}")
        else:
            print(f"  {cat:25s}   N/A           共 {total} findings（无明确判定）")

    # === 3. Module 级质量 ===
    print(f"\n## Module 精确度排名（累计）\n")

    mod_totals = defaultdict(lambda: {"total": 0, "verified": 0, "hallucination": 0})
    for e in recent:
        for mod, stats in e.get("by_module", {}).items():
            mod_totals[mod]["total"] += stats.get("total", 0)
            mod_totals[mod]["verified"] += stats.get("verified", 0)
            mod_totals[mod]["hallucination"] += stats.get("hallucination", 0)

    mod_ranked = []
    for mod, stats in mod_totals.items():
        decidable = stats["verified"] + stats["hallucination"]
        precision = stats["verified"] / decidable if decidable > 0 else None
        mod_ranked.append((mod, stats["total"], stats["verified"], stats["hallucination"], precision))

    mod_ranked.sort(key=lambda x: (x[4] is None, -(x[4] or 0)))

    for mod, total, verified, hallu, prec in mod_ranked:
        if prec is not None:
            prec_str = f"{prec*100:.0f}%"
            marker = "  ← 弱项" if prec < 0.4 else ""
            print(f"  {mod:25s}  {prec_str:>4s} verified  ({verified}/{verified+hallu})"
                  f"  共 {total} findings{marker}")
        else:
            print(f"  {mod:25s}   N/A           共 {total} findings（无明确判定）")

    print(f"\n{'='*60}\n")


def _get_status(result):
    """从 BugResult（dict 或 dataclass）中安全提取 status。"""
    if isinstance(result, dict):
        return result.get("status", "unknown")
    return getattr(result, "status", "unknown")


def _history_path(project_root):
    """返回 history.jsonl 的绝对路径。"""
    return os.path.join(project_root, ".evo-review", _HISTORY_FILE)
