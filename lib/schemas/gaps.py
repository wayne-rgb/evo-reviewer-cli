"""盲区归类 JSON Schema"""

GAPS_SCHEMA = {
    "type": "object",
    "properties": {
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "唯一标识，如 G1, G2"},
                    "module": {"type": "string"},
                    "gap_name": {"type": "string"},
                    "infra_plan": {"type": "string"},
                    "evidence_finding_ids": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "architecture_constraint": {"type": ["string", "null"]}
                },
                "required": ["id", "module", "gap_name", "infra_plan", "evidence_finding_ids"]
            }
        }
    },
    "required": ["gaps"]
}
