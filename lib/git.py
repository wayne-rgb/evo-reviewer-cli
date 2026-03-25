"""
Git 操作封装

所有 git 命令通过 subprocess.run 执行，返回值或抛异常。
"""

import subprocess
from typing import Optional


def _run_git(args: list, cwd: Optional[str] = None, check: bool = True) -> str:
    """
    执行 git 命令，返回 stdout。

    参数:
        args: git 子命令及参数列表（不含 'git'）
        cwd: 工作目录
        check: 是否在非零退出时抛异常

    返回:
        命令的 stdout 输出（已 strip）
    """
    cmd = ["git"] + args
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git 命令失败: {' '.join(cmd)}\n"
            f"退出码: {proc.returncode}\n"
            f"stderr: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def git_diff_files(n: int = 5, cwd: Optional[str] = None) -> list:
    """
    获取最近 n 个 commit 的变更文件列表。

    自动处理 commit 数不足 n 个的情况（回退到首个 commit）。

    参数:
        n: 回溯的 commit 数量
        cwd: 工作目录

    返回:
        文件路径列表
    """
    # 获取实际 commit 数量，防止 HEAD~n 超出范围
    try:
        count_str = _run_git(["rev-list", "--count", "HEAD"], cwd=cwd)
        actual_count = int(count_str)
    except (RuntimeError, ValueError):
        actual_count = 0

    if actual_count == 0:
        return []

    if actual_count < n:
        if actual_count == 1:
            # 仅一个 commit：用 diff-tree 获取该 commit 引入的文件
            output = _run_git(
                ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
                cwd=cwd, check=False,
            )
        else:
            # commit 不足 n 个，比较最早 commit 到 HEAD
            output = _run_git(
                ["diff", "--name-only", "--diff-filter=ACMRT", "HEAD~" + str(actual_count - 1)],
                cwd=cwd, check=False,
            )
        if not output:
            # fallback: 列出所有 tracked 文件
            output = _run_git(["ls-files"], cwd=cwd, check=False)
    else:
        output = _run_git(["diff", "--name-only", f"HEAD~{n}"], cwd=cwd)

    if not output:
        return []
    return [line for line in output.splitlines() if line.strip()]


def git_diff_content(base: str = "HEAD~5", cwd: Optional[str] = None) -> str:
    """
    获取与指定 base 之间的 diff 内容。

    参数:
        base: diff 的基准（默认 HEAD~5）
        cwd: 工作目录

    返回:
        diff 文本
    """
    return _run_git(["diff", base], cwd=cwd)


def git_commit(message: str, cwd: Optional[str] = None) -> str:
    """
    暂存所有变更并提交。

    参数:
        message: commit 消息
        cwd: 工作目录

    返回:
        git commit 的输出
    """
    _run_git(["add", "-A"], cwd=cwd)
    return _run_git(["commit", "-m", message], cwd=cwd)


def git_push(cwd: Optional[str] = None) -> str:
    """
    推送到远端。

    返回:
        git push 的输出
    """
    return _run_git(["push"], cwd=cwd)


def git_current_branch(cwd: Optional[str] = None) -> str:
    """
    获取当前分支名。

    返回:
        分支名字符串
    """
    return _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)


def git_root(cwd: Optional[str] = None) -> str:
    """
    获取 git 仓库根目录的绝对路径。

    返回:
        仓库根目录路径
    """
    return _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)


def files_to_modules(files: list, modules: list) -> dict:
    """
    将文件列表归属到模块（根据 src_dir 前缀匹配）。

    参数:
        files: 文件路径列表（相对于仓库根目录）
        modules: ModuleConfig 列表（来自 lib.config.get_modules）

    返回:
        dict，key 为模块名，value 为该模块下的文件列表。
        未匹配到任何模块的文件归入 "_other" key。
    """
    result = {}
    unmatched = []

    for filepath in files:
        matched = False
        for mod in modules:
            if not mod.src_dir:
                continue
            # src_dir 是相对路径前缀，如 "togo-agent/src/"
            if filepath.startswith(mod.src_dir):
                result.setdefault(mod.name, []).append(filepath)
                matched = True
                break
            # 也尝试匹配 test_dir
            if mod.test_dir and filepath.startswith(mod.test_dir):
                result.setdefault(mod.name, []).append(filepath)
                matched = True
                break
        if not matched:
            unmatched.append(filepath)

    if unmatched:
        result["_other"] = unmatched

    return result
