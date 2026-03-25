"""
状态机 + JSON 持久化

管理 review 会话的完整生命周期：阶段推进、发现记录、结果持久化。
状态文件存储在 {project_root}/.evo-review/state-{session_id}.json
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class Phase(Enum):
    """Review 流程阶段"""
    BOOTSTRAP = "bootstrap"
    SCOPE = "scope"
    SCAN = "scan"
    ORGANIZE = "organize"
    CONFIRM = "confirm"
    VERIFY = "verify"
    CROSS_VALIDATE = "cross_validate"
    MERGE = "merge"
    INFRA_C1 = "infra_c1"
    INFRA_C2 = "infra_c2"
    REPORT = "report"
    DONE = "done"


class BugStatus(Enum):
    """Bug 验证状态"""
    PENDING = "pending"
    VERIFIED = "verified"
    HALLUCINATION = "hallucination"
    FIX_FAILED = "fix_failed"
    UNVERIFIED = "unverified"
    SKIPPED = "skipped"


@dataclass
class BugResult:
    """单个 bug 的验证结果"""
    status: str  # BugStatus 的值
    reason: str = ""
    test_file: str = ""


@dataclass
class ReviewState:
    """
    Review 会话的完整状态。

    字段说明:
        session_id: 会话 ID，格式 YYYYMMDD-HHMMSS
        command: 命令类型 "review" | "deep"
        phase: 当前阶段（Phase 枚举值）
        scope: 扫描范围（文件列表）
        modules: 涉及的模块列表
        findings: 扫描发现的问题列表
        gaps: 测试覆盖缺口
        worktrees: 工作树映射 {finding_id: worktree_path}
        results: 验证结果 {finding_id: BugResult}
        phase_c1_done: 基础设施 C1 阶段是否完成
        phase_c2_done: 基础设施 C2 阶段是否完成
        overflow: 溢出到下次 review 的低优先级发现
        high_freq_rules: 高频违规规则列表
        hot_files: 问题热点文件列表
        r2_findings: 深度 review R2 阶段发现
        r5_findings: 深度 review R5 阶段发现
    """
    session_id: str
    command: str
    phase: str
    scope: list = field(default_factory=list)
    modules: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    worktrees: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)
    phase_c1_done: bool = False
    phase_c2_done: bool = False
    overflow: list = field(default_factory=list)
    high_freq_rules: list = field(default_factory=list)
    hot_files: list = field(default_factory=list)
    # 边界展开（scope 阶段写入）
    changed_by_module: dict = field(default_factory=dict)   # {模块名: [变更文件列表]}
    boundary_context: dict = field(default_factory=dict)    # {模块名: {boundary_files, counterpart_files, protocols}}
    p0_context: list = field(default_factory=list)          # [{case_id, keyword, scope}]
    # 深度 review 专用
    r2_findings: list = field(default_factory=list)
    r5_findings: list = field(default_factory=list)

    def save(self, path: str) -> None:
        """序列化为 JSON 写入文件。自动创建父目录。"""
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        data = asdict(self)
        # BugResult 在 results 中可能是 dataclass 实例，asdict 已递归处理
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "ReviewState":
        """从 JSON 文件反序列化。"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # results 字段中的值需要还原为 BugResult
        # 只传入 BugResult 已知的字段，忽略多余字段，防止 TypeError
        valid_fields = {"status", "reason", "test_file"}
        results = {}
        for k, v in data.get("results", {}).items():
            if isinstance(v, dict):
                filtered = {fk: fv for fk, fv in v.items() if fk in valid_fields}
                results[k] = BugResult(**filtered)
            else:
                results[k] = v
        data["results"] = results

        return cls(**data)

    def advance(self, phase) -> None:
        """推进到指定阶段。接受 Phase 枚举或字符串。"""
        if isinstance(phase, Phase):
            self.phase = phase.value
        else:
            self.phase = str(phase)

    def state_file(self, project_root: str) -> str:
        """返回当前会话状态文件的完整路径。"""
        return os.path.join(
            self.state_dir(project_root),
            f"state-{self.session_id}.json",
        )

    @staticmethod
    def state_dir(project_root: str) -> str:
        """返回 .evo-review/ 目录的绝对路径。"""
        return os.path.join(project_root, ".evo-review")

    @staticmethod
    def latest_state_path(project_root: str) -> Optional[str]:
        """
        找到最新的状态文件路径。
        按文件名中的 session_id 排序（YYYYMMDD-HHMMSS 天然有序）。
        """
        state_dir = ReviewState.state_dir(project_root)
        if not os.path.isdir(state_dir):
            return None

        state_files = [
            f for f in os.listdir(state_dir)
            if f.startswith("state-") and f.endswith(".json")
        ]
        if not state_files:
            return None

        # 按文件名排序，最后一个就是最新的
        state_files.sort()
        return os.path.join(state_dir, state_files[-1])

    @classmethod
    def new_session(cls, command: str, scope: list, project_root: str = ".") -> "ReviewState":
        """
        创建新的 review 会话。

        参数:
            command: "review" 或 "deep"
            scope: 扫描范围（文件列表）
            project_root: 项目根目录，用于确定状态文件存储位置

        返回:
            初始化的 ReviewState 实例（已保存到文件）
        """
        session_id = time.strftime("%Y%m%d-%H%M%S")
        state = cls(
            session_id=session_id,
            command=command,
            phase=Phase.BOOTSTRAP.value,
            scope=scope,
        )
        # 自动保存
        state_path = os.path.join(
            cls.state_dir(project_root),
            f"state-{session_id}.json",
        )
        state.save(state_path)
        return state
