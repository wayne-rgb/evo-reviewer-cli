"""阶段 C-2：文档 + 趋势治理 + 存量清理"""

import logging
import subprocess
import json

logger = logging.getLogger(__name__)


def run_infra_c2(state, project_root):
    """Phase C-2：
    1. CLI 跑 trend
    2. claude 更新文档 + 清理存量（模式 B，有 Bash）
    """
    from lib.claude import call_claude_session
    from lib.prompts.infra import PHASE_C2_PROMPT

    verified = [
        f for f in state.findings
        if state.results.get(f["id"], {}).get("status") == "verified"
    ]

    # 1. 跑 trend
    trend_output = ""
    try:
        result = subprocess.run(
            ["bash", "scripts/test-governance-gate.sh", "trend"],
            cwd=project_root, capture_output=True, text=True, timeout=30,
        )
        trend_output = result.stdout
    except Exception as e:
        logger.warning(f"trend 获取失败: {e}")

    # 解析高频规则
    high_freq = _parse_high_freq(trend_output)

    # 2. claude 更新文档（模式 B，有 Bash — 需要跑单元测试验证清理）
    call_claude_session(
        prompt=PHASE_C2_PROMPT.format(
            verified_bugs_json=json.dumps(verified, ensure_ascii=False, indent=2),
            trend_output=trend_output,
            high_freq_rules="\n".join(f"- {r}" for r in high_freq) if high_freq else "无",
        ),
        model="opus",
        tools="Read,Glob,Grep,Edit,Write,Bash",  # C-2 需要 Bash
        max_turns=25,
        cwd=project_root,
    )

    # commit + push
    from lib.git import git_commit, git_push
    try:
        git_commit("Phase C-2: 文档 + 趋势治理 + 存量清理", cwd=project_root)
        git_push(cwd=project_root)
    except Exception as e:
        logger.warning(f"C-2 提交/推送失败: {e}")

    state.phase_c2_done = True
    logger.info("Phase C-2 完成")


def _parse_high_freq(trend_output):
    """从 trend 输出解析高频规则（>=10 次）"""
    high_freq = []
    for line in trend_output.split('\n'):
        line = line.strip()
        # 匹配格式如 "  15  R1-xxx  <- 高频..."
        if '高频' in line:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    count = int(parts[0])
                    if count >= 10:
                        high_freq.append(parts[1])
                except ValueError:
                    pass
    return high_freq
