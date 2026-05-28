"""R5 交叉检验三件套的 prompt 模板。

R5-A 静态调用方分析不用 LLM,无 prompt。
R5-B 同模式候选 + R5-C adversarial 输入用此处 prompt。

设计原则:
- 每个调用极轻量(30s timeout,3 turns),失败一个不影响其他
- schema 强制具体证据/具体值,禁止"建议加更多 case"这种空话
"""

# ===== R5-B 同模式候选检测 =====
SIMILAR_PATTERN_PROMPT = """检查这段代码是否存在与已知 bug 同类问题(category 相同的反模式)。

【已知 bug】
ID: {bug_id}
类别: {bug_category}
位置: {bug_file}:{bug_line}
描述: {bug_description}

【候选代码片段】
文件: {candidate_file}
内容(前 60 行):
```
{candidate_snippet}
```

【任务】
判断候选代码是否存在与已知 bug **同类**(category 相同) 的问题。
不是问"代码质量好不好",而是问"有没有同款反模式"。

判定标准:
- yes:  候选代码有相同的反模式 / 漏洞模式,需要相同的修复手法
- no:   候选代码不存在该类问题(可能完全无关 / 已正确处理)
- uncertain: 候选代码片段不足以判断

【输出】
仅 JSON,字段:
  verdict (yes/no/uncertain)
  reason (≤200 字,必须给出具体的行号/代码片段证据,不准空泛)
"""


# ===== R5-C adversarial 测试输入 =====
ADVERSARIAL_PROMPT = """看这个红绿测试文件,作为攻击者/异常路径设计者,给出 3 个能"绕过"测试的输入。

【已修复的 bug】
ID: {bug_id}
位置: {bug_file}:{bug_line}
描述: {bug_description}

【测试文件内容】
文件: {test_file}
```
{test_content}
```

【任务】
设计 3 个具体输入,满足:
1. 仍然能触发 bug 描述里那一类问题(同根因)
2. 但**不会被当前测试用例覆盖**(测试存在覆盖盲区)

换句话说:测试现在能拦住"已知输入",但有哪些"未知输入"还能让 bug 复现。

【硬性要求】
- input 字段必须是**具体值**(数字 / 字符串 / JSON / curl 命令 / 代码片段),禁止文字描述
  ❌ 错:"超大输入"        ✅ 对:"5MB 的纯 0xFF 字节流"
  ❌ 错:"非法字符"        ✅ 对:"\\x00\\xFF\\xFE\\xFF 这四个字节"
  ❌ 错:"恶意 JSON"       ✅ 对:'{{"__proto__":{{"isAdmin":true}}}}'
- why_bypass 必须解释当前测试的具体哪一行/哪一个 case 拦不住这个输入
- 禁止输出"建议增加更多 case" / "建议覆盖边界" 这种空话

【输出】
仅 JSON,字段 adversarial_inputs (3 个对象):
  label (短标签,≤20 字)
  input (具体值,见上)
  why_bypass (≤150 字,引用测试文件具体行/case 名)
"""
