"""test-check：检查测试文件的维度覆盖质量"""

import logging
import os
import json

logger = logging.getLogger(__name__)


def run_test_check(test_path, project_root):
    """检查单个测试文件的质量和维度覆盖。"""
    from lib.claude import call_claude_bare
    from lib.schemas.verify import TEST_CHECK_SCHEMA

    if not os.path.exists(test_path):
        # 尝试相对路径
        test_path = os.path.join(project_root, test_path)

    if not os.path.exists(test_path):
        print(f"文件不存在: {test_path}")
        return

    with open(test_path, 'r') as f:
        test_content = f.read()

    # 尝试找到对应的源码文件
    source_content = _find_source(test_path)

    prompt = f"""请分析以下测试文件的质量和维度覆盖。

## 测试文件：{test_path}
```
{test_content}
```

## 对应源码
```
{source_content or '未找到对应源码'}
```

## 维度标准
1=正常路径 2=副作用清理 3=并发安全 4=错误恢复 5=安全边界 6=故障后可用

请评估：
- 覆盖了哪些维度？
- 缺失哪些应该覆盖的维度？
- 测试质量评分（1-10）
- 存在的问题
- 做得好的地方"""

    result = call_claude_bare(
        prompt=prompt,
        model="opus",
        tools="Read,Glob,Grep",
        output_schema=TEST_CHECK_SCHEMA,
        max_turns=10,
        cwd=project_root,
    )

    _print_scorecard(result, test_path)


def _print_scorecard(result, test_path):
    """打印测试质量评分卡"""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            print(result)
            return

    dim_names = {
        1: "正常路径", 2: "副作用清理", 3: "并发安全",
        4: "错误恢复", 5: "安全边界", 6: "故障后可用",
    }

    print(f"\n{'='*60}")
    print(f"测试质量评分卡：{os.path.basename(test_path)}")
    print(f"{'='*60}")

    score = result.get("quality_score", 0)
    bar = "█" * score + "░" * (10 - score)
    print(f"\n质量评分：[{bar}] {score}/10")

    print("\n已覆盖维度：")
    for d in result.get("dimensions_covered", []):
        print(f"  {d}. {dim_names.get(d, '?')}")

    missing = result.get("dimensions_missing", [])
    if missing:
        print("\n缺失维度：")
        for m in missing:
            d = m.get("dimension", 0)
            print(f"  {d}. {dim_names.get(d, '?')} — {m.get('reason', '')}")
            if m.get("suggestion"):
                print(f"     建议：{m['suggestion']}")

    strengths = result.get("strengths", [])
    if strengths:
        print("\n优点：")
        for s in strengths:
            print(f"  - {s}")

    issues = result.get("issues", [])
    if issues:
        print("\n问题：")
        for i in issues:
            print(f"  - {i}")

    print()


def _find_source(test_path):
    """根据测试文件路径推断并读取源码"""
    # TypeScript: src/__tests__/foo.test.ts -> src/*/foo.ts
    base = os.path.basename(test_path)
    if base.endswith('.test.ts'):
        source_name = base.replace('.test.ts', '.ts')
    elif base.endswith('_test.go'):
        source_name = base.replace('_test.go', '.go')
    else:
        return None

    # 在目录树中搜索，限制范围防止遍历整个文件系统
    project_dir = test_path
    for _ in range(5):
        parent = os.path.dirname(project_dir)
        if parent == project_dir:
            break  # 已到根目录，停止上溯
        project_dir = parent

    # 排除的目录
    skip_dirs = {
        "node_modules", ".build", "Build", "DerivedData",
        "__pycache__", ".git", "Pods", ".evo-review",
    }

    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        if source_name in files:
            path = os.path.join(root, source_name)
            if path != test_path:
                try:
                    with open(path, 'r') as f:
                        return f.read()[:5000]  # 限制长度
                except Exception:
                    continue

    return None
