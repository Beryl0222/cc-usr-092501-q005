"""领域事件信封的基础校验。"""

from __future__ import annotations

from datetime import datetime

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id", "occurred_at", "version", "summary")

EVENT_TYPES = frozenset({
    "UNIT_REGISTERED",
    "ARTIFACT_REGISTERED",
    "RECORD_REGISTERED",
    "RECORD_AMENDED",
    "CONTRIBUTOR_REGISTERED",
    "LOAN_TERMS_RECORDED",
    "EMBARGO_SET",
    "EMBARGO_EXTENDED",
    "CLAIM_PROPOSED",
    "CLAIM_CORRECTED",
    "TRANSLATION_SUBMITTED",
    "TRANSLATION_REVISED",
    "CARRIER_REGISTERED",
    "RELEASE_REQUESTED",
    "BILATERAL_APPROVAL_RECORDED",
    "RELEASE_APPROVED",
    "PUBLIC_VARIANT_ISSUED",
    "VARIANT_CORRECTED",
    "VARIANT_SUSPENDED",
    "VARIANT_RESUMED",
    "ARTIFACT_WITHDRAWN",
    "ARTIFACT_REINSTATED",
    "TASK_OPENED",
    "TASK_RESOLVED",
})

AGGREGATE_TYPES = frozenset({
    "excavation_unit",
    "artifact",
    "excavation_record",
    "contributor",
    "loan_terms",
    "embargo",
    "research_claim",
    "translation",
    "public_carrier",
    "release_request",
    "public_variant",
    "coordination_task",
})

def validate_event(record: object) -> list[str]:
    if not isinstance(record, dict):
        return ["事件必须是 JSON 对象"]
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    event_type = record.get("event_type")
    if event_type is not None and event_type not in EVENT_TYPES:
        errors.append(f"未知事件类型：{event_type}")
    aggregate_type = record.get("aggregate_type")
    if aggregate_type is not None and aggregate_type not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{aggregate_type}")
    if "version" in record and (not isinstance(record["version"], int) or isinstance(record["version"], bool) or record["version"] < 1):
        errors.append("version 必须是正整数")
    if "occurred_at" in record:
        try:
            parsed = datetime.fromisoformat(str(record["occurred_at"]).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                errors.append("occurred_at 必须包含时区")
        except ValueError:
            errors.append("occurred_at 必须是 ISO 8601 时间")
    return errors
