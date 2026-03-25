---
name: deep
description: "全模块深度审查：多轮扫描（R1标准+R2深度）→红绿验证→R5交叉检验→基础设施强化。比 review 更彻底。"
argument-hint: "[模块...]"
allowed-tools: Bash, Read
---

执行全模块深度审查。evo-cli 内部会调用多个 claude 子进程，整个流程可能持续 60+ 分钟，必须后台运行。

## 步骤

1. 后台启动 evo-cli，日志写入文件：

```bash
EVO_CLI="${CLAUDE_SKILL_DIR}/../../evo-cli"
LOG_FILE=".evo-review/deep-$(date +%Y%m%d-%H%M%S).log"
mkdir -p .evo-review
nohup "$EVO_CLI" deep $ARGUMENTS > "$LOG_FILE" 2>&1 &
EVO_PID=$!
echo "evo-cli deep 已启动（PID: $EVO_PID），日志：$LOG_FILE"
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
- 阶段 2（确认清单）需要用户交互输入。后台模式下 stdin 关闭会自动跳过确认（全部接受）。
- 如果用户需要交互式排除特定 bug，建议在终端直接运行。
