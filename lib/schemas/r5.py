"""R5 交叉检验三件套的 JSON Schema。"""

SIMILAR_PATTERN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["yes", "no", "uncertain"]},
        "reason": {"type": "string", "minLength": 1, "maxLength": 400},
    },
    "required": ["verdict", "reason"],
}


ADVERSARIAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "adversarial_inputs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string", "minLength": 1, "maxLength": 40},
                    "input": {"type": "string", "minLength": 1, "maxLength": 500},
                    "why_bypass": {"type": "string", "minLength": 1, "maxLength": 300},
                },
                "required": ["label", "input", "why_bypass"],
            },
        },
    },
    "required": ["adversarial_inputs"],
}
