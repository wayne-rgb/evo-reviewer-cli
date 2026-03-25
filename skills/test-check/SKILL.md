---
name: test-check
description: "检查测试文件的维度覆盖质量，输出评分卡和缺失维度建议。"
argument-hint: "<测试文件路径>"
allowed-tools: Bash
---

检查测试文件质量。运行以下命令：

```bash
"${CLAUDE_SKILL_DIR}/../../evo-cli" test-check $ARGUMENTS
```
