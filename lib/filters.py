"""
语言运行时 impossible-bug 过滤器

根据语言运行时特性过滤不可能存在的 bug。
例如 Node.js 是单线程事件循环，不存在线程级并发 bug（data_race/deadlock/mutex）。
但异步逻辑竞态和资源泄漏仍然是真实问题。

使用方式：
1. is_impossible(finding, language) — 判断某个发现是否是该语言中不可能的 bug
2. get_runtime_facts(language) — 获取运行时约束文本，注入到扫描 prompt 中
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# ==================== 语言运行时约束 ====================
# 键为语言标识，值为在该语言中不可能存在的 bug 类别列表
IMPOSSIBLE_CATEGORIES = {
    "typescript": [
        # 仅过滤线程级并发（Node.js 单线程），保留异步逻辑竞态
        "toctou",           # 检查-使用竞态（需要线程）
        "data_race",        # 数据竞争（线程级）
        "deadlock",         # 死锁
        "mutex",            # 互斥锁
    ],
    "javascript": [
        # 与 TypeScript 相同
        "toctou", "data_race", "deadlock", "mutex",
    ],
    "swift_mainactor": [
        # @MainActor 修饰的类自动隔离到主线程
        "actor_isolation",  # actor 隔离问题
        "data_race",        # 数据竞争
    ],
}

# ==================== 关键词匹配模式 ====================
# 用于从 finding 的描述文本中检测 bug 类别
KEYWORD_PATTERNS = {
    "concurrency": re.compile(
        r"(?i)(concurrent|thread|race\s*condition|toctou|mutex|deadlock|data\s*race)"
    ),
    "thread_safety": re.compile(
        r"(?i)(thread.safe|thread.unsafe|synchroniz)"
    ),
    "actor_isolation": re.compile(
        r"(?i)(actor\s*isolation|sendable|@MainActor\s*violation)"
    ),
    "data_race": re.compile(
        r"(?i)(data\s*race|concurrent\s*access|simultaneous\s*access)"
    ),
    "deadlock": re.compile(
        r"(?i)(deadlock|dead\s*lock|circular\s*wait)"
    ),
    "mutex": re.compile(
        r"(?i)(mutex|lock\s*contention|spinlock)"
    ),
    "toctou": re.compile(
        r"(?i)(toctou|time.of.check|check.then.act)"
    ),
}

# ==================== 语言运行时事实（注入 prompt） ====================
# 这些文本会被插入到扫描 prompt 中，引导 LLM 在扫描时就遵守运行时约束
RUNTIME_FACTS = {
    "typescript": """语言运行时约束（扫描时必须遵守，违反此约束的发现一律不报告）：
- TypeScript/Node.js：单线程事件循环，不存在线程级并发/TOCTOU/竞态
- 异步操作（Promise/async-await）可能有逻辑竞态，但不是线程竞态
- setInterval/setTimeout 泄漏是真实问题，属于 resource_leak 不是 concurrency
- EventEmitter listener 泄漏是真实问题，属于 resource_leak""",

    "javascript": """语言运行时约束（扫描时必须遵守，违反此约束的发现一律不报告）：
- JavaScript/Node.js：单线程事件循环，不存在线程级并发/TOCTOU/竞态
- 异步操作可能有逻辑竞态，但不是线程竞态
- setInterval/setTimeout 泄漏是真实问题，属于 resource_leak 不是 concurrency""",

    "go": """语言运行时约束：
- Go goroutine 泄漏需要有明确的无退出机制证据（如无 context/done channel）
- 有 defer close 的资源不算泄漏
- sync.Mutex 使用需要看是否真的有并发路径
- channel 方向（send-only / receive-only）由类型系统保证，不需要运行时检查""",

    "swift": """语言运行时约束：
- Swift @MainActor 类：属性访问自动隔离到主线程，不存在 actor 隔离问题
- 非 @MainActor 的类才可能有并发问题
- DispatchQueue.main.async 是安全的隔离模式
- Combine Publisher 在 receive(on:) 指定的调度器上回调，非线程安全问题""",

    "python": """语言运行时约束：
- CPython 有 GIL，纯 Python 代码不会有真正的数据竞争
- 但 I/O 操作期间 GIL 会释放，涉及共享状态的 I/O 回调可能有竞态
- threading.Lock 在纯 CPU 场景下通常不必要
- asyncio 是单线程协程，不存在线程级并发问题""",

    "rust": """语言运行时约束：
- Rust 的所有权系统和借用检查器在编译期消除了数据竞争
- Send/Sync trait 由编译器强制，不需要运行时检查
- unsafe 块内的代码不受以上保证，需要人工审查""",
}


def is_impossible(finding: dict, module_language: str) -> bool:
    """
    判断一个 finding 是否是该语言中不可能存在的 bug。

    通过两种策略检测：
    1. finding 的 category 字段直接匹配不可能类别
    2. finding 的 description 字段包含不可能类别的关键词

    参数:
        finding: 扫描发现的 bug 信息字典，期望包含以下字段：
            - id: bug 标识
            - category: bug 类别（如 "concurrency"）
            - description: bug 描述文本
        module_language: 模块的编程语言（如 "typescript", "go", "swift"）

    返回:
        True 表示该 bug 在此语言中不可能存在（应过滤掉）
    """
    lang = module_language.lower()
    impossible = IMPOSSIBLE_CATEGORIES.get(lang, [])
    if not impossible:
        return False

    finding_id = finding.get("id", "<unknown>")
    category = finding.get("category", "").lower()
    description = finding.get("description", "").lower()

    # 策略 1：类别直接匹配
    for imp in impossible:
        if imp in category:
            logger.info(
                "过滤不可能的 bug: %s — %s 不存在 %s 类问题",
                finding_id, lang, imp,
            )
            return True

    # 策略 2：描述关键词匹配
    for imp in impossible:
        pattern = KEYWORD_PATTERNS.get(imp)
        if pattern and pattern.search(description):
            logger.info(
                "过滤不可能的 bug: %s — 描述含 %s 关键词（语言: %s）",
                finding_id, imp, lang,
            )
            return True

    return False


def filter_findings(findings: list, module_language: str) -> tuple:
    """
    批量过滤 findings，返回（保留的, 被过滤的）两个列表。

    参数:
        findings: 扫描发现列表
        module_language: 模块语言

    返回:
        (kept, filtered) — 保留的发现列表和被过滤的发现列表
    """
    kept = []
    filtered = []
    for f in findings:
        if is_impossible(f, module_language):
            filtered.append(f)
        else:
            kept.append(f)

    if filtered:
        logger.info(
            "语言过滤器: %d 个发现中过滤了 %d 个不可能的 bug（语言: %s）",
            len(findings), len(filtered), module_language,
        )

    return kept, filtered


def get_runtime_facts(language: str) -> str:
    """
    获取语言运行时事实文本，用于注入到扫描 prompt 中。

    参数:
        language: 编程语言名称

    返回:
        运行时约束描述文本，不支持的语言返回空字符串
    """
    return RUNTIME_FACTS.get(language.lower(), "")


def get_supported_languages() -> list:
    """返回所有支持运行时约束过滤的语言列表"""
    return sorted(set(list(IMPOSSIBLE_CATEGORIES.keys()) + list(RUNTIME_FACTS.keys())))
