---
name: cover
description: "分析跨模块测试覆盖缺口并自动生成集成测试，提高跨模块测试覆盖率。"
allowed-tools: Bash
---

分析当前项目的跨模块集成测试覆盖情况，找出未覆盖的边界场景和维度，自动生成测试用例。

运行以下命令：

```bash
"${CLAUDE_SKILL_DIR}/../../evo-cli" cover
```

如果只想覆盖特定模块：

```bash
"${CLAUDE_SKILL_DIR}/../../evo-cli" cover --modules togo-agent,agentapi
```
