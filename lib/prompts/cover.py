"""cover 命令的 prompt 模板"""

# Phase 1：分析现有跨模块测试覆盖，输出缺口清单
ANALYZE_COVERAGE_PROMPT = """你是跨模块集成测试专家。请分析当前项目的跨模块测试覆盖情况，找出所有覆盖缺口。

## 项目模块
{modules_info}

## 模块间通信拓扑
{topology_summary}

## P0 场景（必须覆盖）
{p0_cases}

## 现有跨模块测试文件及场景
{existing_tests}

## 现有测试 helper 能力
{helpers_summary}

{trend_weaknesses}

## 分析方法

### 第一步：构建覆盖矩阵
列出所有模块边界对（如 websocket↔cli、http↔config、bot↔cli 等），
对每个边界对检查 6 个测试维度的覆盖情况：
1. **happy_path** — 正常业务流能走通
2. **cleanup** — 连接断开/实例销毁后资源清理
3. **concurrency** — 多设备/多请求并发操作
4. **error_recovery** — 网络断开、API 失败、消息格式错误后的恢复
5. **security_boundary** — 未认证访问、畸形消息、越权操作
6. **fault_tolerance** — 重连后状态恢复、部分失败不影响整体

### 第二步：与 P0 场景交叉
检查每个 P0 场景是否在现有测试中被覆盖（包括边界状态）。

### 第三步：读源码确认
对于不确定是否覆盖的场景，Read 对应的源码和测试文件确认。
特别关注：
- 源码中的 catch/finally/error handler 是否被测试
- 状态机的所有转换路径是否被测试
- 广播消息是否在所有接收端被验证

### 第四步：输出缺口清单
按优先级排序：
- P0：P0 场景未覆盖或覆盖不完整
- P1：模块边界对完全无测试
- P2：已有测试但缺少重要维度（error_recovery、concurrency、fault_tolerance）

每个缺口必须包含：
- 具体的用户场景描述（不是抽象的"测试并发"，而是"两个 iOS 设备同时修改同一个 CLI 配置"）
- 测试实现提示：用哪些 helper、mock 什么、断言什么
- 相关源码文件路径

## 输出要求

### coverage_matrix（必须）
输出完整的覆盖矩阵：列出所有识别到的模块边界对，每个边界对标注 6 个维度的覆盖状态（true/false）。
这个矩阵应该反映你实际读代码确认的结果，不要猜测。

### gaps（必须）
- 不要报告已有测试已经覆盖的场景
- 不要报告纯单元测试范畴的问题（那是 test-check 的职责）
- 每个缺口的 scenario 必须足够具体，能直接据此写测试
- test_hint 必须引用项目中已有的 helper 函数名（如果有）
- 如果趋势数据显示某个 category 幻觉率高，优先为该 category 生成更精确的测试场景

### coverage_summary（必须）
汇总统计：总边界对数、已覆盖数、现有测试文件数、各维度覆盖数。"""

# Phase 2：为单个缺口生成集成测试
GENERATE_TEST_PROMPT = """请为以下跨模块测试缺口编写集成测试。

## 缺口信息
- ID: {gap_id}
- 模块边界: {module_pair}
- 场景: {scenario}
- 维度: {dimension}
- 优先级: {priority}
- 实现提示: {test_hint}

## 现有测试模式参考
{test_pattern_example}

## 可用的测试 Helper
{helpers_available}

## 要求
1. 测试文件放在项目的跨模块测试目录（参考现有 cross-module-*.test.ts 的位置）
2. 文件名格式：cross-module-cover-{gap_id_lower}.test.ts
3. 遵循现有测试模式：
   - 使用 createTestApp() 创建隔离测试实例
   - 使用 createAuthenticatedClient() 创建已认证 WS 客户端
   - afterEach 中清理所有连接和资源
4. 测试必须是**绿灯测试**（验证当前代码行为正确）
5. 充分覆盖边界状态：不只是 happy path，还要测错误输入、超时、并发
6. 使用 describe/it 结构，测试名称清晰描述场景
7. 如果场景涉及故障注入，使用 createFailAfterNMock 等 helper
8. 如果场景涉及广播验证，使用 assertBroadcastToAll 等 helper

请先 Read 相关源码和已有测试文件理解上下文，然后编写测试。"""

# 测试失败后修复
FIX_TEST_PROMPT = """刚写的集成测试运行失败了。请根据错误信息修复测试代码。

## 测试场景
- 缺口: {gap_id} — {scenario}

## 错误输出（最后 40 行）
{error_output}

## 要求
1. 分析失败原因：是测试逻辑问题还是对业务代码理解有误
2. 只修改测试文件，不要修改业务代码
3. 如果是异步时序问题，增加适当的等待或重试
4. 如果是 mock/helper 使用不当，参考现有测试的用法修正"""
