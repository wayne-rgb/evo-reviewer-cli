# /deep — 全模块深度审查

将深度审查委托给 evo-review CLI：
1. R1 标准五类扫描（opus）
2. R2 深度扫描（架构/状态机/错误传播）
3. 无 R3（opus 质量足够，不需要筛选步骤）
4. R4 红绿验证
5. R5 轻量化交叉检验（不开 worktree）
6. Phase C 基础设施强化

## 执行

```bash
EVO_REVIEW="${EVO_REVIEW:-$(which evo-review 2>/dev/null || echo './evo-review')}"
$EVO_REVIEW deep $ARGUMENTS
```
