"""领域状态投影：由事件流重建的内存结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

SIDE_CN = "CN"
SIDE_EG = "EG"
SIDES = (SIDE_CN, SIDE_EG)

ROLE_DATA_STEWARD = "DATA_STEWARD"
ROLE_EDITOR = "EDITOR"
ROLE_RESEARCHER = "RESEARCHER"
ROLE_TRANSLATOR = "TRANSLATOR"

CARRIER_EXHIBITION_LABEL = "EXHIBITION_LABEL"
CARRIER_CATALOG = "CATALOG"
CARRIER_LECTURE = "LECTURE"
CARRIER_PAPER = "PAPER"
CARRIER_KINDS = (CARRIER_EXHIBITION_LABEL, CARRIER_CATALOG, CARRIER_LECTURE, CARRIER_PAPER)

TASK_CONTRIBUTOR_OBJECTION = "CONTRIBUTOR_OBJECTION"
TASK_TRANSLATION_DISPUTE = "TRANSLATION_DISPUTE"
TASK_EMBARGO_EXTENSION = "EMBARGO_EXTENSION"
TASK_KINDS = (TASK_CONTRIBUTOR_OBJECTION, TASK_TRANSLATION_DISPUTE, TASK_EMBARGO_EXTENSION)

REQUEST_PENDING = "PENDING"
REQUEST_APPROVED = "APPROVED"

VARIANT_ACTIVE = "ACTIVE"
VARIANT_SUSPENDED = "SUSPENDED"

TASK_OPEN = "OPEN"
TASK_RESOLVED = "RESOLVED"


@dataclass
class UnitState:
    unit_id: str
    name: str
    site: str


@dataclass
class ArtifactState:
    artifact_id: str
    unit_id: str
    name: str
    on_display: bool = True


@dataclass
class RecordState:
    record_id: str
    artifact_id: str
    kind: str
    versions: dict[int, dict] = field(default_factory=dict)
    current: int = 0


@dataclass
class ContributorState:
    contributor_id: str
    name: str
    side: str
    roles: list[str]


@dataclass
class LoanTermsState:
    loan_id: str
    artifact_id: str
    lender: str
    allowed_record_kinds: list[str]
    notes: str = ""


@dataclass
class EmbargoState:
    embargo_id: str
    artifact_id: str
    not_before: datetime
    reason: str = ""
    extensions: list[dict] = field(default_factory=list)


@dataclass
class ClaimState:
    claim_id: str
    artifact_id: str
    author_id: str
    record_ids: list[str] = field(default_factory=list)
    versions: dict[int, dict] = field(default_factory=dict)
    current: int = 0


@dataclass
class TranslationState:
    translation_id: str
    claim_id: str
    language: str
    translator_id: str
    versions: dict[int, dict] = field(default_factory=dict)
    current: int = 0


@dataclass
class CarrierState:
    carrier_id: str
    kind: str
    artifact_ids: list[str]
    requires_physical_display: bool


@dataclass
class RequestVersionState:
    """一次冻结提交：内容变化时新建，确认不跨版本继承。"""

    number: int
    declared: dict
    frozen: dict
    content_hash: str
    carrier_ids: list[str]
    submitted_by: str
    summary: str
    approvals: dict[str, dict] = field(default_factory=dict)
    status: str = REQUEST_PENDING


@dataclass
class RequestState:
    request_id: str
    versions: dict[int, RequestVersionState] = field(default_factory=dict)
    current: int = 0

    def version(self, number: int | None = None) -> RequestVersionState:
        return self.versions[number or self.current]


@dataclass
class VariantState:
    variant_id: str
    carrier_id: str
    versions: dict[int, dict] = field(default_factory=dict)
    current: int = 0
    status: str = VARIANT_ACTIVE
    suspended_reason: str | None = None
    suspended_artifact_id: str | None = None


@dataclass
class TaskState:
    task_id: str
    kind: str
    opened_by: str
    target: dict
    detail: str = ""
    status: str = TASK_OPEN
    resolution: dict | None = None
