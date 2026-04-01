"""
config.yaml 读写

简易 YAML 解析器，只支持 test-governance/config.yaml 的两级嵌套格式。
零外部依赖，不使用 PyYAML。
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# 语言复杂度系数：Swift/ObjC 的 UI+并发特性需要更多分析时间
_LANG_COMPLEXITY = {
    "swift": 1.8,
    "go": 1.0,
    "typescript": 1.2,
    "python": 1.0,
}

# 超时下限和上限（秒）
# 宁可多等几分钟，也不要超时失败——失败 = 100% token 浪费 + 重试时间
_TIMEOUT_MIN = 480   # 8 分钟：即使小模块也需要深读协议逻辑
_TIMEOUT_MAX = 1800  # 30 分钟：大型 Swift 项目（10MB+）的扫描上限


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

    def estimate_timeout(self, project_root: str, task: str = "scan") -> int:
        """根据模块规模和语言复杂度动态估算 Claude 调用超时。

        估算公式：
            timeout = base + (file_count × per_file) + (total_kb × per_kb)
            timeout *= lang_complexity
            clamp(TIMEOUT_MIN, TIMEOUT_MAX)

        参数:
            project_root: 项目根目录
            task: 任务类型 — "scan"（扫描）或 "verify"（写测试/修复）

        返回:
            超时秒数
        """
        if not self.src_dir:
            logger.warning("模块 %s 的 src_dir 未配置，使用最大超时 %ds", self.name, _TIMEOUT_MAX)
            return _TIMEOUT_MAX
        src_path = os.path.join(project_root, self.src_dir)
        file_count, total_bytes = _measure_source(src_path)

        total_kb = total_bytes / 1024.0
        lang_factor = _LANG_COMPLEXITY.get(self.language, 1.0)

        if task == "verify":
            # 验证只操作单个文件附近，基数低但保留语言系数
            base, per_file, per_kb = 120, 0.5, 0.02
        else:
            # 扫描需要广读代码；字节影响远小于文件数（Claude 按 token 读）
            base, per_file, per_kb = 150, 1.2, 0.03

        raw = (base + file_count * per_file + total_kb * per_kb) * lang_factor
        timeout = int(max(_TIMEOUT_MIN, min(_TIMEOUT_MAX, raw)))

        logger.info(
            "estimate_timeout: %s task=%s files=%d kb=%.0f lang=%s factor=%.1f → %ds",
            self.name, task, file_count, total_kb, self.language, lang_factor, timeout,
        )
        return timeout


def _measure_source(src_path: str) -> tuple:
    """统计目录下源码文件数和总字节数。

    只统计常见源码扩展名，跳过 node_modules / .build / vendor 等。
    返回 (file_count, total_bytes)。
    """
    if not src_path or not os.path.isdir(src_path):
        return 0, 0

    source_exts = {
        ".ts", ".tsx", ".js", ".jsx",
        ".swift", ".m", ".h",
        ".go",
        ".py",
    }
    skip_dirs = {
        "node_modules", ".build", "vendor", "dist", "build", "__pycache__",
        # Go 标准库 / 工具链源码（常见于 GOROOT 拷贝或 vendor 式项目）
        "go", "testdata",
        # Xcode / Swift Package Manager
        ".swiftpm", "Pods", "DerivedData", "SourcePackages",
        # 通用
        ".git", ".evo-review", "bin", "deploy", "scripts",
    }

    file_count = 0
    total_bytes = 0

    for root, dirs, files in os.walk(src_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            ext = os.path.splitext(f)[1]
            if ext in source_exts:
                file_count += 1
                try:
                    total_bytes += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass

    return file_count, total_bytes


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

    支持 EVO_CONFIG 环境变量覆盖默认路径，方便非标准目录结构的项目使用。

    参数:
        project_root: 项目根目录

    返回:
        解析后的配置字典
    """
    config_path = os.environ.get("EVO_CONFIG") or os.path.join(
        project_root, "test-governance", "config.yaml"
    )
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
