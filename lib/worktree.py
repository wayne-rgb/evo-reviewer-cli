"""
智能 worktree 管理

提供 git worktree 的创建、合并、回滚、提交等操作。
核心策略：同一 Xcode 项目的模块合并到一个 worktree，避免 pbxproj 冲突。
"""

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Worktree:
    """一个 git worktree 实例"""
    path: str       # worktree 在磁盘上的绝对路径
    branch: str     # worktree 对应的分支名
    modules: list = field(default_factory=list)  # 该 worktree 中包含的模块名列表


def plan_worktrees(modules: list, project_root: str) -> dict:
    """
    智能 worktree 分配，避免 pbxproj 冲突。

    核心规则：
    - 属于同一 Xcode 项目（.xcodeproj）的模块合并到同一个 worktree
    - 不属于任何 Xcode 项目的模块各自独立一个 worktree
    - 返回 {模块名: Worktree} 的映射

    参数:
        modules: 模块列表，每个模块需要有 name 和 src_dir 属性
        project_root: 项目根目录的绝对路径

    返回:
        dict — 键为模块名，值为 Worktree 实例
    """
    # 按 Xcode 项目分组
    groups = {}
    for m in modules:
        xcode_proj = _get_xcode_project(m, project_root)
        name = _module_name(m)
        # Xcode 项目路径作为 key（同项目模块合并），否则用模块名
        key = xcode_proj if xcode_proj else name
        groups.setdefault(key, []).append(m)

    logger.info("worktree 分组结果: %d 个组（来自 %d 个模块）", len(groups), len(modules))

    worktrees = {}
    for key, group_modules in groups.items():
        # 用组内第一个模块的名称作为 worktree 名称
        first_name = _module_name(group_modules[0])
        wt = create_worktree(first_name, project_root)
        # 记录该 worktree 包含的所有模块
        wt.modules = [_module_name(m) for m in group_modules]
        for m in group_modules:
            worktrees[_module_name(m)] = wt

    return worktrees


def create_worktree(name: str, project_root: str) -> Worktree:
    """
    创建 git worktree。

    分支命名格式: evo-review-{name}-{timestamp}
    路径: {project_root}/.evo-review/worktrees/{name}

    参数:
        name: worktree 名称（通常是模块名）
        project_root: 项目根目录

    返回:
        Worktree 实例
    """
    branch = f"evo-review-{name}-{int(time.time())}"
    path = os.path.join(project_root, ".evo-review", "worktrees", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # 清理已存在的 worktree
    if os.path.exists(path):
        logger.info("清理已存在的 worktree: %s", path)
        remove_worktree(path, project_root)

    logger.info("创建 worktree: path=%s, branch=%s", path, branch)
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, path],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    logger.debug("git worktree add 输出: %s", result.stdout.strip())

    wt = Worktree(path=path, branch=branch, modules=[name])
    _precheck_worktree(wt, project_root)
    return wt


def remove_worktree(path: str, project_root: str) -> None:
    """
    移除 git worktree。使用 --force 确保即使有未提交改动也能移除。

    参数:
        path: worktree 路径
        project_root: 项目根目录
    """
    result = subprocess.run(
        ["git", "worktree", "remove", "--force", path],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("移除 worktree 失败（可能已不存在）: %s — %s", path, result.stderr.strip())
    else:
        logger.info("已移除 worktree: %s", path)


def merge_worktree(wt: Worktree, project_root: str) -> None:
    """
    合并 worktree 分支到当前分支，然后清理。

    流程:
    1. git merge --no-edit 合并分支
    2. 移除 worktree 目录
    3. 删除临时分支

    参数:
        wt: 要合并的 Worktree 实例
        project_root: 项目根目录（主仓库）
    """
    logger.info("合并 worktree 分支 %s 到主分支", wt.branch)

    # 合并
    subprocess.run(
        ["git", "merge", wt.branch, "--no-edit"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )

    # 移除 worktree
    remove_worktree(wt.path, project_root)

    # 删除临时分支
    result = subprocess.run(
        ["git", "branch", "-d", wt.branch],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("删除分支 %s 失败: %s", wt.branch, result.stderr.strip())
    else:
        logger.info("已删除分支: %s", wt.branch)


def revert_bug_files(wt_path: str, bug: dict) -> None:
    """
    回滚单个 bug 相关的文件改动。

    当某个 bug 的修复被证实是幻觉时，需要回滚该 bug 在 worktree 中的改动。
    当前实现：回滚 worktree 中所有未提交的改动（保守策略）。

    参数:
        wt_path: worktree 路径
        bug: bug 信息字典（预留，未来可用于精确回滚特定文件）
    """
    logger.info("回滚 worktree %s 中的未提交改动", wt_path)
    subprocess.run(
        ["git", "checkout", "--", "."],
        cwd=wt_path,
        capture_output=True,
        text=True,
    )


def commit_in_worktree(wt: Worktree, message: str) -> bool:
    """
    在 worktree 中暂存所有改动并提交。

    参数:
        wt: Worktree 实例
        message: 提交信息

    返回:
        True 表示有内容被提交，False 表示无改动（跳过提交）
    """
    # 暂存所有改动
    subprocess.run(
        ["git", "add", "-A"],
        cwd=wt.path,
        check=True,
        capture_output=True,
        text=True,
    )

    # 检查是否有待提交的内容
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=wt.path,
        capture_output=True,
        text=True,
    )

    if not result.stdout.strip():
        logger.info("worktree %s 无改动，跳过提交", wt.path)
        return False

    # 提交
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=wt.path,
        check=True,
        capture_output=True,
        text=True,
    )
    logger.info("已在 worktree %s 提交: %s", wt.path, message[:80])
    return True


def cleanup_all_worktrees(project_root: str) -> None:
    """
    清理所有 evo-review 创建的 worktree。

    用于异常恢复或手动清理场景。扫描 .evo-review/worktrees/ 目录，
    逐个移除所有 worktree。

    参数:
        project_root: 项目根目录
    """
    wt_dir = os.path.join(project_root, ".evo-review", "worktrees")
    if not os.path.isdir(wt_dir):
        logger.info("无 worktree 目录，跳过清理")
        return

    for name in os.listdir(wt_dir):
        path = os.path.join(wt_dir, name)
        if os.path.isdir(path):
            remove_worktree(path, project_root)

    # 清理 git worktree 的残留引用
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    logger.info("已清理所有 evo-review worktree")


def _get_xcode_project(module, project_root: str) -> Optional[str]:
    """
    检查模块是否属于某个 Xcode 项目。

    通过查找模块 src_dir 的父目录中是否存在 .xcodeproj 来判断。
    属于同一 Xcode 项目的模块应合并到同一 worktree。

    参数:
        module: 模块对象（需要有 src_dir 属性）或 dict
        project_root: 项目根目录

    返回:
        Xcode 项目路径（如 "macos-app/VoiceToGo.xcodeproj"），不属于则返回 None
    """
    src_dir = _module_src_dir(module)
    if not src_dir:
        return None

    src = os.path.join(project_root, src_dir)
    parent = os.path.dirname(src.rstrip("/"))

    try:
        entries = os.listdir(parent) if os.path.isdir(parent) else []
    except OSError:
        return None

    for item in entries:
        if item.endswith(".xcodeproj"):
            return os.path.join(parent, item)

    return None


def _precheck_worktree(wt: Worktree, project_root: str) -> None:
    """worktree 创建后的环境预检。

    按语言检测常见问题：
    - Go: 确保 go.mod 可被识别（go list），防止 "cannot find main module"
    - TypeScript: 如果有 package.json 但无 node_modules，跑 npm install
    - Swift: 无需特殊处理（Xcode 自动管理）

    预检失败只记 warning，不阻塞流程。所有操作包在 try-except 中防止崩溃。
    """
    for mod_name in wt.modules:
        try:
            _precheck_single_module(wt.path, mod_name)
        except Exception as e:
            logger.warning("worktree 预检 %s 异常（不阻塞）: %s", mod_name, e)


def _precheck_single_module(wt_path: str, mod_name: str) -> None:
    """单个模块的环境预检。"""
    mod_dir = os.path.join(wt_path, mod_name)

    # Go 模块检查 — 在模块目录或 worktree 根目录找 go.mod
    go_mod = os.path.join(mod_dir, "go.mod")
    if not os.path.exists(go_mod):
        go_mod = os.path.join(wt_path, "go.mod")
    if os.path.exists(go_mod):
        go_dir = os.path.dirname(go_mod)
        result = subprocess.run(
            ["go", "list", "./..."],
            cwd=go_dir,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "worktree 预检: Go 模块 %s 的 go list 失败: %s",
                mod_name, result.stderr[:200],
            )
        else:
            logger.info("worktree 预检: Go 模块 %s 可用", mod_name)

    # TypeScript 模块检查 — 在模块目录找 package.json
    pkg_json = os.path.join(mod_dir, "package.json")
    if os.path.exists(pkg_json):
        node_modules = os.path.join(mod_dir, "node_modules")
        if not os.path.isdir(node_modules):
            logger.info("worktree 预检: %s 缺少 node_modules，执行 npm install", mod_name)
            subprocess.run(
                ["npm", "install", "--prefer-offline", "--no-audit"],
                cwd=mod_dir,
                capture_output=True, text=True, timeout=120,
            )
        else:
            logger.info("worktree 预检: TypeScript 模块 %s 可用", mod_name)


def _module_name(module) -> str:
    """从模块对象/字典中提取名称"""
    if isinstance(module, dict):
        return module.get("name", str(module))
    return getattr(module, "name", str(module))


def _module_src_dir(module) -> Optional[str]:
    """从模块对象/字典中提取源码目录"""
    if isinstance(module, dict):
        return module.get("src_dir")
    return getattr(module, "src_dir", None)
