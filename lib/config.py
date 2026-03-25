"""
config.yaml 读写

简易 YAML 解析器，只支持 test-governance/config.yaml 的两级嵌套格式。
零外部依赖，不使用 PyYAML。
"""

import os
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ModuleConfig:
    """单个模块的配置"""
    name: str
    language: str = ""
    src_dir: str = ""
    test_dir: str = ""
    helper_dir: str = ""
    lint_command: str = ""
    typecheck_command: str = ""
    unit_command: str = ""
    cross_command: str = ""
    errcheck_command: str = ""


def _parse_simple_yaml(text: str) -> dict:
    """
    简易 YAML 解析器。
    只支持以下格式（两级嵌套 key-value，值为字符串）：

        top_key:
          second_key:
            key1: "value1"
            key2: value2

    返回嵌套 dict，如 {"top_key": {"second_key": {"key1": "value1", ...}}}。
    支持带引号和不带引号的值，支持 # 行内注释。
    """
    result = {}
    current_l1 = None  # 第一级 key（如 modules）
    current_l2 = None  # 第二级 key（如 togo-agent）

    for line in text.splitlines():
        # 跳过空行和纯注释行
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # 计算缩进级别（空格数）
        indent = len(line) - len(line.lstrip())

        # 去掉行内注释（但要注意引号内的 #）
        # 简单处理：如果 # 在引号外才算注释
        content = _strip_inline_comment(stripped)
        if not content:
            continue

        # 匹配 key: value 或 key:（无值）
        match = re.match(r'^(\S[^:]*?)\s*:\s*(.*?)\s*$', content)
        if not match:
            continue

        key = match.group(1).strip()
        value = match.group(2).strip()

        # 去掉值的引号
        if value and len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        if indent == 0:
            # 第一级 key
            current_l1 = key
            current_l2 = None
            if current_l1 not in result:
                result[current_l1] = {}
        elif indent <= 4 and current_l1 is not None:
            if not value:
                # 第二级分组 key（如模块名）
                current_l2 = key
                if current_l2 not in result[current_l1]:
                    result[current_l1][current_l2] = {}
            else:
                if current_l2 is not None:
                    # 第三级 key-value
                    result[current_l1][current_l2][key] = value
                else:
                    # 第一级下直接的 key-value
                    result[current_l1][key] = value
        elif indent > 4 and current_l1 is not None and current_l2 is not None:
            # 第三级 key-value（缩进 > 4）
            result[current_l1][current_l2][key] = value

    return result


def _strip_inline_comment(s: str) -> str:
    """去掉行内注释，但保留引号内的 #。"""
    in_quote = None
    for i, ch in enumerate(s):
        if ch in ('"', "'"):
            if in_quote is None:
                in_quote = ch
            elif in_quote == ch:
                in_quote = None
        elif ch == '#' and in_quote is None:
            return s[:i].rstrip()
    return s


def load_config(project_root: str) -> dict:
    """
    读取 test-governance/config.yaml，返回解析后的 dict。

    参数:
        project_root: 项目根目录

    返回:
        解析后的配置字典
    """
    config_path = os.path.join(project_root, "test-governance", "config.yaml")
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        text = f.read()

    return _parse_simple_yaml(text)


def get_modules(project_root: str) -> list:
    """
    从 config.yaml 读取模块列表，返回 ModuleConfig 列表。

    参数:
        project_root: 项目根目录

    返回:
        list[ModuleConfig]
    """
    config = load_config(project_root)
    modules_dict = config.get("modules", {})

    modules = []
    for name, props in modules_dict.items():
        if not isinstance(props, dict):
            continue
        module = ModuleConfig(
            name=name,
            language=props.get("language", ""),
            src_dir=props.get("src_dir", ""),
            test_dir=props.get("test_dir", ""),
            helper_dir=props.get("helper_dir", ""),
            lint_command=props.get("lint_command", ""),
            typecheck_command=props.get("typecheck_command", ""),
            unit_command=props.get("unit_command", ""),
            cross_command=props.get("cross_command", ""),
            errcheck_command=props.get("errcheck_command", ""),
        )
        modules.append(module)

    return modules
