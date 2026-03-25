"""阶段 0-1：Bootstrap — 首次自动初始化 test-governance 基础设施"""

import os, logging, json

logger = logging.getLogger(__name__)

def run_bootstrap(state, project_root):
    """检查 test-governance/ 是否完整，不完整则初始化。

    步骤：
    1. 检查 test-governance/ 目录和必要文件
    2. 如果缺失 config.yaml → 调用 claude 扫描项目生成
    3. 如果缺失 gate.sh → 从模板复制
    4. 如果缺失 infrastructure.md / coding-guidelines.md / dimension-coverage.yaml → 创建骨架
    5. 检查 .gitignore 包含 gate-violations.log
    """
    tg_dir = os.path.join(project_root, "test-governance")
    scripts_dir = os.path.join(project_root, "scripts")

    needs_bootstrap = False
    missing = []

    # 检查必要文件
    required_files = {
        "config.yaml": os.path.join(tg_dir, "config.yaml"),
        "infrastructure.md": os.path.join(tg_dir, "infrastructure.md"),
        "coding-guidelines.md": os.path.join(tg_dir, "coding-guidelines.md"),
        "dimension-coverage.yaml": os.path.join(tg_dir, "dimension-coverage.yaml"),
        "gate.sh": os.path.join(scripts_dir, "test-governance-gate.sh"),
    }

    for name, path in required_files.items():
        if not os.path.exists(path):
            missing.append(name)
            needs_bootstrap = True

    if not needs_bootstrap:
        logger.info("test-governance 已完整，跳过 bootstrap")
        return

    logger.info(f"需要初始化：{missing}")

    os.makedirs(tg_dir, exist_ok=True)
    os.makedirs(scripts_dir, exist_ok=True)

    # 生成缺失文件
    if "config.yaml" in missing:
        _generate_config(project_root)

    if "gate.sh" in missing:
        _copy_gate_template(project_root)

    for md_file in ["infrastructure.md", "coding-guidelines.md", "dimension-coverage.yaml"]:
        if md_file in missing:
            _create_skeleton(os.path.join(tg_dir, md_file), md_file)

    # 检查 .gitignore
    _ensure_gitignore(project_root)

    # 跨模块拓扑扫描（多模块时）
    from lib.config import get_modules
    try:
        modules = get_modules(project_root)
        if len(modules) > 1:
            _scan_cross_module_topology(project_root, modules)
    except Exception as e:
        logger.warning(f"跨模块拓扑扫描跳过: {e}")

    logger.info("Bootstrap 完成")


def _generate_config(project_root):
    """调用 claude 扫描项目结构生成 config.yaml"""
    from lib.claude import call_claude_bare
    from lib.prompts.scan import BOOTSTRAP_SCAN_PROMPT

    result = call_claude_bare(
        prompt=BOOTSTRAP_SCAN_PROMPT,
        model="opus",
        tools="Read,Glob,Grep",
        max_turns=10,
        cwd=project_root,
    )

    # 将结果写入 config.yaml
    config_path = os.path.join(project_root, "test-governance", "config.yaml")
    if isinstance(result, dict):
        # 简单格式化为 YAML
        lines = ["# test-governance/config.yaml — 模块配置\n", "modules:\n"]
        for name, mod in result.get("modules", {}).items():
            lines.append(f"  {name}:\n")
            for k, v in mod.items():
                lines.append(f'    {k}: "{v}"\n')
        with open(config_path, 'w') as f:
            f.writelines(lines)
    else:
        # result 是字符串，直接写入
        with open(config_path, 'w') as f:
            f.write(str(result))

    logger.info(f"生成 config.yaml: {config_path}")


def _copy_gate_template(project_root):
    """从内置模板复制 gate.sh"""
    # 模板路径：evo-review-cli/templates/gate.sh
    cli_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    template = os.path.join(cli_root, "templates", "gate.sh")
    target = os.path.join(project_root, "scripts", "test-governance-gate.sh")

    if os.path.exists(template):
        import shutil
        shutil.copy2(template, target)
        os.chmod(target, 0o755)
        logger.info(f"复制 gate 模板: {target}")
    else:
        logger.warning(f"gate 模板不存在: {template}，跳过")


def _create_skeleton(path, filename):
    """创建骨架文件"""
    skeletons = {
        "infrastructure.md": "# 测试基础设施注册表\n\n| 编号 | 类型 | 模块 | 描述 | 文件 |\n|------|------|------|------|------|\n",
        "coding-guidelines.md": "# 编码规范 — 高频违规源头治理\n\n本文件记录高频违规的 ❌/✅ 对比示例。\n",
        "dimension-coverage.yaml": "# 测试维度覆盖配置\n# 维度：1=正常路径 2=副作用清理 3=并发安全 4=错误恢复 5=安全边界 6=故障后可用\n",
    }
    content = skeletons.get(filename, f"# {filename}\n")
    with open(path, 'w') as f:
        f.write(content)
    logger.info(f"创建骨架: {path}")


def _ensure_gitignore(project_root):
    """确保 .gitignore 包含 gate-violations.log"""
    gitignore = os.path.join(project_root, ".gitignore")
    pattern = "test-governance/gate-violations.log"

    if os.path.exists(gitignore):
        with open(gitignore, 'r') as f:
            content = f.read()
        if pattern not in content:
            with open(gitignore, 'a') as f:
                f.write(f"\n# evo-review\n{pattern}\n.evo-review/\n")
            logger.info("更新 .gitignore")
    else:
        with open(gitignore, 'w') as f:
            f.write(f"# evo-review\n{pattern}\n.evo-review/\n")


def _scan_cross_module_topology(project_root, modules):
    """跨模块拓扑扫描：识别模块间通信模式和契约。"""
    from lib.claude import call_claude_bare

    module_names = ", ".join(m.name for m in modules)
    prompt = f"""扫描以下模块间的通信拓扑和契约依赖。

模块列表：{module_names}

请检查：
1. 模块间的 import/依赖关系
2. 共享的类型定义（如 Message 类型在多模块间同步）
3. 通信协议（HTTP/WebSocket/IPC）
4. 跨模块的状态依赖

输出简要的依赖关系描述，用于后续审查时判断跨模块影响。"""

    try:
        result = call_claude_bare(
            prompt=prompt,
            model="opus",
            tools="Read,Glob,Grep",
            max_turns=10,
            cwd=project_root,
        )
        # 写入 test-governance 目录供后续参考
        topo_path = os.path.join(project_root, "test-governance", "cross-module-topology.md")
        with open(topo_path, 'w') as f:
            f.write(f"# 跨模块拓扑（自动生成）\n\n{result}\n")
        logger.info(f"跨模块拓扑扫描完成: {topo_path}")
    except Exception as e:
        logger.warning(f"跨模块拓扑扫描失败: {e}")
