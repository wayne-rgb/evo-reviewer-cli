"""阶段 C 基础设施 prompt"""

PHASE_C1_PROMPT = """请根据已验证的 bug 编写 gate 规则和测试 helper。

## 已验证的 bug
{verified_bugs_json}

## 项目配置
{config_json}

## 任务
1. 在 scripts/test-governance-gate.sh 中添加能检测同类问题的 gate 规则
2. 在对应模块的 helper 目录添加测试辅助工具（如资源泄漏检测器）
3. 更新 test-governance/infrastructure.md 注册新增项
4. 更新 test-governance/coding-guidelines.md 添加 ❌/✅ 对比示例

## 规则编写要求
- 规则 ID 格式：R{{N}}-{{描述}}，N 接续现有最大编号
- 每个规则必须有 log_violation 调用
- severity 为 BLOCK 或 WARN"""

PHASE_C2_PROMPT = """请更新文档、处理违规趋势、清理存量问题。

## 已验证的 bug
{verified_bugs_json}

## 违规趋势
{trend_output}

## 高频违规规则（≥10 次）
{high_freq_rules}

## 任务
1. 更新 test-governance/infrastructure.md（注册新 helper/规则）
2. 如有高频违规 → 在 coding-guidelines.md 添加源头治理示例
3. 更新 test-governance/dimension-coverage.yaml（新测试维度）
4. 清理存量：如果有明显的存量问题可以顺手修复，修复后跑单元测试验证"""

CONSTRAINTS_PROMPT = """根据已验证的 bug，提取应写入 CLAUDE.md 的架构约束。

## 已验证的 bug
{verified_bugs_json}

## 判断标准
- 只提取跨模块的、架构级的约束
- 不提取单文件的 bug 修复（那些已经通过测试覆盖了）
- 约束应该能防止同类问题再次发生

如果没有值得写入的约束，返回空数组。"""

CROSS_SCAN_PROMPT = """你是代码审查专家，正在进行交叉检验。

## 已修复的 bug（不要重复）
{verified_summary}

## 新增的测试模式
{test_patterns}

## 检查重点
1. 修复是否引入新问题
2. 新测试是否有漏洞（比如 mock 过度导致不测真实行为）
3. 类似模式在其他文件中是否存在

如果没有发现，返回空数组。"""

BOOTSTRAP_SCAN_PROMPT = """扫描项目结构，生成模块配置。

请检查以下目录的项目结构：
- 查找 package.json / go.mod / *.xcodeproj 等项目文件
- 确定每个模块的语言、源码目录、测试目录
- 推断测试命令

输出 JSON 格式的模块配置。"""
