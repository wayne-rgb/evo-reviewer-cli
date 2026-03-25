"""追加到项目 CLAUDE.md 的测试治理段落模板"""

TESTING_SECTION = """
## 测试治理（evo-review 自动维护）

### 测试运行策略（铁律）

**禁止随意跑全量测试。** 决策树：
1. 改了 1 个文件 → 只跑该文件对应的单个测试文件
2. 改了 1 个模块内的多个文件 → 只跑该模块的测试（unit 级）
3. 跨模块改动 → 跑集成测试（cross 级）
4. 最终推送前，用户明确要求 → 跑全量测试

### 测试编写的 6 个维度

1. **正常路径** — 核心功能成功执行
2. **副作用清理** — timer/lock/连接是否被正确清理
3. **并发安全** — 多线程/多协程并发操作
4. **错误恢复** — 错误处理后状态一致性
5. **安全边界** — 超大/畸形/恶意输入
6. **故障后可用性** — 故障后仍能处理后续请求

### CI 策略

CI 由 evo-review 驱动：
- `./evo-review ci` — 按改动范围自动选择测试
- `./evo-review review` — 代码审查 + 红绿验证
- `./evo-review deep` — 全模块深度审查

### 门禁

```bash
bash scripts/test-governance-gate.sh preflight  # 静态分析
bash scripts/test-governance-gate.sh trend       # 违规趋势
```
"""
