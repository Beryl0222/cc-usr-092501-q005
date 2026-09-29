"""领域事件名称、参与方与释出载体约定。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .envelope import validate_event

# 双边数据责任人：中方与埃方。
PARTIES = ("CN", "EG")

# 面向公众的载体（渠道）。
CHANNEL_GALLERY_LABEL = "gallery_label"      # 展签
CHANNEL_CATALOG = "catalog"                  # 图录
CHANNEL_PUBLIC_LECTURE = "public_lecture"    # 公共讲座
CHANNEL_ACADEMIC_PAPER = "academic_paper"    # 学术论文
CHANNELS = (
    CHANNEL_GALLERY_LABEL,
    CHANNEL_CATALOG,
    CHANNEL_PUBLIC_LECTURE,
    CHANNEL_ACADEMIC_PAPER,
)

# 依赖文物实体在场展示的载体；撤展只暂停这些载体。
PHYSICAL_CHANNELS = frozenset({CHANNEL_GALLERY_LABEL})

# 解释主张的确定性。
CONFIDENCE_PROVISIONAL = "provisional"        # 初步结论，须标注不确定性
CONFIDENCE_CONFIRMED = "confirmed"
CONFIDENCE_LEVELS = (CONFIDENCE_PROVISIONAL, CONFIDENCE_CONFIRMED)

EVENT_RECORD_REGISTERED = "RECORD_REGISTERED"
EVENT_OBJECT_REGISTERED = "OBJECT_REGISTERED"
EVENT_LOAN_TERMS_REGISTERED = "LOAN_TERMS_REGISTERED"
EVENT_EMBARGO_REGISTERED = "EMBARGO_REGISTERED"
EVENT_EMBARGO_EXTENDED = "EMBARGO_EXTENDED"
EVENT_CLAIM_PROPOSED = "CLAIM_PROPOSED"
EVENT_CLAIM_CORRECTED = "CLAIM_CORRECTED"
EVENT_TRANSLATION_DRAFTED = "TRANSLATION_DRAFTED"
EVENT_TASK_OPENED = "TASK_OPENED"
EVENT_TASK_CLOSED = "TASK_CLOSED"
EVENT_DISPUTE_FILED = "DISPUTE_FILED"
EVENT_DISPUTE_RESOLVED = "DISPUTE_RESOLVED"
EVENT_RELEASE_REQUESTED = "RELEASE_REQUESTED"
EVENT_BILATERAL_APPROVAL_RECORDED = "BILATERAL_APPROVAL_RECORDED"
EVENT_REQUEST_APPROVED = "REQUEST_APPROVED"
EVENT_REQUEST_REJECTED = "REQUEST_REJECTED"
EVENT_PUBLIC_VARIANT_ISSUED = "PUBLIC_VARIANT_ISSUED"
EVENT_PUBLICATION_SUSPENDED = "PUBLICATION_SUSPENDED"
EVENT_PUBLICATION_REINSTATED = "PUBLICATION_REINSTATED"
EVENT_PUBLICATION_CORRECTION_LINKED = "PUBLICATION_CORRECTION_LINKED"
EVENT_OBJECT_WITHDRAWN = "OBJECT_WITHDRAWN"
EVENT_OBJECT_RETURNED = "OBJECT_RETURNED"

AGG_RECORD = "excavation_record"
AGG_OBJECT = "find_object"
AGG_CLAIM = "research_claim"
AGG_TRANSLATION = "translation"
AGG_LOAN = "loan_terms"
AGG_EMBARGO = "embargo"
AGG_REQUEST = "release_request"
AGG_VARIANT = "public_variant"
AGG_DISPUTE = "dispute"
AGG_TASK = "task"


def make_event(
    event_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    occurred_at: datetime,
    version: int,
    summary: str,
    **payload: Any,
) -> dict[str, Any]:
    """构造一条信封合法的领域事件；payload 展开为事件附加字段。"""
    event: dict[str, Any] = {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at.isoformat(),
        "version": version,
        "summary": summary,
    }
    event.update(payload)
    errors = validate_event(event)
    if errors:
        raise ValueError("；".join(errors))
    return event


def canonical_hash(payload: Any) -> str:
    """对申请内容做稳定指纹，用于“相同申请重试复用原流程”。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
