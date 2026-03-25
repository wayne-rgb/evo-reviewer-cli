"""阶段 0-2：确定审查范围（含边界展开）"""

import os, logging, subprocess

logger = logging.getLogger(__name__)


def determine_scope(state, project_root, args=None):
    """确定审查范围：哪些模块、哪些文件、哪些跨模块边界。

    逻辑：
    - 有参数 → 参数指定的目录/模块
    - 无参数 → git diff --name-only HEAD~5 归属模块

    新增（边界展开）：
    - 收集 git diff 的具体变更文件列表（按模块分组）
    - 解析 cross-module-topology.md 得到模块间依赖
    - 当变更触及边界文件时，自动展开对端模块的对应文件
    - 解析 p0-cases.tsv 得到与变更关联的 P0 场景

    同时做就绪检查：
    - 模块缺测试命令 → 标记 warning
    - 测试目录为空 → 标记 "只建骨架"
    """
    from lib.config import get_modules
    from lib.git import git_diff_files, files_to_modules

    all_modules = get_modules(project_root)
    changed_files = git_diff_files(n=5, cwd=project_root)

    if args and len(args) > 0:
        # 指定了目录/模块
        scope_paths = args
        target_modules = _match_modules(scope_paths, all_modules)
        # 即使指定了目录，也收集 diff 文件用于聚焦扫描
        module_files = files_to_modules(changed_files, all_modules)
    else:
        # 默认：最近 5 个 commit 的改动文件
        module_files = files_to_modules(changed_files, all_modules)
        modules_by_name = {m.name: m for m in all_modules}
        target_modules = [
            modules_by_name[name] for name in module_files
            if name in modules_by_name  # 排除 "_other"
        ]
        scope_paths = list(set(
            m.src_dir for m in target_modules
        ))

    if not target_modules:
        logger.warning("未找到受影响的模块")
        return [], []

    # 就绪检查
    for m in target_modules:
        warnings = []
        if not m.unit_command:
            warnings.append(f"⚠️ {m.name}: 缺少 unit_command")
        if not m.lint_command:
            warnings.append(f"⚠️ {m.name}: 缺少 lint_command")
        test_dir = os.path.join(project_root, m.test_dir) if m.test_dir else None
        if test_dir and not os.path.isdir(test_dir):
            warnings.append(f"⚠️ {m.name}: 测试目录不存在 ({m.test_dir})")
        for w in warnings:
            logger.warning(w)

    # --- 新增：边界展开 ---

    # 1. 按模块分组变更文件（排除 _other）
    changed_by_module = {
        name: files for name, files in module_files.items()
        if name != "_other"
    }

    # 2. 解析跨模块拓扑，得到边界依赖
    topology = _parse_topology(project_root)

    # 3. 边界展开：检测变更是否触及边界文件，如果是则附上对端文件
    boundary_context = _expand_boundaries(
        changed_by_module, topology, all_modules, project_root
    )

    # 4. 解析 p0-cases.tsv，找到与变更关联的场景
    p0_context = _load_related_p0_cases(changed_files, project_root)

    # 写入 state
    state.modules = [m.name for m in target_modules]
    state.scope = scope_paths
    state.changed_by_module = changed_by_module
    state.boundary_context = boundary_context
    state.p0_context = p0_context

    # 日志
    if boundary_context:
        logger.info(
            "边界展开：%d 个模块有跨模块边界文件",
            len(boundary_context),
        )
    if p0_context:
        logger.info("关联 P0 场景：%d 个", len(p0_context))

    return target_modules, scope_paths


def _match_modules(paths, all_modules):
    """将路径参数匹配到模块"""
    matched = []
    for m in all_modules:
        for p in paths:
            # 路径前缀匹配
            if p.startswith(m.src_dir) or m.src_dir.startswith(p) or m.name == p:
                if m not in matched:
                    matched.append(m)
    return matched


# ==================== 跨模块拓扑解析 ====================

def _parse_topology(project_root):
    """解析 cross-module-topology.md，提取模块间边界依赖。

    返回 dict，结构：
    {
        "togo-agent": {
            "iOS-app": {
                "shared_types": ["togo-agent/src/types/index.ts"],
                "counterparts": {
                    "togo-agent/src/types/index.ts": ["iOS-app/Packages/.../Message.swift"],
                },
                "protocols": ["WebSocket"],
            },
            ...
        },
        ...
    }

    如果 topology 文件不存在，用 config.yaml 的模块信息 + 硬编码的常见边界模式做 fallback。
    """
    topo_path = os.path.join(project_root, "test-governance", "cross-module-topology.md")

    if os.path.exists(topo_path):
        return _parse_topology_from_file(topo_path)

    # fallback：从 config.yaml 推断基本拓扑
    logger.info("cross-module-topology.md 不存在，使用 fallback 拓扑推断")
    return _infer_topology_fallback(project_root)


def _parse_topology_from_file(topo_path):
    """从 cross-module-topology.md 文件解析结构化拓扑。

    该文件是 bootstrap 阶段 claude 生成的自由格式 markdown。
    我们只提取关键信息：模块对之间的共享类型和通信协议。
    """
    topology = {}

    try:
        with open(topo_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return topology

    # 提取文件路径引用（形如 `path/to/file.ext` 或 path/to/file.ext）
    import re
    file_refs = re.findall(r'`([^`]+\.[a-zA-Z]{1,5})`', content)
    # 也匹配非反引号包裹的路径
    file_refs += re.findall(r'(?:^|\s)(\S+/\S+\.[a-zA-Z]{1,5})(?:\s|$|[,，。])', content, re.MULTILINE)
    file_refs = list(set(file_refs))

    # 提取模块名对（形如 "模块A → 模块B" 或 "模块A ↔ 模块B"）
    module_pairs = re.findall(r'(\w[\w-]+)\s*[→↔←]\s*(\w[\w-]+)', content)

    # 将文件引用归属到模块对
    # 这是 best-effort 解析，拓扑文件格式不固定
    for src_mod, dst_mod in module_pairs:
        topology.setdefault(src_mod, {}).setdefault(dst_mod, {
            "shared_types": [],
            "counterparts": {},
            "protocols": [],
        })
        topology.setdefault(dst_mod, {}).setdefault(src_mod, {
            "shared_types": [],
            "counterparts": {},
            "protocols": [],
        })

    # 检测通信协议关键词
    protocol_keywords = {
        "WebSocket": re.compile(r'(?i)websocket|ws://|wss://'),
        "HTTP": re.compile(r'(?i)\bhttp\b|rest\s*api|http://|https://'),
        "IPC": re.compile(r'(?i)\bipc\b|unix\s*socket|named\s*pipe'),
    }
    detected_protocols = []
    for name, pattern in protocol_keywords.items():
        if pattern.search(content):
            detected_protocols.append(name)

    # 为所有模块对添加检测到的协议
    for src_mod in topology:
        for dst_mod in topology[src_mod]:
            topology[src_mod][dst_mod]["protocols"] = detected_protocols

    return topology


def _infer_topology_fallback(project_root):
    """从已知的项目约定推断基本拓扑。

    已知约定（来自 CLAUDE.md）：
    - togo-agent 是中心节点，与 iOS-app/macos-app/voice-tunnel 通信
    - togo-agent/src/types/index.ts 是跨端共享类型的权威来源
    - iOS-app 的 Message.swift 需要和 types/index.ts 保持一致
    """
    topology = {}

    # togo-agent 的类型文件是跨模块边界的核心
    type_file = "togo-agent/src/types/index.ts"
    type_file_path = os.path.join(project_root, type_file)

    if not os.path.exists(type_file_path):
        return topology

    # togo-agent ↔ iOS-app 边界
    ios_counterparts = _find_counterpart_files(
        project_root, "iOS-app/", [".swift"],
        keywords=["Message", "TaskStatus", "CLIType", "MessageType"],
    )

    # togo-agent ↔ macos-app 边界
    mac_counterparts = _find_counterpart_files(
        project_root, "macos-app/", [".swift"],
        keywords=["Message", "ServerMessage", "ClientMessage"],
    )

    if ios_counterparts:
        topology.setdefault("togo-agent", {})["iOS-app"] = {
            "shared_types": [type_file],
            "counterparts": {type_file: ios_counterparts},
            "protocols": ["WebSocket"],
        }
        topology.setdefault("iOS-app", {})["togo-agent"] = {
            "shared_types": ios_counterparts,
            "counterparts": {cp: [type_file] for cp in ios_counterparts},
            "protocols": ["WebSocket"],
        }

    if mac_counterparts:
        topology.setdefault("togo-agent", {})["macos-app"] = {
            "shared_types": [type_file],
            "counterparts": {type_file: mac_counterparts},
            "protocols": ["HTTP"],
        }
        topology.setdefault("macos-app", {})["togo-agent"] = {
            "shared_types": mac_counterparts,
            "counterparts": {cp: [type_file] for cp in mac_counterparts},
            "protocols": ["HTTP"],
        }

    return topology


def _find_counterpart_files(project_root, prefix, extensions, keywords):
    """在指定目录前缀下找包含关键词的对端源文件（确定性搜索，不调 LLM）。

    策略：
    - 只搜索源文件，排除测试文件和构建产物
    - 优先匹配文件名命中的（最可能是类型定义文件）
    - 次优先匹配文件内容中有类型定义关键词的（struct/enum/class + keyword）
    """
    # 排除的目录名
    SKIP_DIRS = {
        "node_modules", ".build", "Build", "DerivedData",
        "__pycache__", ".git", "Pods", "checkouts",
    }
    # 排除的路径段（测试目录）
    SKIP_PATH_SEGMENTS = {"Tests", "Test", "__tests__", "test", "tests"}

    name_matches = []   # 文件名匹配（高优先）
    content_matches = []  # 内容匹配（低优先）

    search_root = os.path.join(project_root, prefix)
    if not os.path.isdir(search_root):
        return []

    for root, dirs, files in os.walk(search_root):
        # 跳过构建产物、缓存、测试目录
        dirs[:] = [
            d for d in dirs
            if d not in SKIP_DIRS and d not in SKIP_PATH_SEGMENTS
        ]

        rel_root = os.path.relpath(root, project_root)
        # 双重检查：路径中不包含测试相关段
        if any(seg in rel_root.split(os.sep) for seg in SKIP_PATH_SEGMENTS):
            continue

        for fname in files:
            if not any(fname.endswith(ext) for ext in extensions):
                continue
            # 跳过测试文件名
            if "Test" in fname and fname != fname.replace("Test", ""):
                continue

            filepath = os.path.join(root, fname)
            rel_path = os.path.relpath(filepath, project_root)

            # 优先级 1：文件名包含关键词
            if any(kw.lower() in fname.lower() for kw in keywords):
                name_matches.append(rel_path)
                continue

            # 优先级 2：文件内容前 30 行包含类型定义 + 关键词
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    head_lines = [f.readline() for _ in range(30)]
                head = "".join(head_lines)
                # 要求同时有类型定义关键词（struct/enum/class/interface/type）和目标关键词
                has_type_def = any(
                    td in head for td in ["struct ", "enum ", "class ", "interface ", "type "]
                )
                has_keyword = any(kw in head for kw in keywords)
                if has_type_def and has_keyword:
                    content_matches.append(rel_path)
            except Exception:
                pass

    # 文件名匹配优先，内容匹配补充，总数限制 10
    results = name_matches[:10]
    remaining = 10 - len(results)
    if remaining > 0:
        results.extend(content_matches[:remaining])

    return results


# ==================== 边界展开 ====================

def _expand_boundaries(changed_by_module, topology, all_modules, project_root):
    """检测变更文件是否触及跨模块边界，如果是则收集对端文件。

    返回 dict：
    {
        "togo-agent": {
            "boundary_files": ["togo-agent/src/types/index.ts"],
            "counterpart_files": {
                "togo-agent/src/types/index.ts": [
                    "iOS-app/Packages/.../Message.swift"
                ],
            },
            "protocols": ["WebSocket"],
        },
        ...
    }
    """
    boundary_context = {}

    for module_name, files in changed_by_module.items():
        if module_name not in topology:
            continue

        module_boundaries = topology[module_name]

        for peer_module, peer_info in module_boundaries.items():
            shared_types = peer_info.get("shared_types", [])
            counterparts = peer_info.get("counterparts", {})
            protocols = peer_info.get("protocols", [])

            # 检查变更文件是否触及共享类型
            for changed_file in files:
                for shared_file in shared_types:
                    # 路径前缀匹配（变更文件可能是 shared_file 本身或其子路径）
                    if changed_file == shared_file or changed_file.startswith(shared_file.rsplit("/", 1)[0] + "/"):
                        # 命中边界！收集对端文件
                        peer_files = counterparts.get(shared_file, [])
                        if peer_files:
                            ctx = boundary_context.setdefault(module_name, {
                                "boundary_files": [],
                                "counterpart_files": {},
                                "protocols": [],
                            })
                            if changed_file not in ctx["boundary_files"]:
                                ctx["boundary_files"].append(changed_file)
                            # 合并对端文件（同一变更文件可能对应多个对端模块）
                            existing = ctx["counterpart_files"].get(changed_file, [])
                            for pf in peer_files:
                                if pf not in existing:
                                    existing.append(pf)
                            ctx["counterpart_files"][changed_file] = existing
                            for p in protocols:
                                if p not in ctx["protocols"]:
                                    ctx["protocols"].append(p)

    return boundary_context


# ==================== P0 场景关联 ====================

def _load_related_p0_cases(changed_files, project_root):
    """从 p0-cases.tsv 中找到与变更文件关联的 P0 场景。

    关联规则：
    - p0 case 的 search_scope 列与变更文件的目录前缀匹配
    - p0 case 的 keyword 列在变更文件的文件名或路径中出现

    返回 list[dict]：
    [
        {"case_id": "PAIRING_KEY_NIL_CLEARS_LOCAL", "keyword": "geminiApiKey", "scope": "iOS-app/..."},
        ...
    ]
    """
    p0_path = os.path.join(project_root, "test-governance", "p0-cases.tsv")
    if not os.path.exists(p0_path):
        return []

    cases = []
    try:
        with open(p0_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []

    # 跳过注释行和表头
    data_lines = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]

    # 将变更文件拼成一个搜索字符串
    changed_str = " ".join(changed_files)
    # 也提取变更文件的目录前缀集合
    changed_dirs = set()
    for f in changed_files:
        parts = f.split("/")
        for i in range(1, len(parts)):
            changed_dirs.add("/".join(parts[:i]))

    for line in data_lines:
        parts = line.split("\t")
        if len(parts) < 2:
            continue

        case_id = parts[0].strip()
        keyword = parts[1].strip()
        scope = parts[2].strip() if len(parts) > 2 else ""

        # 关联条件 1：scope 目录与变更文件目录匹配
        scope_match = False
        if scope:
            for cd in changed_dirs:
                if cd.startswith(scope) or scope.startswith(cd):
                    scope_match = True
                    break

        # 关联条件 2：keyword 出现在变更文件路径中
        keyword_match = keyword.lower() in changed_str.lower()

        if scope_match or keyword_match:
            cases.append({
                "case_id": case_id,
                "keyword": keyword,
                "scope": scope,
            })

    return cases
