"""溯源查询：从一句展签（或任何公众载体条目）回溯完整链路。

回溯内容：
- 当时冻结的数据版本（原始记录、测量数据）；
- 当时有效的研究结论版本，及其后续更正；
- 与主张关联的翻译稿；
- 许可链路：借展条款、双边会签、禁发期；
- 贡献署名；
- 本公众版本的暂停/恢复记录与更正关联。
"""

from __future__ import annotations

from typing import Any

from .events import (
    AGG_CLAIM,
    AGG_OBJECT,
    AGG_RECORD,
    AGG_TRANSLATION,
    CHANNELS,
    EVENT_BILATERAL_APPROVAL_RECORDED,
    EVENT_PUBLICATION_REINSTATED,
    EVENT_PUBLICATION_SUSPENDED,
    EVENT_PUBLIC_VARIANT_ISSUED,
    EVENT_RELEASE_REQUESTED,
)
from .model import State, reduce_events
from .store import EventStore


class TraceNotFound(LookupError):
    pass


class Tracer:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def trace_label(self, channel: str, label_key: str) -> dict[str, Any]:
        if channel not in CHANNELS:
            raise TraceNotFound(f"未知载体：{channel}")
        state = reduce_events(self.store.events)
        variant_id = state.variants_by_label.get((channel, label_key))
        if variant_id is None:
            raise TraceNotFound(f"未找到载体条目：{channel}/{label_key}")
        return self.trace_variant(variant_id, state=state)

    def trace_variant(self, variant_id: str, *, state: State | None = None) -> dict[str, Any]:
        state = state or reduce_events(self.store.events)
        variant = state.variants.get(variant_id)
        if variant is None:
            raise TraceNotFound(f"未找到公众版本：{variant_id}")

        variant_events = self.store.events_for(variant_id)
        issued_at = next(
            e["occurred_at"] for e in variant_events
            if e["event_type"] == EVENT_PUBLIC_VARIANT_ISSUED
        )
        status_history = [
            {"at": e["occurred_at"], "status": (
                "suspended" if e["event_type"] == EVENT_PUBLICATION_SUSPENDED else "reinstated"),
             "reason": e.get("reason", "")}
            for e in variant_events
            if e["event_type"] in (EVENT_PUBLICATION_SUSPENDED, EVENT_PUBLICATION_REINSTATED)
        ]

        request = state.requests.get(variant.request_id)
        request_events = self.store.events_for(variant.request_id)
        frozen = next(
            (e.get("frozen", {}) for e in request_events
             if e["event_type"] == EVENT_RELEASE_REQUESTED),
            {},
        )
        approval_events = [
            e for e in request_events
            if e["event_type"] == EVENT_BILATERAL_APPROVAL_RECORDED
        ]

        sources: dict[str, list[dict[str, Any]]] = {"records": [], "claims": [], "objects": [],
                                                    "loans": [], "translations": []}
        for ref in variant.frozen.get("data_refs", ()):
            snapshot = frozen.get(f"{ref['type']}:{ref['id']}:{ref.get('version', '')}")
            if ref["type"] == AGG_RECORD:
                sources["records"].append(self._record_entry(state, ref, snapshot))
            elif ref["type"] == AGG_CLAIM:
                sources["claims"].append(self._claim_entry(state, ref, snapshot))
            elif ref["type"] == AGG_OBJECT:
                sources["objects"].append(self._object_entry(state, ref))
            elif ref["type"] == "loan_terms":
                loan = state.loans.get(ref["id"])
                if loan:
                    sources["loans"].append({
                        "id": loan["id"], "clauses": loan["clauses"],
                        "permitted_channels": list(loan["permitted_channels"])})
            elif ref["type"] == AGG_TRANSLATION:
                sources["translations"].append(self._translation_entry(state, ref))

        # 主张关联的翻译即使未列入申请引用，也属于追溯链路。
        seen_translations = {t["id"] for t in sources["translations"]}
        for claim_entry in sources["claims"]:
            for drafts in state.translations.values():
                for draft in drafts:
                    if draft["claim_id"] == claim_entry["id"] and draft["id"] not in seen_translations:
                        seen_translations.add(draft["id"])
                        sources["translations"].append(
                            self._translation_entry(state, {"type": AGG_TRANSLATION,
                                                            "id": draft["id"]}))

        object_ids = {o["id"] for o in sources["objects"]}
        for record_entry in sources["records"]:
            for obj in state.objects.values():
                if obj["record_id"] == record_entry["id"]:
                    object_ids.add(obj["id"])
        for ref in variant.frozen.get("data_refs", ()):
            if ref["type"] == AGG_CLAIM:
                snapshot = frozen.get(f"{ref['type']}:{ref['id']}:{ref.get('version', '')}") or {}
                object_ids.update(snapshot.get("object_ids", []))

        # 主张隐式关联的文物也纳入溯源清单。
        listed_objects = {o["id"] for o in sources["objects"]}
        for object_id in sorted(object_ids - listed_objects):
            obj = state.objects.get(object_id)
            if obj:
                sources["objects"].append({
                    "id": obj["id"], "title": obj["title"], "record_id": obj["record_id"],
                    "display_status": obj["display_status"],
                    "withdrawn_reason": obj.get("withdrawn_reason"),
                    "loan_terms_id": obj.get("loan_terms_id"), "implicit_via": "claim"})

        return {
            "label": {"channel": variant.channel, "label_key": variant.label_key},
            "variant": {
                "id": variant.id,
                "status": variant.status,
                "issued_at": issued_at,
                "content": variant.content,
                "correction_of": variant.correction_of,
                "corrected_by": variant.corrected_by,
                "status_history": status_history,
            },
            "request": {
                "id": variant.request_id,
                "status": request.status if request else None,
                "channels": list(request.channels) if request else [],
                "revision_of": request.revision_of if request else None,
                "bilateral_approvals": [
                    {"party": e["party"], "by": e.get("by", ""), "at": e["occurred_at"]}
                    for e in approval_events
                ],
            },
            "sources": sources,
            "permissions": {
                "loans": [self._loan_for(state, oid) for oid in sorted(object_ids)
                          if self._loan_for(state, oid)],
                "embargos": self._embargo_summary(state, object_ids,
                                                  {r["id"] for r in sources["records"]}),
            },
            "contributors": variant.contributors,
        }

    # -- 条目构造 -------------------------------------------------------

    @staticmethod
    def _record_entry(state: State, ref: dict[str, Any], snapshot: Any) -> dict[str, Any]:
        record = state.records[ref["id"]]
        frozen_version = ref.get("version")
        latest = max(record.versions) if record.versions else None
        return {
            "id": ref["id"],
            "unit_id": record.unit_id,
            "title": record.title,
            "frozen_version": frozen_version,
            "latest_version": latest,
            "frozen_at_publication": snapshot is not None,
            "data": (snapshot or {}).get("data", {}),
        }

    @staticmethod
    def _claim_entry(state: State, ref: dict[str, Any], snapshot: Any) -> dict[str, Any]:
        claim = state.claims[ref["id"]]
        frozen_version = ref.get("version")
        latest = max(claim.versions)
        snapshot = snapshot or claim.versions.get(frozen_version, {})
        later = []
        if frozen_version is not None:
            for version in range(frozen_version + 1, latest + 1):
                entry = claim.versions[version]
                later.append({
                    "version": version,
                    "text": entry["text"],
                    "confidence": entry["confidence"],
                    "at": entry["at"],
                    "reason": entry.get("reason", ""),
                })
        return {
            "id": ref["id"],
            "frozen_version": frozen_version,
            "text_as_published": snapshot.get("text", ""),
            "confidence_as_published": snapshot.get("confidence", ""),
            "contributors": snapshot.get("contributors", []),
            "latest_version": latest,
            "later_corrections": later,
        }

    @staticmethod
    def _object_entry(state: State, ref: dict[str, Any]) -> dict[str, Any]:
        obj = state.objects[ref["id"]]
        return {
            "id": obj["id"],
            "title": obj["title"],
            "record_id": obj["record_id"],
            "display_status": obj["display_status"],
            "withdrawn_reason": obj.get("withdrawn_reason"),
            "loan_terms_id": obj.get("loan_terms_id"),
        }

    @staticmethod
    def _translation_entry(state: State, ref: dict[str, Any]) -> dict[str, Any]:
        drafts = state.translations.get(ref["id"], [])
        wanted = ref.get("version")
        draft = next((d for d in drafts if d["version"] == wanted), drafts[-1])
        return {
            "id": draft["id"],
            "claim_id": draft["claim_id"],
            "language": draft["language"],
            "version": draft["version"],
            "latest_version": drafts[-1]["version"],
            "text": draft["text"],
            "translator_id": draft["translator_id"],
            "at": draft["at"],
        }

    @staticmethod
    def _loan_for(state: State, object_id: str) -> dict[str, Any] | None:
        obj = state.objects.get(object_id)
        if not obj or not obj.get("loan_terms_id"):
            return None
        loan = state.loans.get(obj["loan_terms_id"])
        if loan is None:
            return None
        return {
            "object_id": object_id,
            "loan_terms_id": loan["id"],
            "clauses": loan["clauses"],
            "permitted_channels": list(loan["permitted_channels"]),
        }

    @staticmethod
    def _embargo_summary(state: State, object_ids: set[str], record_ids: set[str]) -> list[dict[str, Any]]:
        scope = object_ids | record_ids
        result = []
        for embargo in state.embargos.values():
            if scope & set(embargo["scope_ids"]):
                result.append({
                    "id": embargo["id"],
                    "until": embargo["until"],
                    "channels": list(embargo["channels"]),
                    "history": embargo["history"],
                })
        return result
