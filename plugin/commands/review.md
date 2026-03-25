# /review — 自进化代码审查

将审查委托给 evo-review CLI，它会：
1. 用 opus 扫描代码（无 sonnet 幻觉）
2. 过滤语言运行时不可能的 bug
3. 按测试体系缺口归类
4. 红绿验证每个 bug（测试先红后绿）
5. 合并修复、写 gate 规则、更新文档

## 执行

```bash
# 检测 evo-review CLI 路径
EVO_REVIEW="${EVO_REVIEW:-$(which evo-review 2>/dev/null || echo './evo-review')}"

# 参数透传
$EVO_REVIEW review $ARGUMENTS
```

如果 CLI 不存在，提示用户安装：
```
evo-review CLI 未找到。请将 evo-review-cli/ 加入 PATH：
  export PATH="$PATH:/path/to/evo-review-cli"
  chmod +x /path/to/evo-review-cli/evo-review
```
