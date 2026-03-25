# /ci — CI 验证

纯 CLI 代码，不调 claude。根据 git diff 确定改动范围，运行对应模块的检查。

## 执行

```bash
EVO_REVIEW="${EVO_REVIEW:-$(which evo-review 2>/dev/null || echo './evo-review')}"
$EVO_REVIEW ci
```
