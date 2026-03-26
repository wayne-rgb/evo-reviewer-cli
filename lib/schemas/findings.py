"""扫描结果 JSON Schema"""

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "唯一标识，如 F1, F2"},
                    "category": {
                        "type": "string",
                        "enum": ["resource_leak", "flag_lock", "error_swallow",
                                 "concurrency", "security_boundary", "state_machine",
                                 "error_propagation", "implicit_assumption", "architecture"]
                    },
                    "file": {"type": "string", "description": "bug 核心位置的文件路径"},
                    "line": {"type": "integer", "description": "bug 核心位置的行号"},
                    "description": {"type": "string", "description": "bug 描述，必须用 '函数名 (line N)' 格式引用关键行号：触发点、正常路径对比、影响范围"},
                    "severity": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "test_strategy": {
                        "type": "string",
                        "enum": ["behavior", "missing_mechanism", "compile"]
                    },
                    "why_not_caught": {"type": "string"},
                    "infra_needed": {"type": "string"}
                },
                "required": ["id", "category", "file", "line", "description", "severity"]
            }
        }
    },
    "required": ["findings"]
}
