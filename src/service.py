"""成果释出协调服务：所有业务规则的唯一裁决处。

职责要点：
- 提交释出方案时冻结引用的数据版本（快照随事件持久化）；
- 中埃双边数据责任人分别会签，两边都到齐才批准；
- 策展编辑只能对已批准申请、且在其载体与借展范围内生成公众版本；
- 相同内容重试复用原流程，内容变化必须以修订方式新建申请；
- 文物临时撤展只暂停依赖实体展示的载体，不封锁学术事实；
- 异议、翻译争议、禁发期延期各自开立独立待办；
- 修订以更正关联旧版，已展出内容不被覆盖。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
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
    CHANNELS,
    CONFIDENCE_LEVELS,
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
    PARTIES,
    PHYSICAL_CHANNELS,
    canonical_hash,
    make_event,
)
from .model import RequestState, State, reduce_events
from .store import EventStore

ROLE_DATA_STEWARD = "data_steward"          # 双方数据责任人
ROLE_CURATORIAL_EDITOR = "curatorial_editor"  # 策展编辑
ROLE_COORDINATOR = "coordinator"            # 秘书处协调员

VERSIONED_REFS = (AGG_RECORD, AGG_CLAIM)
SIMPLE_REFS = (AGG_OBJECT, AGG_LOAN, AGG_TRANSLATION)
ALL_REF_TYPES = VERSIONED_REFS + SIMPLE_REFS


class ServiceError(RuntimeError):
    """业务规则被违反。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _ref_key(ref: dict[str, Any]) -> str:
    return f"{ref['type']}:{ref['id']}:{ref.get('version', '')}"


class ReleaseCoordinationService:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._lock = threading.RLock()

    # -- 内部工具 -------------------------------------------------------

    def _state(self) -> State:
        return reduce_events(self.store.events)

    def _append(self, event: dict[str, Any]) -> dict[str, Any]:
        self.store.append(event, expected_version=event["version"] - 1 or None)
        return event

    def _next_event(
        self,
        aggregate_id: str,
        event_type: str,
        aggregate_type: str,
        summary: str,
        now: datetime,
        **payload: Any,
    ) -> dict[str, Any]:
        return make_event(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=now,
            version=self.store.version_of(aggregate_id) + 1,
            summary=summary,
            **payload,
        )

    @staticmethod
    def _require_actor(actor: dict[str, Any], role: str) -> None:
        if actor.get("role") != role:
            raise ServiceError(f"该操作需要 {role} 角色，当前为 {actor.get('role')!r}")

    def _snapshot_ref(self, state: State, ref: dict[str, Any]) -> dict[str, Any]:
        ref_type, ref_id, version = ref["type"], ref["id"], ref.get("version")
        if ref_type == AGG_RECORD:
            record = state.records.get(ref_id)
            if record is None or version not in record.versions:
                raise ServiceError(f"原始记录 {ref_id} 不存在版本 v{version}")
            entry = record.versions[version]
            return {"type": ref_type, "id": ref_id, "version": version,
                    "unit_id": record.unit_id, "title": record.title, "data": entry["data"]}
        if ref_type == AGG_CLAIM:
            claim = state.claims.get(ref_id)
            if claim is None or version not in claim.versions:
                raise ServiceError(f"解释主张 {ref_id} 不存在版本 v{version}")
            return {"type": ref_type, "id": ref_id, "version": version, **claim.versions[version]}
        if ref_type == AGG_OBJECT:
            obj = state.objects.get(ref_id)
            if obj is None:
                raise ServiceError(f"文物对象 {ref_id} 未登记")
            return {"type": ref_type, "id": ref_id, "version": version, **obj}
        if ref_type == AGG_LOAN:
            loan = state.loans.get(ref_id)
            if loan is None:
                raise ServiceError(f"借展条款 {ref_id} 未登记")
            return {"type": ref_type, "id": ref_id, "version": version,
                    "clauses": loan["clauses"], "permitted_channels": list(loan["permitted_channels"])}
        if ref_type == AGG_TRANSLATION:
            drafts = state.translations.get(ref_id, [])
            if not drafts:
                raise ServiceError(f"翻译稿 {ref_id} 不存在")
            if version is None:
                draft = drafts[-1]
            else:
                draft = next((d for d in drafts if d["version"] == version), None)
                if draft is None:
                    raise ServiceError(f"翻译稿 {ref_id} 不存在版本 v{version}")
            return {"type": ref_type, "id": ref_id, "version": draft["version"],
                    "language": draft["language"], "text": draft["text"],
                    "translator_id": draft["translator_id"]}
        raise ServiceError(f"未知引用类型：{ref_type}")

    @staticmethod
    def _normalize_refs(data_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for ref in data_refs:
            if ref.get("type") not in ALL_REF_TYPES:
                raise ServiceError(f"未知引用类型：{ref.get('type')}")
            if not ref.get("id"):
                raise ServiceError("引用必须给出 id")
            entry = {"type": ref["type"], "id": ref["id"]}
            if "version" in ref and ref["version"] is not None:
                if not isinstance(ref["version"], int) or ref["version"] < 1:
                    raise ServiceError("引用版本必须是正整数")
                entry["version"] = ref["version"]
            normalized.append(entry)
        normalized.sort(key=lambda r: (r["type"], r["id"], r.get("version", 0)))
        return normalized

    @staticmethod
    def _request_content(channels: tuple[str, ...],
                         data_refs: list[dict[str, Any]],
                         contributors: list[str]) -> dict[str, Any]:
        return {
            "channels": sorted(channels),
            "data_refs": data_refs,
            "contributors": sorted(contributors),
        }

    # -- 登记：发掘单元 / 原始记录 / 文物 / 借展 / 禁发期 ---------------

    def register_record(self, record_id: str, unit_id: str, title: str,
                        data: dict[str, Any], *, actor: dict[str, Any] | None = None,
                        now: datetime | None = None) -> dict[str, Any]:
        now = now or _now()
        with self._lock:
            event = self._next_event(
                record_id, EVENT_RECORD_REGISTERED, AGG_RECORD,
                f"登记发掘原始记录：{title}", now,
                unit_id=unit_id, title=title, data=data)
            return self._append(event)

    def register_object(self, object_id: str, record_id: str, title: str,
                        *, loan_terms_id: str | None = None,
                        now: datetime | None = None) -> dict[str, Any]:
        now = now or _now()
        with self._lock:
            state = self._state()
            if record_id not in state.records:
                raise ServiceError(f"原始记录 {record_id} 尚未登记")
            if loan_terms_id and loan_terms_id not in state.loans:
                raise ServiceError(f"借展条款 {loan_terms_id} 尚未登记")
            event = self._next_event(
                object_id, EVENT_OBJECT_REGISTERED, AGG_OBJECT,
                f"登记文物对象：{title}", now,
                record_id=record_id, title=title, loan_terms_id=loan_terms_id)
            return self._append(event)

    def register_loan_terms(self, loan_id: str, object_id: str, clauses: str,
                            permitted_channels: list[str] | tuple[str, ...],
                            *, now: datetime | None = None) -> dict[str, Any]:
        now = now or _now()
        permitted = tuple(permitted_channels)
        bad = [c for c in permitted if c not in CHANNELS]
        if bad:
            raise ServiceError(f"未知载体：{bad}")
        with self._lock:
            event = self._next_event(
                loan_id, EVENT_LOAN_TERMS_REGISTERED, AGG_LOAN,
                f"登记借展条款：{loan_id}", now,
                object_id=object_id, clauses=clauses, permitted_channels=list(permitted))
            return self._append(event)

    def register_embargo(self, embargo_id: str, scope_ids: list[str],
                         until: str | datetime, *, channels: list[str] | tuple[str, ...] = (),
                         now: datetime | None = None) -> dict[str, Any]:
        now = now or _now()
        until_dt = _parse(until)
        bad = [c for c in channels if c not in CHANNELS]
        if bad:
            raise ServiceError(f"未知载体：{bad}")
        with self._lock:
            event = self._next_event(
                embargo_id, EVENT_EMBARGO_REGISTERED, AGG_EMBARGO,
                f"登记禁发期至 {until_dt.isoformat()}", now,
                scope_ids=list(scope_ids), channels=list(channels), until=until_dt.isoformat())
            return self._append(event)

    # -- 解释主张与翻译 -------------------------------------------------

    def propose_claim(self, claim_id: str, text: str, confidence: str,
                      contributors: list[str], *, record_ids: list[str] | None = None,
                      object_ids: list[str] | None = None,
                      now: datetime | None = None) -> dict[str, Any]:
        if confidence not in CONFIDENCE_LEVELS:
            raise ServiceError(f"未知确定性标注：{confidence}")
        now = now or _now()
        with self._lock:
            state = self._state()
            for rid in record_ids or ():
                if rid not in state.records:
                    raise ServiceError(f"原始记录 {rid} 尚未登记")
            if claim_id in state.claims:
                raise ServiceError("主张已存在，修订请使用 correct_claim")
            event = self._next_event(
                claim_id, EVENT_CLAIM_PROPOSED, AGG_CLAIM,
                f"提出解释主张（{confidence}）", now,
                text=text, confidence=confidence, contributors=list(contributors),
                record_ids=list(record_ids or ()), object_ids=list(object_ids or ()))
            return self._append(event)

    def correct_claim(self, claim_id: str, text: str, confidence: str,
                      contributors: list[str], *, reason: str,
                      record_ids: list[str] | None = None,
                      object_ids: list[str] | None = None,
                      now: datetime | None = None) -> dict[str, Any]:
        """以新版本更正主张；旧版本保留，版本之间挂更正关系。"""
        if confidence not in CONFIDENCE_LEVELS:
            raise ServiceError(f"未知确定性标注：{confidence}")
        now = now or _now()
        with self._lock:
            state = self._state()
            claim = state.claims.get(claim_id)
            if claim is None or not claim.versions:
                raise ServiceError(f"主张 {claim_id} 不存在，应先 propose_claim")
            corrected_version = max(claim.versions)
            event = self._next_event(
                claim_id, EVENT_CLAIM_CORRECTED, AGG_CLAIM,
                f"更正解释主张：{reason}", now,
                text=text, confidence=confidence, contributors=list(contributors),
                record_ids=list(record_ids or ()), object_ids=list(object_ids or ()),
                corrected_version=corrected_version, reason=reason)
            return self._append(event)

    def draft_translation(self, translation_id: str, claim_id: str, language: str,
                          text: str, translator_id: str, *,
                          now: datetime | None = None) -> dict[str, Any]:
        now = now or _now()
        with self._lock:
            state = self._state()
            if claim_id not in state.claims:
                raise ServiceError(f"解释主张 {claim_id} 不存在")
            event = self._next_event(
                translation_id, EVENT_TRANSLATION_DRAFTED, AGG_TRANSLATION,
                f"提交 {language} 翻译稿", now,
                claim_id=claim_id, language=language, text=text, translator_id=translator_id)
            return self._append(event)

    # -- 异议、争议、禁发延期：各自独立待办 -----------------------------

    def file_dispute(self, dispute_id: str, kind: str, subject_type: str, subject_id: str,
                     raised_by: str, detail: str, *,
                     now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """开立贡献人异议或翻译争议，同时产生一条独立待办。"""
        if kind not in ("contributor_objection", "translation_dispute"):
            raise ServiceError(f"未知争议类型：{kind}")
        now = now or _now()
        with self._lock:
            state = self._state()
            for open_dispute in state.disputes.values():
                if (open_dispute["status"] == "open"
                        and open_dispute["kind"] == kind
                        and open_dispute["subject_type"] == subject_type
                        and open_dispute["subject_id"] == subject_id):
                    raise ServiceError("同一主题已有未解决争议")
            task_id = f"task-{uuid.uuid4().hex[:10]}"
            task_event = self._next_event(
                task_id, EVENT_TASK_OPENED, AGG_TASK,
                f"待办：{detail[:24]}", now,
                kind=kind, subject_type=subject_type, subject_id=subject_id, title=detail)
            self._append(task_event)
            dispute_event = self._next_event(
                dispute_id, EVENT_DISPUTE_FILED, AGG_DISPUTE,
                f"提出{('贡献人异议' if kind == 'contributor_objection' else '翻译争议')}", now,
                kind=kind, subject_type=subject_type, subject_id=subject_id,
                raised_by=raised_by, detail=detail, task_id=task_id)
            self._append(dispute_event)
            return dispute_event, task_event

    def resolve_dispute(self, dispute_id: str, resolution: str, *,
                        now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or _now()
        with self._lock:
            state = self._state()
            dispute = state.disputes.get(dispute_id)
            if dispute is None:
                raise ServiceError(f"争议 {dispute_id} 不存在")
            if dispute["status"] == "resolved":
                raise ServiceError("争议已解决")
            results = []
            results.append(self._append(self._next_event(
                dispute_id, EVENT_DISPUTE_RESOLVED, AGG_DISPUTE,
                f"解决争议：{resolution[:24]}", now, resolution=resolution)))
            task = state.tasks.get(dispute["task_id"])
            if task and task["status"] == "open":
                results.append(self._append(self._next_event(
                    task["id"], EVENT_TASK_CLOSED, AGG_TASK,
                    "争议解决，关闭待办", now, resolution=resolution)))
            return results

    def request_embargo_extension(self, embargo_id: str, new_until: str | datetime,
                                  reason: str, *, requested_by: str,
                                  now: datetime | None = None) -> dict[str, Any]:
        now = now or _now()
        new_until_dt = _parse(new_until)
        with self._lock:
            state = self._state()
            embargo = state.embargos.get(embargo_id)
            if embargo is None:
                raise ServiceError(f"禁发期 {embargo_id} 不存在")
            if new_until_dt <= _parse(embargo["until"]):
                raise ServiceError("延期后的禁发期限必须晚于当前期限")
            task_id = f"task-{uuid.uuid4().hex[:10]}"
            event = self._next_event(
                task_id, EVENT_TASK_OPENED, AGG_TASK,
                f"待办：禁发期延期申请（{embargo_id}）", now,
                kind="embargo_extension", subject_type=AGG_EMBARGO, subject_id=embargo_id,
                title=reason, requested_by=requested_by, new_until=new_until_dt.isoformat())
            return self._append(event)

    def decide_embargo_extension(self, task_id: str, grant: bool, *,
                                 resolution: str = "",
                                 now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or _now()
        with self._lock:
            state = self._state()
            task = state.tasks.get(task_id)
            if task is None or task["kind"] != "embargo_extension":
                raise ServiceError(f"禁发延期待办 {task_id} 不存在")
            if task["status"] == "closed":
                raise ServiceError("待办已处理")
            embargo_id = task["subject_id"]
            embargo = state.embargos[embargo_id]
            new_until = task.get("new_until")
            results = []
            if grant:
                if _parse(new_until) <= _parse(embargo["until"]):
                    raise ServiceError("延期后的禁发期限必须晚于当前期限")
                results.append(self._append(self._next_event(
                    embargo_id, EVENT_EMBARGO_EXTENDED, AGG_EMBARGO,
                    f"禁发期延长至 {new_until}", now,
                    new_until=new_until, task_id=task_id, reason=task["title"])))
            results.append(self._append(self._next_event(
                task_id, EVENT_TASK_CLOSED, AGG_TASK,
                "批准禁发延期" if grant else "驳回禁发延期", now,
                resolution=resolution or ("granted" if grant else "denied"))))
            return results

    # -- 释出申请：冻结版本 / 重试复用 / 内容变化新版本 ------------------

    def submit_release_request(self, request_id: str, channels: list[str] | tuple[str, ...],
                               data_refs: list[dict[str, Any]], contributors: list[str],
                               *, revision_of: str | None = None, client_ref: str = "",
                               now: datetime | None = None) -> tuple[RequestState, bool]:
        """提交释出方案。返回（申请状态, 是否为重试复用）。"""
        now = now or _now()
        channels = tuple(channels)
        bad = [c for c in channels if c not in CHANNELS]
        if bad:
            raise ServiceError(f"未知载体：{bad}")
        if not channels:
            raise ServiceError("至少指定一个公开载体")
        refs = self._normalize_refs(list(data_refs))
        with self._lock:
            state = self._state()

            existing = state.requests.get(request_id)
            incoming_content = self._request_content(channels, refs, list(contributors))
            if existing is not None:
                # 相同申请重试：内容指纹一致才允许复用，且不产生新事件。
                if existing.content_hash == canonical_hash(incoming_content):
                    return existing, True
                raise ServiceError("申请编号已被不同内容占用：内容变化必须用新编号新建版本")

            for ref in refs:
                self._snapshot_ref(state, ref)  # 校验引用版本确实存在
            if revision_of is not None:
                prior = state.requests.get(revision_of)
                if prior is None:
                    raise ServiceError(f"被修订的申请 {revision_of} 不存在")
            content_hash = canonical_hash(incoming_content)
            reused = state.requests_by_hash.get(content_hash)
            if reused is not None:
                return reused, True
            if revision_of is not None:
                prior = state.requests[revision_of]
                if prior.content_hash == content_hash:
                    raise ServiceError("内容与原申请完全一致，应直接重试原流程而非新建版本")

            # 冻结：把每个引用在该版本的内容快照进事件。
            frozen = {_ref_key(ref): self._snapshot_ref(state, ref) for ref in refs}
            event = self._next_event(
                request_id, EVENT_RELEASE_REQUESTED, AGG_REQUEST,
                f"提交释出方案，载体：{','.join(sorted(channels))}", now,
                channels=list(channels), data_refs=refs,
                contributors=list(contributors), content_hash=content_hash,
                client_ref=client_ref, revision_of=revision_of, frozen=frozen)
            self._append(event)
            return self._state().requests[request_id], False

    # -- 崩溃恢复 -------------------------------------------------------

    def recover_pending_decisions(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """服务恢复后补偿：双边会签均已记录却未落批准的申请，补记批准。"""
        now = now or _now()
        recovered = []
        with self._lock:
            state = self._state()
            for request in state.requests.values():
                if request.status == "submitted" and all(p in request.approvals for p in PARTIES):
                    recovered.append(self._append(self._next_event(
                        request.id, EVENT_REQUEST_APPROVED, AGG_REQUEST,
                        "恢复后补记：中埃双边会签已齐备", now)))
        return recovered

    # -- 双边会签 -------------------------------------------------------

    def record_approval(self, request_id: str, party: str, actor: dict[str, Any], *,
                        now: datetime | None = None) -> list[dict[str, Any]]:
        """中（CN）埃（EG）数据责任人分别确认；两边到齐即批准。幂等。"""
        self._require_actor(actor, ROLE_DATA_STEWARD)
        if party not in PARTIES:
            raise ServiceError(f"未知责任方：{party}（应为 CN 或 EG）")
        now = now or _now()
        with self._lock:
            # 先做恢复补偿，保证“恢复后继续未完成会签”。
            self.recover_pending_decisions(now=now)
            state = self._state()
            request = state.requests.get(request_id)
            if request is None:
                raise ServiceError(f"释出申请 {request_id} 不存在")
            if request.status == "rejected":
                raise ServiceError("申请已被拒绝，不能再批准")
            prior = request.approvals.get(party)
            if prior is not None:
                if prior["by"] != actor.get("id"):
                    raise ServiceError(f"{party} 方已由 {prior['by']} 确认，不能换人重复会签")
                return []  # 同一责任人重试：幂等，不产生事件
            results = [self._append(self._next_event(
                request_id, EVENT_BILATERAL_APPROVAL_RECORDED, AGG_REQUEST,
                f"{party} 方数据责任人确认", now, party=party, by=actor["id"]))]
            state = self._state()
            request = state.requests[request_id]
            if all(p in request.approvals for p in PARTIES) and request.status == "submitted":
                results.append(self._append(self._next_event(
                    request_id, EVENT_REQUEST_APPROVED, AGG_REQUEST,
                    "中埃双边会签齐备，申请批准", now)))
            return results

    def reject_request(self, request_id: str, actor: dict[str, Any], reason: str, *,
                       now: datetime | None = None) -> dict[str, Any]:
        self._require_actor(actor, ROLE_DATA_STEWARD)
        now = now or _now()
        with self._lock:
            state = self._state()
            request = state.requests.get(request_id)
            if request is None:
                raise ServiceError(f"释出申请 {request_id} 不存在")
            if request.status != "submitted":
                raise ServiceError(f"申请当前为 {request.status}，不能拒绝")
            return self._append(self._next_event(
                request_id, EVENT_REQUEST_REJECTED, AGG_REQUEST,
                f"拒绝释出申请：{reason[:24]}", now, reason=reason))

    # -- 禁发与借展判定 -------------------------------------------------

    def _blocking_embargos(self, state: State, ref_ids: set[str], channel: str,
                           at: datetime) -> list[dict[str, Any]]:
        blockers = []
        for embargo in state.embargos.values():
            if _parse(embargo["until"]) <= at:
                continue
            if embargo["channels"] and channel not in embargo["channels"]:
                continue
            scope = set(embargo["scope_ids"])
            if ref_ids & scope:
                blockers.append(embargo)
        return blockers

    def _dependency_scope(self, state: State, refs: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
        """由引用清单求依赖的文物与原始记录集合（穿透主张与翻译）。"""
        object_ids: set[str] = set()
        record_ids: set[str] = set()
        for ref in refs:
            ref_type = ref["type"]
            if ref_type == AGG_OBJECT:
                object_ids.add(ref["id"])
            elif ref_type == AGG_RECORD:
                record_ids.add(ref["id"])
            elif ref_type == AGG_CLAIM:
                claim = state.claims.get(ref["id"])
                if claim:
                    version = claim.versions.get(ref.get("version") or max(claim.versions))
                    object_ids.update(version.get("object_ids", []))
                    record_ids.update(version.get("record_ids", []))
            elif ref_type == AGG_TRANSLATION:
                drafts = state.translations.get(ref["id"])
                if drafts:
                    claim = state.claims.get(drafts[-1]["claim_id"])
                    if claim:
                        version = claim.versions[max(claim.versions)]
                        object_ids.update(version.get("object_ids", []))
                        record_ids.update(version.get("record_ids", []))
        # 记录与其文物互相补齐。
        for obj in state.objects.values():
            if obj["record_id"] in record_ids:
                object_ids.add(obj["id"])
            if obj["id"] in object_ids and obj.get("record_id"):
                record_ids.add(obj["record_id"])
        return object_ids, record_ids

    def _check_publication_ready(self, state: State, request: RequestState,
                                 channel: str, at: datetime) -> None:
        if request.status != "approved":
            raise ServiceError(f"申请 {request.id} 尚未获批，不能生成公众版本")
        if channel not in request.channels:
            raise ServiceError(f"载体 {channel} 不在批准范围 {list(request.channels)} 内")
        refs = request.data_refs
        object_ids, record_ids = self._dependency_scope(state, refs)
        for object_id in object_ids:
            obj = state.objects[object_id]
            if channel in PHYSICAL_CHANNELS and obj["display_status"] == "withdrawn":
                raise ServiceError(f"文物 {object_id} 已撤展，不能生成依赖实体展示的 {channel}")
            loan = state.loans.get(obj.get("loan_terms_id") or "")
            if loan is not None and channel not in loan["permitted_channels"]:
                raise ServiceError(f"借展条款不允许通过 {channel} 公开文物 {object_id}")
        blockers = self._blocking_embargos(state, object_ids | record_ids, channel, at)
        if blockers:
            raise ServiceError(
                f"禁发期未到：{[(b['id'], b['until']) for b in blockers]}")

    # -- 公众版本：签发 / 更正 / 撤展暂停 / 恢复 ------------------------

    def issue_variant(self, variant_id: str, request_id: str, channel: str,
                      label_key: str, content: str, actor: dict[str, Any], *,
                      now: datetime | None = None) -> dict[str, Any]:
        self._require_actor(actor, ROLE_CURATORIAL_EDITOR)
        now = now or _now()
        with self._lock:
            state = self._state()
            request = state.requests.get(request_id)
            if request is None:
                raise ServiceError(f"释出申请 {request_id} 不存在")
            if (channel, label_key) in state.variants_by_label:
                raise ServiceError(f"{channel}/{label_key} 已有发布版本，修订须走更正流程")
            self._check_publication_ready(state, request, channel, now)
            contributors = self._collect_contributors(state, request)
            event = self._next_event(
                variant_id, EVENT_PUBLIC_VARIANT_ISSUED, AGG_VARIANT,
                f"发布公众版本：{channel}/{label_key}", now,
                request_id=request_id, channel=channel, label_key=label_key,
                content=content, frozen={"data_refs": request.data_refs},
                contributors=contributors)
            return self._append(event)

    @staticmethod
    def _collect_contributors(state: State, request: RequestState) -> list[dict[str, str]]:
        """汇总贡献署名：主张贡献人、申请列明的贡献人、双边会签责任人。"""
        contributors: dict[str, str] = {}
        for person in request.contributors:
            contributors.setdefault(person, "贡献人")
        for ref in request.data_refs:
            if ref["type"] == AGG_CLAIM:
                claim = state.claims.get(ref["id"])
                if claim is not None:
                    version = claim.versions.get(ref.get("version") or max(claim.versions))
                    for person in version["contributors"]:
                        contributors.setdefault(person, "研究贡献")
        for party, approval in request.approvals.items():
            contributors.setdefault(approval["by"], f"{party} 方数据责任人")
        return [{"id": pid, "role": role} for pid, role in sorted(contributors.items())]

    def issue_correction(self, new_variant_id: str, old_variant_id: str,
                         request_id: str, content: str, actor: dict[str, Any], *,
                         now: datetime | None = None) -> list[dict[str, Any]]:
        """发布更正版：旧版原样保留，仅挂更正指针。"""
        self._require_actor(actor, ROLE_CURATORIAL_EDITOR)
        now = now or _now()
        with self._lock:
            state = self._state()
            old = state.variants.get(old_variant_id)
            if old is None:
                raise ServiceError(f"旧版 {old_variant_id} 不存在")
            if old.corrected_by:
                raise ServiceError("旧版已有关联更正")
            request = state.requests.get(request_id)
            if request is None:
                raise ServiceError(f"释出申请 {request_id} 不存在")
            if request.revision_of is None:
                raise ServiceError("更正是内容变化后的新申请版本，须以 revision_of 关联原申请")
            if request.status != "approved":
                raise ServiceError("更正所依据的申请尚未获批")
            self._check_publication_ready(state, request, old.channel, now)
            contributors = self._collect_contributors(state, request)
            issued = self._next_event(
                new_variant_id, EVENT_PUBLIC_VARIANT_ISSUED, AGG_VARIANT,
                f"发布更正版本：{old.channel}/{old.label_key}", now,
                request_id=request_id, channel=old.channel, label_key=old.label_key,
                content=content, frozen={"data_refs": request.data_refs},
                contributors=contributors, correction_of=old_variant_id)
            results = [self._append(issued)]
            return results

    def withdraw_object(self, object_id: str, reason: str, *,
                        now: datetime | None = None) -> list[dict[str, Any]]:
        """临时撤展：暂停依赖实体展示的载体；学术论文等不受影响。"""
        now = now or _now()
        with self._lock:
            state = self._state()
            if object_id not in state.objects:
                raise ServiceError(f"文物 {object_id} 未登记")
            if state.objects[object_id]["display_status"] == "withdrawn":
                raise ServiceError("文物已处于撤展状态")
            results = [self._append(self._next_event(
                object_id, EVENT_OBJECT_WITHDRAWN, AGG_OBJECT,
                f"文物临时撤展：{reason[:24]}", now, reason=reason))]
            state = self._state()
            for variant in state.variants.values():
                if variant.channel not in PHYSICAL_CHANNELS or variant.status != "issued":
                    continue
                dep_objects, dep_records = self._dependency_scope(
                    state, variant.frozen.get("data_refs", []))
                if object_id in dep_objects:
                    suspend_reason = f"object_withdrawal:{object_id}"
                    results.append(self._append(self._next_event(
                        variant.id, EVENT_PUBLICATION_SUSPENDED, AGG_VARIANT,
                        f"因文物 {object_id} 撤展暂停实体展示载体", now, reason=suspend_reason)))
            return results

    def return_object(self, object_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or _now()
        with self._lock:
            state = self._state()
            obj = state.objects.get(object_id)
            if obj is None:
                raise ServiceError(f"文物 {object_id} 未登记")
            if obj["display_status"] != "withdrawn":
                raise ServiceError("文物不在撤展状态")
            results = [self._append(self._next_event(
                object_id, EVENT_OBJECT_RETURNED, AGG_OBJECT,
                f"文物 {object_id} 重返展厅", now))]
            state = self._state()
            for variant in state.variants.values():
                if (variant.status == "suspended"
                        and variant.suspend_reason == f"object_withdrawal:{object_id}"):
                    request = state.requests[variant.request_id]
                    # 复用发布就绪判定：任一依赖文物仍撤展、借展不允许、禁发未过都不恢复。
                    try:
                        self._check_publication_ready(state, request, variant.channel, now)
                    except ServiceError:
                        continue
                    results.append(self._append(self._next_event(
                        variant.id, EVENT_PUBLICATION_REINSTATED, AGG_VARIANT,
                        f"文物 {object_id} 返展，恢复展示", now)))
            return results
