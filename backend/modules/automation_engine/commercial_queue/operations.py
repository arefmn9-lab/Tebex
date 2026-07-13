from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class OperationDefinition:
    name: str
    required: bool = True


@dataclass(frozen=True)
class OperationValidationResult:
    valid: bool
    validated_operation_order: list[str]
    validation_errors: list[str]
    normalized_execution_steps: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OperationRegistry:
    def __init__(self) -> None:
        self._operations: dict[str, OperationDefinition] = {}
        self._required_order = ["save_contact", "forward_message"]

    def register(self, definition: OperationDefinition) -> None:
        self._operations[definition.name] = definition

    def supported_operations(self) -> list[str]:
        return list(self._operations.keys())

    def validate(self, requested_order: list[str] | None) -> OperationValidationResult:
        order = [str(item).strip() for item in (requested_order or self._required_order) if str(item).strip()]
        errors: list[str] = []
        seen: set[str] = set()
        for name in order:
            if name not in self._operations:
                errors.append(f"unsupported_operation:{name}")
            if name in seen:
                errors.append(f"duplicate_operation:{name}")
            seen.add(name)
        for required in self._required_order:
            if required not in order:
                errors.append(f"missing_required_operation:{required}")
        required_positions = [order.index(name) for name in self._required_order if name in order]
        if required_positions != sorted(required_positions):
            errors.append("unsafe_operation_order")
        steps = [
            {"step_name": name, "operation": name, "index": index + 1}
            for index, name in enumerate(order)
            if name in self._operations
        ]
        return OperationValidationResult(
            valid=not errors,
            validated_operation_order=order,
            validation_errors=errors,
            normalized_execution_steps=steps,
        )


operation_registry = OperationRegistry()
operation_registry.register(OperationDefinition("save_contact"))
operation_registry.register(OperationDefinition("forward_message"))
