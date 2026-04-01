"""cover 命令的 JSON Schema 定义"""

# Phase 1 输出：覆盖缺口清单
COVERAGE_GAPS_SCHEMA = {
    "type": "object",
    "properties": {
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "缺口 ID，如 G1, G2"
                    },
                    "module_pair": {
                        "type": "string",
                        "description": "模块边界对，如 'websocket ↔ cli' 或 'http ↔ config'"
                    },
                    "scenario": {
                        "type": "string",
                        "description": "需要测试的具体场景（用户视角），如 '多设备同时修改同一个 CLI 配置，最后写入应该胜出'"
                    },
                    "dimension": {
                        "type": "string",
                        "enum": [
                            "happy_path",
                            "cleanup",
                            "concurrency",
                            "error_recovery",
                            "security_boundary",
                            "fault_tolerance"
                        ],
                        "description": "测试维度"
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["P0", "P1", "P2"],
                        "description": "优先级：P0=P0 场景未覆盖，P1=模块边界无测试，P2=已有测试缺维度"
                    },
                    "why_missing": {
                        "type": "string",
                        "description": "为什么现有测试没覆盖到（简要）"
                    },
                    "test_hint": {
                        "type": "string",
                        "description": "测试实现提示：应该用哪些 helper、mock 什么、断言什么"
                    },
                    "related_source_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "需要读取的源码文件路径（写测试时作为上下文）"
                    }
                },
                "required": ["id", "module_pair", "scenario", "dimension", "priority", "test_hint"]
            }
        },
        "coverage_summary": {
            "type": "object",
            "description": "当前覆盖率概况",
            "properties": {
                "total_boundary_pairs": {
                    "type": "integer",
                    "description": "总模块边界对数"
                },
                "covered_pairs": {
                    "type": "integer",
                    "description": "已有测试覆盖的边界对数"
                },
                "existing_test_count": {
                    "type": "integer",
                    "description": "现有跨模块测试文件数"
                },
                "dimension_coverage": {
                    "type": "object",
                    "description": "各维度覆盖情况，如 {'happy_path': 25, 'error_recovery': 8}",
                    "additionalProperties": {"type": "integer"}
                }
            }
        }
    },
    "required": ["gaps", "coverage_summary"]
}
