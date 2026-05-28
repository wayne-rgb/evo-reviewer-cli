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


# ===== C-1 分子化:每个 verified bug 独立调用 =====
PHASE_C1_SINGLE_PROMPT = """根据**这一个**已验证的 bug 编写 gate 规则(必要时附 helper)。

## bug (只处理这一个)
{bug_json}

## 项目配置
{config_json}

## 任务(只做这一个 bug,不要扩散到其他 finding 或别的功能)
1. 在 scripts/test-governance-gate.sh 中添加 **1 条** 规则,检测同类问题
   - 规则 ID 格式: R{{N}}-{{简短描述}},N 接续现有最大编号
   - 必须有 log_violation 调用
   - severity = BLOCK 或 WARN
2. 如果规则确实需要 helper(资源检测器 / 测试夹具),在对应模块的 helper 目录添加
3. 如果有新基础设施,**追加 1-2 行** 到 test-governance/infrastructure.md 注册
4. 如果该 bug 适合做反例,**追加** 1 小段到 test-governance/coding-guidelines.md (❌/✅ 对比)

## 硬性约束
- 只处理这一个 bug
- 不要改业务代码 / 测试代码 / 其他模块的文件
- 不要重写已有 gate 规则,只追加
- 完成后输出做了哪些改动(简短列表),不要长篇分析

## 预算
- 最多 10 turns,180 秒
- 完不成就只完成第 1 项(最重要),其余跳过
"""

# 单 finding 失败后,跑完所有 finding 再做一次 preflight 修复
PHASE_C1_PREFLIGHT_FIX_PROMPT = """preflight 检查失败。请读取错误输出修复 gate 规则。

**硬性约束:不要改业务代码,不要扩散到其他文件,只修 gate 规则本身。**

最多 8 turns。完不成就只修最关键的错误。
"""

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

# 注意：BOOTSTRAP_SCAN_PROMPT 的权威定义在 lib/prompts/scan.py 中。
# bootstrap.py import 的是 scan.py 的版本。此处不再重复定义。
