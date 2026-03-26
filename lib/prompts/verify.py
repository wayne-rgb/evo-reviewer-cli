"""阶段 A 红绿验证 prompt"""

WRITE_TEST_PROMPT = """你需要为以下 bug 编写一个失败测试（红灯）。

## Bug 信息
- ID: {bug_id}
- 文件: {bug_file}:{bug_line}
- 描述: {bug_description}
- 严重性: {bug_severity}
- 测试策略: {test_strategy}

## 要求
1. 测试必须在 bug 未修复时失败（红灯）
2. 测试文件放在对应模块的测试目录
3. 测试名称要清晰描述被测行为
4. 使用 @dimension 标注覆盖的测试维度
5. 不要修改业务代码，只写测试

请先读取 bug 所在文件理解上下文，然后编写测试。"""

WRITE_FIX_PROMPT = """测试已确认 bug 存在（红灯）。现在请修复这个 bug。

## Bug 信息
- ID: {bug_id}
- 文件: {bug_file}:{bug_line}
- 描述: {bug_description}
{cross_module_hint}

## 要求
1. 最小化改动，只修复 bug 本身
2. **如果 bug 涉及多个模块，必须同时修复所有涉及的模块**
3. 不要修改测试文件
4. 不要做额外的重构或改进
5. 修复后测试应该通过（绿灯）

请读取文件并修复。"""

RETRY_FIX_PROMPT = """修复后测试仍然失败。请根据错误信息调整修复。

## Bug 信息
- ID: {bug_id}
- 文件: {bug_file}:{bug_line}

## 测试错误输出
{error_output}

## 要求
1. 分析错误原因
2. 调整修复方案
3. 不要修改测试文件"""

CHECK_REASON_PROMPT = """测试写完后直接就失败了。请判断失败原因是否与声称的 bug 相关。

## 声称的 Bug
- ID: {bug_id}
- 描述: {bug_description}

## 测试输出（最后 50 行）
{test_output}

## 判断标准
- related=true：测试失败确实暴露了声称的 bug
- related=false：测试失败是因为其他原因（语法错误、导入失败、mock 问题等）"""

MUST_FIX_PROMPT = """R3 深度评估已确认以下 bug 真实存在且必须修复。请直接修复并编写验证测试。

## Bug 信息
- ID: {bug_id}
- 文件: {bug_file}:{bug_line}
- 描述: {bug_description}
- R3 判定理由: {eval_reason}
{cross_module_hint}

## 要求
1. 先读取 bug 所在文件理解上下文
2. 修复 bug（最小化改动，只修复 bug 本身）
3. **如果 bug 涉及多个模块，必须同时修复所有涉及的模块**（如发送端和接收端都需要对齐）
4. 编写验证测试（绿灯测试，验证修复后行为正确）
5. 测试文件放在对应模块的测试目录
6. 测试名称要清晰描述被测行为
7. 不要做额外的重构或改进"""

FIX_REGRESSION_PROMPT = """lint 或已有测试失败了。请修复回归问题，不要改变之前的 bug 修复逻辑。

## 错误输出
{error_output}

## 要求
1. 只修复 lint/测试回归，不要改变 bug 修复
2. 如果是 import 问题，修复 import
3. 如果是类型问题，修复类型"""
