---
name: deep
description: "全模块深度审查：多轮扫描（R1标准+R2深度）→R3深度评估→红绿验证→R5交叉检验→基础设施强化。比 review 更彻底。"
argument-hint: "[模块...]"
allowed-tools: Bash, Read
---

使用 evo-cli 分阶段执行全模块深度审查。每个阶段完成后汇报进度给用户。

## 变量

```bash
EVO_CLI="${CLAUDE_SKILL_DIR}/../../evo-cli"
```

## 流程

### 阶段 1：双轮扫描

后台运行扫描（R1 标准 + R2 深度，可能 10-20 分钟）：

```bash
"$EVO_CLI" deep --until scan $ARGUMENTS
```

用 `run_in_background: true` 执行。完成后向用户汇报：
- R1 和 R2 分别发现了多少问题
- 问题按模块和盲区的分布
- 每个 finding 的 ID、严重级别、文件位置、描述

### 阶段 2：用户确认扫描结果

将确认清单展示给用户，询问：
- **全部确认**：直接进入 R3 深度评估
- **排除部分**：用户指定要排除的 finding ID

### 阶段 3：R3 深度评估

后台运行 R3 深度评估（opus 读代码判定每个 finding 是否值得修，可能 5-15 分钟）：

```bash
# 全部确认
"$EVO_CLI" resume --until evaluate

# 排除了部分
"$EVO_CLI" resume --confirmed "F1,F2,F4" --until evaluate
```

用 `run_in_background: true` 执行。完成后向用户汇报每个 finding 的评估结果：
- **must_fix**：真实可触发 + 用户可感知影响 → 必须进入红绿验证
- **verify**：影响不确定 → 需要红绿测试确认
- **skip**：触发条件极端 / 有上层保护 → 跳过，不进入红绿验证

展示评估明细表（ID、判定、触发概率、原因），让用户确认后再进入 R4。

### 阶段 4：用户确认评估结果

将 R3 评估明细展示给用户，按 finding 逐个列出：
- ID、原始严重级别 → R3 重评严重级别
- 判定（must_fix / verify / skip）
- 触发概率、触发场景
- 判定理由

询问用户：
- **全部确认**：按 R3 建议执行（skip 的不验证，must_fix + verify 进入 R4）
- **调整**：用户指定要覆盖的 finding ID（如"把 F3 也加回来验证"、"F7 不用验了"）

记录用户的调整决定，用于阶段 5 的 `--confirmed` 参数。

### 阶段 5：红绿验证 + 交叉检验

后台运行验证（R4 红绿 + R5 交叉，可能 15-40 分钟）：

```bash
# 用户全部确认 R3 结果（按 R3 建议，只验证 must_fix + verify）
"$EVO_CLI" resume --until verify

# 用户调整了 R3 结果（显式指定要验证的 ID，覆盖 R3 的 skip 判定）
"$EVO_CLI" resume --confirmed "F1,F3,F5,F7" --until verify
```

**`--confirmed` 的语义**：当从 evaluate 阶段 resume 时，`--confirmed` 指定的 ID 会覆盖 R3 的 skip 判定——被 R3 标记为 skip 但出现在 `--confirmed` 中的 finding 会恢复为待验证。未出现在 `--confirmed` 中的 skip finding 保持跳过。

用 `run_in_background: true` 执行。完成后汇报验证结果。

### 阶段 6：收尾

```bash
"$EVO_CLI" resume
```

用 `run_in_background: true` 执行。完成后展示最终报告。

## 注意

- 每个阶段用 `run_in_background: true` 运行，完成后自动收到通知
- deep 比 review 多 R3（深度评估）和 R5（交叉检验）两步
- 阶段 2 和阶段 4（用户确认）在 Claude 侧完成交互
- R3 的价值：在进入昂贵的红绿验证（每个 finding ~50K token）前过滤低价值 findings，节省成本
- 超时建议：scan 1200s，evaluate 600s，verify 2400s，finalize 600s
