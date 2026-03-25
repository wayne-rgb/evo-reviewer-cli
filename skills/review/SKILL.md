---
name: review
description: "自进化代码审查：扫描变更文件+跨模块边界检查→红绿验证对抗幻觉→门禁自动进化。用于审查最近 5 个 commit 或指定路径。"
argument-hint: "[路径...]"
allowed-tools: Bash, Read
---

执行自进化代码审查。evo-cli 内部会调用多个 claude 子进程，整个流程可能持续 30+ 分钟，必须后台运行。

## 步骤

1. 后台启动 evo-cli，日志写入文件：

```bash
EVO_CLI="${CLAUDE_SKILL_DIR}/../../evo-cli"
LOG_FILE=".evo-review/review-$(date +%Y%m%d-%H%M%S).log"
mkdir -p .evo-review
nohup "$EVO_CLI" review $ARGUMENTS > "$LOG_FILE" 2>&1 &
EVO_PID=$!
echo "evo-cli 已启动（PID: $EVO_PID），日志：$LOG_FILE"
echo "$EVO_PID" > .evo-review/evo.pid
```

2. 告知用户已启动，然后定期用 `tail` 查看进度：

```bash
tail -30 "$LOG_FILE"
```

3. 检查进程是否还在运行：

```bash
if kill -0 $(cat .evo-review/evo.pid) 2>/dev/null; then echo "仍在运行"; else echo "已完成"; fi
```

4. 流程完成后，读取完整日志展示最终报告。

## 注意
- 阶段 2（确认清单）需要用户交互输入，evo-cli 会在 stdin 等待。后台模式下直接 echo 空行自动全部确认：
  `echo "" | "$EVO_CLI" review $ARGUMENTS` 可跳过交互，但会失去排除 bug 的机会。
- 如果用户需要交互式选择，建议在终端直接运行：`"$EVO_CLI" review`
