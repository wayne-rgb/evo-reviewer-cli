# /test-check — 测试维度检查

检查指定测试文件的维度覆盖质量。

## 执行

```bash
EVO_REVIEW="${EVO_REVIEW:-$(which evo-review 2>/dev/null || echo './evo-review')}"
$EVO_REVIEW test-check $ARGUMENTS
```
