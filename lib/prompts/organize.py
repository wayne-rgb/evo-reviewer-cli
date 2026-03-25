"""阶段 1.5 盲区归类 prompt"""

ORGANIZE_PROMPT = """你是测试架构专家。请将以下代码审查发现按测试体系缺口归类。

## 所有发现
{findings_json}

## 归类原则
- 不按 bug 组织，按"测试体系缺口"组织
- 同类问题（如"缺少 setInterval 泄漏检测"）归入同一个 gap
- 每个 gap 说明需要什么基础设施来自动抓住这类问题
- 单独的、不属于任何缺口的 bug 也要列出（gap_name 为 bug 本身描述）

## 输出格式
JSON，包含 gaps 数组。"""
