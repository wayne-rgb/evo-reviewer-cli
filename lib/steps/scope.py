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
        "module_a": {
            "module_b": {
                "shared_types": ["module_a/src/types/index.ts"],
                "counterparts": {
                    "module_a/src/types/index.ts": ["module_b/src/Message.swift"],
                },
                "protocols": ["WebSocket"],
            },
            ...
        },
        ...
    }

    topology 文件由 bootstrap 阶段自动生成（多模块项目首次 review 时触发）。
    文件不存在时返回空 dict，不做硬编码推断。
    """
    topo_path = os.path.join(project_root, "test-governance", "cross-module-topology.md")

    if os.path.exists(topo_path):
        return _parse_topology_from_file(topo_path)

    # 没有拓扑文件时返回空（bootstrap 阶段会自动生成，手动运行 evo-cli review 即可触发）
    logger.info("cross-module-topology.md 不存在，跳过边界展开（下次 review 的 bootstrap 会自动生成）")
    return {}


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

    import re

    # 提取文件路径引用（从原始内容中提取，包括反引号内的路径）
    file_refs = re.findall(r'`([^`]+\.[a-zA-Z]{1,5})`', content)
    # 也匹配非反引号包裹的路径
    file_refs += re.findall(r'(?:^|\s)(\S+/\S+\.[a-zA-Z]{1,5})(?:\s|$|[,，。])', content, re.MULTILINE)
    file_refs = list(set(file_refs))

    # 剥离代码块（避免流程图箭头被误匹配为模块对）
    content_no_code = re.sub(r'```[\s\S]*?```', '', content)
    # 剥离反引号（使 "关键模块：`iOS-app` → `togo-agent`" 中的模块名可被匹配）
    content_clean = content_no_code.replace('`', '')

    # 提取模块名对（形如 "模块A → 模块B" 或 "模块A ↔ 模块B"）
    module_pairs = re.findall(r'(\w[\w-]+)\s*[→↔←]\s*(\w[\w-]+)', content_clean)

    # 去重模块对（保留出现顺序）
    seen_pairs = set()
    unique_pairs = []
    for src_mod, dst_mod in module_pairs:
        pair_key = tuple(sorted([src_mod, dst_mod]))
        if pair_key not in seen_pairs:
            seen_pairs.add(pair_key)
            unique_pairs.append((src_mod, dst_mod))

    # 初始化 topology 结构
    for src_mod, dst_mod in unique_pairs:
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

    # 将 file_refs 按路径前缀归属到模块，填充 shared_types 和 counterparts
    # 收集所有出现在 topology 中的模块名
    all_mod_names = set()
    for src_mod, dst_mod in unique_pairs:
        all_mod_names.add(src_mod)
        all_mod_names.add(dst_mod)

    # 按路径前缀将文件归属到模块（如 "togo-agent/src/types/index.ts" → "togo-agent"）
    files_by_module = {}
    for fref in file_refs:
        for mod_name in all_mod_names:
            # 前缀匹配：文件路径以 "模块名/" 开头
            if fref.startswith(mod_name + "/"):
                files_by_module.setdefault(mod_name, []).append(fref)
                break

    # 对每个模块对，双方的文件互为 shared_types / counterparts
    for src_mod, dst_mod in unique_pairs:
        src_files = files_by_module.get(src_mod, [])
        dst_files = files_by_module.get(dst_mod, [])

        if src_files or dst_files:
            # src → dst 方向：src 的文件是 shared_types，dst 的文件是对端
            for sf in src_files:
                if sf not in topology[src_mod][dst_mod]["shared_types"]:
                    topology[src_mod][dst_mod]["shared_types"].append(sf)
                if dst_files:
                    topology[src_mod][dst_mod]["counterparts"][sf] = dst_files

            # dst → src 方向：对称
            for df in dst_files:
                if df not in topology[dst_mod][src_mod]["shared_types"]:
                    topology[dst_mod][src_mod]["shared_types"].append(df)
                if src_files:
                    topology[dst_mod][src_mod]["counterparts"][df] = src_files

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


# ==================== 边界展开 ====================

def _expand_boundaries(changed_by_module, topology, all_modules, project_root):
    """检测变更文件是否触及跨模块边界，如果是则收集对端文件。

    返回 dict：
    {
        "module_name": {
            "boundary_files": ["module_name/src/types/index.ts"],
            "counterpart_files": {
                "module_name/src/types/index.ts": [
                    "peer_module/src/Message.swift"
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


# ==================== 全量边界提取（/deep 专用） ====================

def extract_all_boundaries(project_root):
    """提取所有模块间的边界文件对（不依赖 git diff）。

    用于 /deep 全模块扫描时的 R2 跨模块业务流扫描。
    直接从 topology 提取所有模块对的边界信息。

    返回：
    {
        "module_pairs": [
            {
                "modules": ["backend", "mobile-app"],
                "protocols": ["WebSocket"],
                "shared_files": {
                    "backend/src/types/index.ts": ["mobile-app/src/Message.swift"],
                },
            },
            ...
        ],
        "topology_summary": "模块间通信：backend ↔ mobile-app（WebSocket）；...",
    }
    """
    topology = _parse_topology(project_root)

    module_pairs = []
    seen_pairs = set()  # 避免 A→B 和 B→A 重复

    for src_mod, peers in topology.items():
        for dst_mod, peer_info in peers.items():
            pair_key = tuple(sorted([src_mod, dst_mod]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            shared_types = peer_info.get("shared_types", [])
            counterparts = peer_info.get("counterparts", {})
            protocols = peer_info.get("protocols", [])

            # 合并双向的 shared_files
            shared_files = {}
            for st in shared_types:
                cps = counterparts.get(st, [])
                if cps:
                    shared_files[st] = cps
            # 也检查反向
            reverse_info = topology.get(dst_mod, {}).get(src_mod, {})
            for st in reverse_info.get("shared_types", []):
                cps = reverse_info.get("counterparts", {}).get(st, [])
                if cps and st not in shared_files:
                    shared_files[st] = cps

            if shared_files or protocols:
                module_pairs.append({
                    "modules": list(pair_key),
                    "protocols": protocols,
                    "shared_files": shared_files,
                })

    # 生成拓扑摘要
    if module_pairs:
        summary_parts = []
        for mp in module_pairs:
            mods = " ↔ ".join(mp["modules"])
            protos = ", ".join(mp["protocols"]) if mp["protocols"] else "未知协议"
            summary_parts.append(f"{mods}（{protos}）")
        topology_summary = "模块间通信：" + "；".join(summary_parts)
    else:
        topology_summary = "未检测到模块间通信拓扑"

    return {
        "module_pairs": module_pairs,
        "topology_summary": topology_summary,
    }


# ==================== P0 场景关联 ====================

def _load_related_p0_cases(changed_files, project_root):
    """从 p0-cases.tsv 中找到与变更文件关联的 P0 场景。

    关联规则：
    - p0 case 的 search_scope 列与变更文件的目录前缀匹配
    - p0 case 的 keyword 列在变更文件的文件名或路径中出现

    返回 list[dict]：
    [
        {"case_id": "CASE_ID", "keyword": "keyword", "scope": "module_dir/..."},
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
