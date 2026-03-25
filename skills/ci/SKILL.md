---
name: ci
description: "CI 验证：按改动范围选择测试粒度，不调用 claude，纯跑 lint/typecheck/test。"
allowed-tools: Bash
---

执行 CI 验证。运行以下命令：

```bash
"${CLAUDE_SKILL_DIR}/../../evo-cli" ci
```
