"""cover 命令的 JSON Schema 定义"""

# Phase 1 输出：覆盖矩阵 + 缺口清单
COVERAGE_GAPS_SCHEMA = {
    "type": "object",
    "properties": {
        "coverage_matrix": {
            "type": "array",
            "description": "覆盖矩阵：每个模块边界对在 6 个测试维度上的覆盖情况",
            "items": {
                "type": "object",
                "properties": {
                    "module_pair": {
                        "type": "string",
                        "description": "模块边界对，如 'websocket ↔ cli'"
                    },
                    "chain_name": {
                        "type": "string",
                        "description": "业务链路名称，如 '用户创建订单链路'"
                    },
                    "module_chain": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "完整链路节点序列，如 ['客户端', 'API Gateway', 'order-service', 'payment', 'DB']"
                    },
                    "dimensions": {
                        "type": "object",
                        "description": "各维度覆盖情况（true=已覆盖，false=未覆盖）",
                        "properties": {
                            "happy_path": {"type": "boolean"},
                            "cleanup": {"type": "boolean"},
                            "concurrency": {"type": "boolean"},
                            "error_recovery": {"type": "boolean"},
                            "security_boundary": {"type": "boolean"},
                            "fault_tolerance": {"type": "boolean"}
                        }
                    }
                },
                "required": ["module_pair", "dimensions"]
            }
        },
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
                        "description": "模块边界对，如 'websocket ↔ cli'"
                    },
                    "module_chain": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "缺口所属的完整业务链路节点序列"
                    },
                    "gap_segment": {
                        "type": "string",
                        "description": "链路中具体断裂的段，如 'service-handler → data-layer'"
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
                        "description": "P0=P0 场景未覆盖，P1=模块边界无测试，P2=已有测试缺维度"
                    },
                    "why_missing": {
                        "type": "string",
                        "description": "为什么现有测试没覆盖到"
                    },
                    "test_hint": {
                        "type": "string",
                        "description": "测试实现提示：应该用哪些 helper、mock 什么、断言什么"
                    },
                    "related_source_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "需要读取的源码文件路径"
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
                    "description": "至少有一个维度被覆盖的边界对数"
                },
                "existing_test_count": {
                    "type": "integer",
                    "description": "现有跨模块测试文件数"
                },
                "dimension_coverage": {
                    "type": "object",
                    "description": "各维度被覆盖的边界对数，如 {'happy_path': 5, 'error_recovery': 2}",
                    "additionalProperties": {"type": "integer"}
                },
                "total_chains": {
                    "type": "integer",
                    "description": "识别到的端到端业务链路数"
                },
                "fully_covered_chains": {
                    "type": "integer",
                    "description": "所有维度都覆盖的链路数"
                }
            }
        }
    },
    "required": ["coverage_matrix", "gaps", "coverage_summary"]
}
