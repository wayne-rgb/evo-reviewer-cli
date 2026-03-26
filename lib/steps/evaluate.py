"""R3：深度评估 — 在红绿验证之前过滤低价值 findings，节省 token"""

import logging
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


def run_evaluate(state, project_root, confirmed_ids, modules_by_name):
    """对已确认的 findings 做深度评估，返回值得红绿验证的 finding IDs。

    按模块并行，opus 读代码后判定每个 finding 是 must_fix / verify / skip。
    skip 的 findings 记录到 state.results（不进入红绿验证），节省后续 token。

    Args:
        confirmed_ids: 确认的 finding ID 列表
        modules_by_name: {name: ModuleConfig} 映射

    Returns:
        值得红绿验证的 finding ID 列表
    """
    from lib.claude import call_claude_bare
    from lib.prompts.evaluate import EVALUATE_PROMPT
    from lib.schemas.evaluate import EVALUATE_SCHEMA

    confirmed = [f for f in state.findings if f["id"] in confirmed_ids]
    if not confirmed:
        return []

    # 按模块分组
    by_module = {}
    for f in confirmed:
        by_module.setdefault(f.get("module", "unknown"), []).append(f)

    results = {}  # finding_id -> evaluation dict

    with ThreadPoolExecutor(max_workers=max(1, len(by_module))) as pool:
        futures = {}
        for mod_name, findings in by_module.items():
            module = modules_by_name.get(mod_name)
            if not module:
                # 模块不在配置中，保守保留
                for f in findings:
                    results[f["id"]] = {"id": f["id"], "verdict": "verify", "reason": "模块未配置，保留"}
                continue

            findings_json = json.dumps(findings, ensure_ascii=False, indent=2)
            prompt = EVALUATE_PROMPT.format(
                module_name=mod_name,
                language=module.language,
                findings_json=findings_json,
            )
            timeout = module.estimate_timeout(project_root, task="scan")
            future = pool.submit(
                call_claude_bare,
                prompt=prompt,
                model="opus",
                tools="Read,Glob,Grep",
                output_schema=EVALUATE_SCHEMA,
                max_turns=20,
                cwd=project_root,
                timeout=timeout,
            )
            futures[future] = (mod_name, findings)

        for future in as_completed(futures):
            mod_name, findings = futures[future]
            try:
                result = future.result()
                evaluations = []
                if isinstance(result, dict):
                    evaluations = result.get("evaluations", [])
                elif isinstance(result, str):
                    try:
                        evaluations = json.loads(result).get("evaluations", [])
                    except json.JSONDecodeError:
                        pass

                for ev in evaluations:
                    if "id" in ev:
                        results[ev["id"]] = ev

                logger.info(
                    "%s 评估完成: %d 个 findings → %s",
                    mod_name,
                    len(findings),
                    _summarize_verdicts(evaluations),
                )
            except Exception as e:
                logger.error(f"{mod_name} 评估失败，该模块全部保留: {e}")
                for f in findings:
                    results[f["id"]] = {"id": f["id"], "verdict": "verify", "reason": f"评估失败: {e}"}

    # 分类（只处理 results 中有评估结果的 ID，过滤无效 ID）
    valid_finding_ids = {f["id"] for f in state.findings}
    to_verify = []
    to_skip = []
    for fid in confirmed_ids:
        if fid not in valid_finding_ids:
            logger.warning(f"confirmed_ids 中的 {fid} 不在 state.findings 中，跳过")
            continue
        ev = results.get(fid)
        if not ev or ev.get("verdict") in ("verify", "must_fix"):
            to_verify.append(fid)
        else:
            to_skip.append(fid)
            state.results[fid] = {
                "status": "eval_skipped",
                "reason": ev.get("reason", "深度评估判定不值得修复"),
                "actual_severity": ev.get("actual_severity", ""),
                "trigger_probability": ev.get("trigger_probability", ""),
            }

    # 打印摘要
    must_fix = sum(1 for ev in results.values() if ev.get("verdict") == "must_fix")
    verify = sum(1 for ev in results.values() if ev.get("verdict") == "verify")
    skip = len(to_skip)
    logger.info(f"深度评估汇总: must_fix={must_fix}, verify={verify}, skip={skip}")
    print(f"  must_fix: {must_fix} 个（必须修复）")
    print(f"  verify:   {verify} 个（需红绿验证确认）")
    print(f"  skip:     {skip} 个（不值得修复，跳过）")

    return to_verify


def _summarize_verdicts(evaluations):
    """生成评估摘要字符串"""
    counts = {}
    for ev in evaluations:
        v = ev.get("verdict", "unknown")
        counts[v] = counts.get(v, 0) + 1
    return ", ".join(f"{v}={c}" for v, c in sorted(counts.items()))
