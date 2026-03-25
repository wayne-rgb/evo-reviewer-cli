---
name: deep
description: "全模块深度审查：多轮扫描（R1标准+R2深度）→红绿验证→R5交叉检验→基础设施强化。比 review 更彻底。"
argument-hint: "[模块...]"
allowed-tools: Bash
---

执行全模块深度审查。运行以下命令，等待它完成后将输出展示给用户：

```bash
"${CLAUDE_SKILL_DIR}/../../evo-cli" deep $ARGUMENTS
```
