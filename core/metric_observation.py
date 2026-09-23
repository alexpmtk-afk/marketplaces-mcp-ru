from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .marketplace_knowledge import get_knowledge_metric


@dataclass(frozen=True)
class MetricObservation:
    metric_id: str
    label: str
    definition: str
    value: Any
    unit: str
    marketplace: str
    source_name: str
    source_field: str
    observed_at: str | None
    semantic_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_metric_observation(
    *,
    metric_id: str,
    value: Any,
    unit: str,
    marketplace: str,
    source_name: str,
    source_field: str,
    observed_at: str | None = None,
) -> MetricObservation:
    """Build a normalized value without inventing business meaning.

    Unknown/unregistered meanings remain visible as source_field instead of
    being silently dropped or guessed.
    """
    knowledge = get_knowledge_metric(
        metric_id,
        marketplace=marketplace,
        source_field=source_field,
    )
    if knowledge is None:
        return MetricObservation(
            metric_id=metric_id,
            label=f"Поле источника: {source_field}",
            definition="Официальное бизнес-определение этого поля пока не подтверждено.",
            value=value,
            unit=unit,
            marketplace=marketplace,
            source_name=source_name,
            source_field=source_field,
            observed_at=observed_at,
            semantic_status="source_field",
        )

    return MetricObservation(
        metric_id=metric_id,
        label=str(knowledge["label_ru"]),
        definition=str(knowledge["definition_ru"]),
        value=value,
        unit=str(knowledge.get("unit") or unit),
        marketplace=str(knowledge.get("marketplace") or marketplace),
        source_name=source_name,
        source_field=source_field,
        observed_at=observed_at,
        semantic_status=str(knowledge["semantic_status"]),
    )
