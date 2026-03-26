"""
Claude CLI 调用封装

提供两种调用模式：
- call_claude_bare: 结构化调用（扫描/归类/判定），隔离、无状态、强制 JSON
- call_claude_session: 代码工作调用（写测试/修复/规则），加载 CLAUDE.md + hooks
"""

import json
import logging
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)


def call_claude_bare(
    prompt: str,
    model: str = "opus",
    tools: str = "Read,Glob,Grep",
    output_schema: Optional[dict] = None,
    max_turns: int = 20,
    cwd: Optional[str] = None,
    timeout: int = 300,
) -> dict:
    """
    模式 A：结构化调用（扫描/归类/判定）。
    使用 --setting-sources "project" 只加载 CLAUDE.md（项目架构/规范知识），
    配合 --disable-slash-commands + --strict-mcp-config 跳过 hooks / plugins / MCP。
    配合 --json-schema 强制结构化输出。

    参数:
        prompt: 发送给 Claude 的提示文本
        model: 模型名称，默认 opus（别名，等价于 claude-opus-4-6）
        tools: 允许的工具列表（逗号分隔），空字符串表示禁用所有工具
        output_schema: JSON Schema dict，传入后用 --json-schema 强制结构化输出
        max_turns: 最大对话轮数
        cwd: 工作目录
        timeout: 超时秒数

    返回:
        structured_output（有 schema 时优先）或 result 字段
    """
    cmd = [
        "claude",
        "--setting-sources", "project",  # 只加载 CLAUDE.md（项目知识），跳过 hooks / plugins
        "--disable-slash-commands",   # 跳过 skills / plugins
        "--strict-mcp-config",        # 不自动发现 MCP（未传 --mcp-config 则无 MCP）
        "--permission-mode", "dontAsk",  # 仅执行 --allowedTools 预批准的工具
        "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--max-turns", str(max_turns),
        "--no-session-persistence",  # 避免磁盘 session 垃圾堆积
    ]

    # 工具配置：
    # --tools 限制可用工具集（白名单），Claude 只能看到这些工具
    # --allowedTools 自动批准（无权限提示直接执行）
    if tools is not None:
        if tools == "":
            cmd.extend(["--tools", ""])
        else:
            cmd.extend(["--tools", tools, "--allowedTools", tools])

    # JSON Schema 结构化输出
    if output_schema is not None:
        cmd.extend(["--json-schema", json.dumps(output_schema)])

    logger.info("call_claude_bare: model=%s, tools=%s, max_turns=%d, schema=%s",
                model, tools, max_turns, "yes" if output_schema else "no")
    logger.debug("命令: %s", " ".join(cmd[:10]) + " ...")

    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        logger.error("call_claude_bare 超时（%.1fs），timeout=%ds", elapsed, timeout)
        raise RuntimeError(f"Claude CLI 调用超时（{timeout}s）")

    elapsed = time.time() - start
    logger.info("call_claude_bare 完成，耗时 %.1fs，退出码 %d", elapsed, proc.returncode)

    if proc.returncode != 0:
        logger.error("Claude CLI 非零退出: stderr=%s", proc.stderr[:500] if proc.stderr else "(空)")
        raise RuntimeError(f"Claude CLI 退出码 {proc.returncode}: {proc.stderr[:500]}")

    # 解析 JSON 输出
    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError("Claude CLI 返回空输出")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        logger.error("JSON 解析失败: %s\n原始输出前 300 字符: %s", e, stdout[:300])
        raise RuntimeError(f"Claude CLI 输出非法 JSON: {e}")

    _log_usage(data)

    # 优先取 structured_output（schema 模式），fallback 到 result
    return _extract_result(data)


def call_claude_session(
    prompt: str,
    model: str = "opus",
    tools: str = "Read,Glob,Grep,Edit,Write",
    max_turns: int = 15,
    cwd: Optional[str] = None,
    timeout: int = 300,
) -> dict:
    """
    模式 B：代码工作调用（写测试/修复/规则）。
    使用 -p 模式（加载 CLAUDE.md + hooks），但 --no-session-persistence 保证错误隔离。
    每次独立调用，不复用 session。

    参数:
        prompt: 发送给 Claude 的提示文本
        model: 模型名称，默认 opus
        tools: 允许的工具列表（逗号分隔），默认不含 Bash
        max_turns: 最大对话轮数
        cwd: 工作目录
        timeout: 超时秒数

    返回:
        result 字段内容
    """
    cmd = [
        "claude",
        "--permission-mode", "dontAsk",  # 仅执行 --allowedTools 预批准的工具
        "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
        "--max-turns", str(max_turns),
    ]

    # --tools 声明可用工具集，--allowedTools 预批准（无权限提示）
    if tools:
        cmd.extend(["--tools", tools, "--allowedTools", tools])

    logger.info("call_claude_session: model=%s, tools=%s, max_turns=%d",
                model, tools, max_turns)
    logger.debug("命令: %s", " ".join(cmd[:8]) + " ...")

    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        logger.error("call_claude_session 超时（%.1fs），timeout=%ds", elapsed, timeout)
        raise RuntimeError(f"Claude CLI 调用超时（{timeout}s）")

    elapsed = time.time() - start
    logger.info("call_claude_session 完成，耗时 %.1fs，退出码 %d", elapsed, proc.returncode)

    if proc.returncode != 0:
        logger.error("Claude CLI 非零退出: stderr=%s", proc.stderr[:500] if proc.stderr else "(空)")
        raise RuntimeError(f"Claude CLI 退出码 {proc.returncode}: {proc.stderr[:500]}")

    # 解析 JSON 输出
    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError("Claude CLI 返回空输出")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        logger.error("JSON 解析失败: %s\n原始输出前 300 字符: %s", e, stdout[:300])
        raise RuntimeError(f"Claude CLI 输出非法 JSON: {e}")

    _log_usage(data)

    return _extract_result(data)


def _extract_result(data):
    """从 Claude CLI JSON 输出提取结果。
    优先取 structured_output（--json-schema 模式），fallback 到 result。
    """
    if not isinstance(data, dict):
        return data

    # --json-schema 模式：结果在 structured_output
    structured = data.get("structured_output")
    if structured is not None:
        return structured

    # 普通模式：结果在 result
    if "result" in data:
        return data["result"]

    return data


def _log_usage(data: dict) -> None:
    """从 Claude CLI JSON 输出中提取并记录 token 使用信息。"""
    if not isinstance(data, dict):
        return

    usage = data.get("usage")
    if usage and isinstance(usage, dict):
        input_tokens = usage.get("input_tokens", "?")
        output_tokens = usage.get("output_tokens", "?")
        logger.info("Token 使用: input=%s, output=%s", input_tokens, output_tokens)

    cost = data.get("cost_usd") or data.get("cost")
    if cost is not None:
        logger.info("费用: $%s", cost)

    num_turns = data.get("num_turns")
    if num_turns is not None:
        logger.info("对话轮数: %s", num_turns)
