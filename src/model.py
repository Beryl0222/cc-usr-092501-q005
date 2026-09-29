"""把事件流归约成当前世界状态。纯函数式折叠，不做业务裁决。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import (
    AGG_CLAIM,
    AGG_DISPUTE,
    AGG_EMBARGO,
    AGG_LOAN,
    AGG_OBJECT,
    AGG_RECORD,
    AGG_REQUEST,
    AGG_TASK,
    AGG_TRANSLATION,
    AGG_VARIANT,
    EVENT_BILATERAL_APPROVAL_RECORDED,
    EVENT_CLAIM_CORRECTED,
    EVENT_CLAIM_PROPOSED,
    EVENT_DISPUTE_FILED,
    EVENT_DISPUTE_RESOLVED,
    EVENT_EMBARGO_EXTENDED,
    EVENT_EMBARGO_REGISTERED,
    EVENT_LOAN_TERMS_REGISTERED,
    EVENT_OBJECT_REGISTERED,
    EVENT_OBJECT_RETURNED,
    EVENT_OBJECT_WITHDRAWN,
    EVENT_PUBLICATION_CORRECTION_LINKED,
    EVENT_PUBLICATION_REINSTATED,
    EVENT_PUBLICATION_SUSPENDED,
    EVENT_PUBLIC_VARIANT_ISSUED,
    EVENT_RECORD_REGISTERED,
    EVENT_RELEASE_REQUESTED,
    EVENT_REQUEST_APPROVED,
    EVENT_REQUEST_REJECTED,
    EVENT_TASK_CLOSED,
    EVENT_TASK_OPENED,
    EVENT_TRANSLATION_DRAFTED,
)


@dataclass
class RecordState:
    id: str
    unit_id: str = ""
    title: str = ""
    versions: dict[int, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ClaimState:
    id: str
    versions: dict[int, dict[str, Any]] = field(default_factory=dict)


@dataclass
class RequestState:
    id: str
    content_hash: str = ""
    client_ref: str = ""
    channels: tuple[str, ...] = ()
    data_refs: list[dict[str, Any]] = field(default_factory=list)
    contributors: list[str] = field(default_factory=list)
    status: str = "submitted"
    approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    revision_of: str | None = None
    decision_at: str | None = None


@dataclass
class VariantState:
    id: str
    request_id: str = ""
    channel: str = ""
    label_key: str = ""
    status: str = "issued"
    content: str = ""
    frozen: dict[str, Any] = field(default_factory=dict)
    contributors: list[dict[str, Any]] = field(default_factory=list)
    correction_of: str | None = None
    corrected_by: str | None = None
    suspend_reason: str | None = None


@dataclass
class State:
    records: dict[str, RecordState] = field(default_factory=dict)
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    loans: dict[str, dict[str, Any]] = field(default_factory=dict)
    embargos: dict[str, dict[str, Any]] = field(default_factory=dict)
    claims: dict[str, ClaimState] = field(default_factory=dict)
    translations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    requests: dict[str, RequestState] = field(default_factory=dict)
    requests_by_hash: dict[str, RequestState] = field(default_factory=dict)
    variants: dict[str, VariantState] = field(default_factory=dict)
    variants_by_label: dict[tuple[str, str], str] = field(default_factory=dict)
    disputes: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)

    def ref(self, ref_type: str, ref_id: str) -> Any:
        return {
            AGG_RECORD: self.records,
            AGG_OBJECT: self.objects,
            AGG_LOAN: self.loans,
            AGG_EMBARGO: self.embargos,
            AGG_CLAIM: self.claims,
            AGG_TRANSLATION: self.translations,
            AGG_REQUEST: self.requests,
            AGG_VARIANT: self.variants,
            AGG_DISPUTE: self.disputes,
            AGG_TASK: self.tasks,
        }[ref_type].get(ref_id)


def reduce_events(events: list[dict[str, Any]]) -> State:
    state = State()
    for event in events:
        _apply(state, event)
    return state


def _apply(state: State, event: dict[str, Any]) -> None:
    kind = event["event_type"]
    agg_id = event["aggregate_id"]
    version = event["version"]

    if kind == EVENT_RECORD_REGISTERED:
        record = state.records.setdefault(agg_id, RecordState(id=agg_id))
        record.unit_id = event.get("unit_id", record.unit_id)
        record.title = event.get("title", record.title)
        record.versions[version] = {"data": event.get("data", {}), "at": event["occurred_at"]}

    elif kind == EVENT_OBJECT_REGISTERED:
        state.objects[agg_id] = {
            "id": agg_id,
            "record_id": event.get("record_id"),
            "title": event.get("title", ""),
            "loan_terms_id": event.get("loan_terms_id"),
            "display_status": "on_display",
        }

    elif kind == EVENT_OBJECT_WITHDRAWN:
        state.objects[agg_id]["display_status"] = "withdrawn"
        state.objects[agg_id]["withdrawn_reason"] = event.get("reason", "")

    elif kind == EVENT_OBJECT_RETURNED:
        state.objects[agg_id]["display_status"] = "on_display"
        state.objects[agg_id].pop("withdrawn_reason", None)

    elif kind == EVENT_LOAN_TERMS_REGISTERED:
        state.loans[agg_id] = {
            "id": agg_id,
            "object_id": event.get("object_id"),
            "clauses": event.get("clauses", ""),
            "permitted_channels": tuple(event.get("permitted_channels", ())),
        }

    elif kind == EVENT_EMBARGO_REGISTERED:
        state.embargos[agg_id] = {
            "id": agg_id,
            "scope_ids": list(event.get("scope_ids", ())),
            "channels": tuple(event.get("channels", ())),
            "until": event["until"],
            "status": "active",
            "history": [{"at": event["occurred_at"], "until": event["until"]}],
        }

    elif kind == EVENT_EMBARGO_EXTENDED:
        embargo = state.embargos[agg_id]
        embargo["until"] = event["new_until"]
        embargo["history"].append({"at": event["occurred_at"], "until": event["new_until"]})

    elif kind == EVENT_CLAIM_PROPOSED:
        claim = state.claims.setdefault(agg_id, ClaimState(id=agg_id))
        claim.versions[version] = {
            "text": event.get("text", ""),
            "confidence": event.get("confidence", "provisional"),
            "contributors": list(event.get("contributors", ())),
            "record_ids": list(event.get("record_ids", ())),
            "object_ids": list(event.get("object_ids", ())),
            "at": event["occurred_at"],
        }

    elif kind == EVENT_CLAIM_CORRECTED:
        claim = state.claims.setdefault(agg_id, ClaimState(id=agg_id))
        claim.versions[version] = {
            "text": event.get("text", ""),
            "confidence": event.get("confidence", "provisional"),
            "contributors": list(event.get("contributors", ())),
            "record_ids": list(event.get("record_ids", ())),
            "object_ids": list(event.get("object_ids", ())),
            "corrected_version": event.get("corrected_version", version - 1),
            "reason": event.get("reason", ""),
            "at": event["occurred_at"],
        }

    elif kind == EVENT_TRANSLATION_DRAFTED:
        state.translations.setdefault(agg_id, []).append(
            {
                "id": agg_id,
                "version": version,
                "claim_id": event.get("claim_id"),
                "language": event.get("language"),
                "text": event.get("text", ""),
                "translator_id": event.get("translator_id", ""),
                "at": event["occurred_at"],
            }
        )

    elif kind == EVENT_TASK_OPENED:
        state.tasks[agg_id] = {
            "id": agg_id,
            "kind": event.get("kind"),
            "subject_type": event.get("subject_type"),
            "subject_id": event.get("subject_id"),
            "title": event.get("title", ""),
            "status": "open",
            "opened_at": event["occurred_at"],
            "requested_by": event.get("requested_by"),
            "new_until": event.get("new_until"),
        }

    elif kind == EVENT_TASK_CLOSED:
        state.tasks[agg_id]["status"] = "closed"
        state.tasks[agg_id]["closed_at"] = event["occurred_at"]
        state.tasks[agg_id]["resolution"] = event.get("resolution", "")

    elif kind == EVENT_DISPUTE_FILED:
        state.disputes[agg_id] = {
            "id": agg_id,
            "kind": event.get("kind"),
            "subject_type": event.get("subject_type"),
            "subject_id": event.get("subject_id"),
            "raised_by": event.get("raised_by"),
            "detail": event.get("detail", ""),
            "task_id": event.get("task_id"),
            "status": "open",
            "opened_at": event["occurred_at"],
        }

    elif kind == EVENT_DISPUTE_RESOLVED:
        dispute = state.disputes[agg_id]
        dispute["status"] = "resolved"
        dispute["resolution"] = event.get("resolution", "")
        dispute["resolved_at"] = event["occurred_at"]

    elif kind == EVENT_RELEASE_REQUESTED:
        request = RequestState(
            id=agg_id,
            content_hash=event.get("content_hash", ""),
            client_ref=event.get("client_ref", ""),
            channels=tuple(event.get("channels", ())),
            data_refs=list(event.get("data_refs", ())),
            contributors=list(event.get("contributors", ())),
            revision_of=event.get("revision_of"),
        )
        state.requests[agg_id] = request
        # 相同内容首次出现的申请占用哈希位；重试永远回到同一流程。
        state.requests_by_hash.setdefault(request.content_hash, request)

    elif kind == EVENT_BILATERAL_APPROVAL_RECORDED:
        request = state.requests[agg_id]
        request.approvals[event["party"]] = {"by": event.get("by", ""), "at": event["occurred_at"]}

    elif kind == EVENT_REQUEST_APPROVED:
        state.requests[agg_id].status = "approved"
        state.requests[agg_id].decision_at = event["occurred_at"]

    elif kind == EVENT_REQUEST_REJECTED:
        request = state.requests[agg_id]
        request.status = "rejected"
        request.decision_at = event["occurred_at"]
        request.rejection_reason = event.get("reason", "")

    elif kind == EVENT_PUBLIC_VARIANT_ISSUED:
        variant = VariantState(
            id=agg_id,
            request_id=event.get("request_id", ""),
            channel=event.get("channel", ""),
            label_key=event.get("label_key", ""),
            content=event.get("content", ""),
            frozen=event.get("frozen", {}),
            contributors=list(event.get("contributors", ())),
            correction_of=event.get("correction_of"),
        )
        state.variants[agg_id] = variant
        if variant.label_key:
            # 更正版与旧版共用展签键：键指向当前最新版；旧版经 correction_of 链取回，不被覆盖。
            state.variants_by_label[(variant.channel, variant.label_key)] = agg_id
        if variant.correction_of:
            state.variants[variant.correction_of].corrected_by = agg_id

    elif kind == EVENT_PUBLICATION_SUSPENDED:
        variant = state.variants[agg_id]
        variant.status = "suspended"
        variant.suspend_reason = event.get("reason", "")

    elif kind == EVENT_PUBLICATION_REINSTATED:
        variant = state.variants[agg_id]
        variant.status = "issued"
        variant.suspend_reason = None

    elif kind == EVENT_PUBLICATION_CORRECTION_LINKED:
        # 旧版不被覆盖，只挂更正指针；更正内容本身是另一条 PUBLIC_VARIANT_ISSUED。
        state.variants[agg_id].corrected_by = event["new_variant_id"]
