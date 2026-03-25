"""验证相关 JSON Schema"""

CHECK_REASON_SCHEMA = {
    "type": "object",
    "properties": {
        "related": {"type": "boolean", "description": "测试失败是否与声称的 bug 相关"},
        "reason": {"type": "string", "description": "判断理由"}
    },
    "required": ["related", "reason"]
}

CONSTRAINTS_SCHEMA = {
    "type": "object",
    "properties": {
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "constraint": {"type": "string"},
                    "source_bug_ids": {"type": "array", "items": {"type": "string"}},
                    "detail": {"type": "string"}
                },
                "required": ["constraint", "source_bug_ids", "detail"]
            }
        }
    },
    "required": ["constraints"]
}

TEST_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "dimensions_covered": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1, "maximum": 6}
        },
        "dimensions_missing": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "integer"},
                    "reason": {"type": "string"},
                    "suggestion": {"type": "string"}
                },
                "required": ["dimension", "reason"]
            }
        },
        "quality_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "issues": {
            "type": "array",
            "items": {"type": "string"}
        },
        "strengths": {
            "type": "array",
            "items": {"type": "string"}
        }
    },
    "required": ["dimensions_covered", "dimensions_missing", "quality_score"]
}
