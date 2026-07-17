from __future__ import annotations

from typing import Any


def get_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _literal(value: str, context: dict[str, Any]) -> Any:
    value = value.strip()
    if value.startswith("context."):
        return get_path(context, value[len("context.") :])
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def evaluate_condition(expression: str | None, context: dict[str, Any], exists: Any) -> bool:
    if not expression:
        return True
    expression = expression.strip()
    if " AND " in expression:
        return all(evaluate_condition(part, context, exists) for part in expression.split(" AND "))
    if " OR " in expression:
        return any(evaluate_condition(part, context, exists) for part in expression.split(" OR "))
    if expression.startswith("not "):
        return not evaluate_condition(expression[4:], context, exists)
    if expression.startswith("exists(") and expression.endswith(")"):
        return bool(exists(expression[7:-1].strip()))
    if " in " in expression:
        left, right = expression.split(" in ", 1)
        candidate = _literal(left, context)
        values = [_literal(item.strip(), context) for item in right.strip().strip("[]").split(",") if item.strip()]
        return candidate in values
    if "!=" in expression:
        left, right = expression.split("!=", 1)
        return _literal(left, context) != _literal(right, context)
    if "==" in expression:
        left, right = expression.split("==", 1)
        return _literal(left, context) == _literal(right, context)
    return bool(_literal(expression, context))
