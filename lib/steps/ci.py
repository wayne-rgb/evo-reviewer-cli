"""CI 验证 — 纯 CLI 代码，不调 claude"""

import logging
import subprocess
import os

logger = logging.getLogger(__name__)


def run_ci(project_root):
    """根据 git diff 确定改动范围，运行对应模块的检查。"""
    from lib.git import git_diff_files, files_to_modules
    from lib.config import get_modules

    changed = git_diff_files(n=1, cwd=project_root)

    if not changed:
        print("无文件改动，跳过 CI")
        return True

    # 检查是否全是文档
    all_docs = all(
        f.endswith(('.md', '.txt', '.yaml', '.yml', '.json', '.tsv'))
        for f in changed
    )

    if all_docs:
        print("仅文档改动，只跑 preflight")
        return _run_cmd("bash scripts/test-governance-gate.sh preflight", project_root)

    all_modules = get_modules(project_root)
    module_files = files_to_modules(changed, all_modules)
    # files_to_modules 返回 {name: [files]}，转换为模块对象列表
    modules_by_name = {m.name: m for m in all_modules}
    affected = [modules_by_name[n] for n in module_files if n in modules_by_name]

    if not affected:
        print("改动文件未匹配到已知模块，只跑 preflight")
        return _run_cmd("bash scripts/test-governance-gate.sh preflight", project_root)

    print(f"受影响模块：{', '.join(m.name for m in affected)}")

    all_ok = True

    # preflight 必跑
    if not _run_cmd("bash scripts/test-governance-gate.sh preflight", project_root):
        all_ok = False

    # 各模块检查
    for m in affected:
        print(f"\n--- 模块: {m.name} ({m.language}) ---")

        if m.lint_command:
            if not _run_cmd(m.lint_command, project_root):
                all_ok = False

        if m.typecheck_command:
            if not _run_cmd(m.typecheck_command, project_root):
                all_ok = False

        if m.unit_command:
            if not _run_cmd(m.unit_command, project_root):
                all_ok = False

    # 跨模块检查
    if len(affected) > 1:
        for m in affected:
            if m.cross_command:
                print(f"\n--- 跨模块: {m.name} ---")
                if not _run_cmd(m.cross_command, project_root):
                    all_ok = False

    if all_ok:
        print("\nCI 全部通过")
    else:
        print("\nCI 有失败项")

    return all_ok


def _run_cmd(cmd, project_root):
    """执行命令并显示结果"""
    print(f"  > {cmd}")
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=project_root,
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            print("  通过")
            return True
        else:
            # 只显示最后 20 行
            output = (result.stdout + result.stderr).strip().split('\n')
            for line in output[-20:]:
                print(f"    {line}")
            print(f"  失败 (exit {result.returncode})")
            return False
    except subprocess.TimeoutExpired:
        print("  超时")
        return False
    except Exception as e:
        print(f"  异常: {e}")
        return False
