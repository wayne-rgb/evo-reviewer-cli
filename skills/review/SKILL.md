---
name: review
description: "自进化代码审查：扫描变更文件+跨模块边界检查→红绿验证对抗幻觉→门禁自动进化。用于审查最近 5 个 commit 或指定路径。"
argument-hint: "[路径...]"
allowed-tools: Bash
---

执行自进化代码审查。运行以下命令，等待它完成后将输出展示给用户：

```bash
"${CLAUDE_SKILL_DIR}/../../evo-cli" review $ARGUMENTS
```

如果命令不存在或执行失败，检查：
1. Python 3 是否可用：`python3 --version`
2. `claude` CLI 是否可用：`which claude`
3. 当前目录是否是 git 仓库
