---
name: review
description: "自进化代码审查：扫描变更文件+跨模块边界检查→红绿验证对抗幻觉→门禁自动进化。用于审查最近 5 个 commit 或指定路径。"
argument-hint: "[路径...]"
allowed-tools: Bash, Read
---

使用 evo-cli 分阶段执行代码审查。每个阶段完成后汇报进度给用户。

## 变量

```bash
EVO_CLI="${CLAUDE_SKILL_DIR}/../../evo-cli"
```

## 流程

### 阶段 1：扫描

后台运行扫描（内部调 claude 子进程，可能 5-10 分钟）：

```bash
"$EVO_CLI" review --until scan $ARGUMENTS
```

用 `run_in_background: true` 执行。完成后读取输出，向用户汇报：
- 发现了多少问题
- 问题按模块和盲区的分布
- 每个 finding 的 ID、严重级别、文件位置、描述

### 阶段 2：用户确认

将扫描结果的确认清单展示给用户，询问：
- **全部确认**：直接进入验证
- **排除部分**：用户指定要排除的 finding ID（如 "排除 F3,F5"）

根据用户选择构造 `--confirmed` 参数。如果用户全部确认，不需要 `--confirmed`。

### 阶段 3：红绿验证

后台运行验证（每个 bug 独立调 claude 写测试+修复，可能 10-30 分钟）：

```bash
# 全部确认
"$EVO_CLI" resume --until verify

# 排除了部分 finding
"$EVO_CLI" resume --confirmed "F1,F2,F4" --until verify
```

用 `run_in_background: true` 执行。完成后向用户汇报：
- 验证通过（verified）的数量和详情
- 幻觉（hallucination）的数量
- 修复失败（fix_failed）的数量

### 阶段 4：收尾

```bash
"$EVO_CLI" resume
```

用 `run_in_background: true` 执行。完成后展示最终报告。

## 注意

- 每个阶段用 `run_in_background: true` 运行，完成后自动收到通知
- 阶段 2（确认）是唯一需要用户交互的环节，在 Claude 侧完成，不需要 evo-cli 的 stdin
- 如果任何阶段失败，读取输出分析原因，向用户汇报后决定是否重试
- 超时建议：scan 600s，verify 1800s，finalize 600s
