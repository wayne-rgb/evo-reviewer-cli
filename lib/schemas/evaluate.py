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
                        "description": "触发概率"
                    },
                    "user_impact": {
                        "type": "string",
                        "description": "触发后用户感知到的影响（一句话）"
                    },
                    "self_healing": {
                        "type": "string",
                        "description": "系统是否有自愈机制（一句话）"
                    },
                    "reason": {
                        "type": "string",
                        "description": "判定理由（含衍生影响分析）"
                    },
                    "actual_severity": {
                        "type": "string",
                        "enum": ["HIGH", "MEDIUM", "LOW", "NONE"],
                        "description": "重新评估后的实际严重度"
                    }
                },
                "required": ["id", "verdict", "trigger_probability", "user_impact", "reason", "actual_severity"]
            }
        }
    },
    "required": ["evaluations"]
}
