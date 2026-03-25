---
name: deep
description: "全模块深度审查：多轮扫描（R1标准+R2深度）→红绿验证→R5交叉检验→基础设施强化。比 review 更彻底。"
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

### 阶段 2：用户确认

将确认清单展示给用户，询问：
- **全部确认**：直接进入验证
- **排除部分**：用户指定要排除的 finding ID

### 阶段 3：红绿验证 + 交叉检验

后台运行验证（R4 红绿 + R5 交叉，可能 15-40 分钟）：

```bash
# 全部确认
"$EVO_CLI" resume --until verify

# 排除了部分
"$EVO_CLI" resume --confirmed "F1,F2,F4" --until verify
```

用 `run_in_background: true` 执行。完成后汇报验证结果。

### 阶段 4：收尾

```bash
"$EVO_CLI" resume
```

用 `run_in_background: true` 执行。完成后展示最终报告。

## 注意

- 每个阶段用 `run_in_background: true` 运行，完成后自动收到通知
- deep 的 verify 阶段包含 R5 交叉检验，比 review 多一步
- 阶段 2（确认）在 Claude 侧完成用户交互
- 超时建议：scan 1200s，verify 2400s，finalize 600s
