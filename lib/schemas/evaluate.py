"""R3 深度评估结果 JSON Schema"""

EVALUATE_SCHEMA = {
    "type": "object",
    "properties": {
        "evaluations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "finding ID，如 F1, F2"},
                    "verdict": {
                        "type": "string",
                        "enum": ["must_fix", "verify", "skip"],
                        "description": "must_fix=必须修, verify=需红绿验证确认, skip=不值得修"
                    },
                    "trigger_probability": {
                        "type": "string",
                        "enum": ["high", "medium", "low", "near_zero"],
                        "description": "触发概率等级"
                    },
                    "trigger_scenario": {
                        "type": "string",
                        "description": "触发所需的具体用户操作序列（如：用户录音→语音识别超时→再次点录音按钮）"
                    },
                    "user_impact": {
                        "type": "string",
                        "description": "触发后用户感知到的影响"
                    },
                    "existing_protection": {
                        "type": "string",
                        "description": "同代码库中是否有同类保护模式？此处是否遗漏？对端模块是否有兜底？"
                    },
                    "derivative_impact": {
                        "type": "string",
                        "description": "衍生影响链：bug 触发后的错误状态是否阻塞后续操作？比原始描述更严重吗？"
                    },
                    "reason": {
                        "type": "string",
                        "description": "最终判定理由（综合以上所有分析）"
                    },
                    "actual_severity": {
                        "type": "string",
                        "enum": ["HIGH", "MEDIUM", "LOW", "NONE"],
                        "description": "重新评估后的实际严重度"
                    }
                },
                "required": ["id", "verdict", "trigger_probability", "trigger_scenario", "user_impact", "derivative_impact", "reason", "actual_severity"]
            }
        }
    },
    "required": ["evaluations"]
}
