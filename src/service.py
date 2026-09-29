"""成果释出协调服务。

把发掘单元、文物对象、原始记录、解释主张、贡献人、翻译稿、借展条款、
禁发期与公开载体连成可追踪关系，并执行以下规则：

- 释出申请提交时冻结引用数据的版本；相同申请重试复用原流程，内容变化必须新建版本；
- 中埃双方数据责任人分别确认（双边会签），并发批准也不能越过任一方；
- 策展编辑只能在批准范围内生成面向公众的版本，禁发期与撤展状态在发行时把关；
- 初步结论可标注不确定性；后续修订以更正关联旧版，曾经展出的内容不被覆盖；
- 文物临时撤展只暂停依赖实体展示条件的载体，不自动封锁已获准的学术事实；
- 贡献人异议、翻译争议、禁发期延长是彼此独立的待办；
- 事件全部落盘，服务恢复后继续未完成的会签；
- 任意公开版本可追溯到数据来源、翻译、许可、贡献署名与当时有效的研究结论。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone

from .errors import DomainError
from .model import (
    CARRIER_KINDS,
    REQUEST_APPROVED,
    REQUEST_PENDING,
    ROLE_DATA_STEWARD,
    ROLE_EDITOR,
    SIDES,
    TASK_CONTRIBUTOR_OBJECTION,
    TASK_EMBARGO_EXTENSION,
    TASK_KINDS,
    TASK_OPEN,
    TASK_RESOLVED,
    TASK_TRANSLATION_DISPUTE,
    VARIANT_ACTIVE,
    VARIANT_SUSPENDED,
    ArtifactState,
    CarrierState,
    ClaimState,
    ContributorState,
    EmbargoState,
    LoanTermsState,
    RecordState,
    RequestState,
    RequestVersionState,
    TaskState,
    TranslationState,
    UnitState,
    VariantState,
)
from .store import EventStore

_CATEGORIES = ("records", "claims", "translations")


def _parse_ref(ref: str) -> tuple[str, int | None]:
    """解析 ``id`` 或 ``id@版本`` 形式的引用。"""
    if "@" in ref:
        base, _, pin = ref.rpartition("@")
        if not base or not pin.isdigit() or int(pin) < 1:
            raise DomainError(f"引用格式错误：{ref}")
        return base, int(pin)
    return ref, None


def _parse_time(value: object, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise DomainError(f"{label} 必须是 ISO 8601 时间") from None
    if parsed.tzinfo is None:
        raise DomainError(f"{label} 必须包含时区")
    return parsed


class ReleaseCoordinationService:
    """成果释出协调服务：命令在锁内完成检查并追加事件，查询读取投影。"""

    def __init__(self, store: EventStore, clock=None):
        self._store = store
        self._clock = clock or store.clock
        self._lock = threading.RLock()
        self._units: dict[str, UnitState] = {}
        self._artifacts: dict[str, ArtifactState] = {}
        self._records: dict[str, RecordState] = {}
        self._contributors: dict[str, ContributorState] = {}
        self._loan_terms: dict[str, LoanTermsState] = {}
        self._embargoes: dict[str, EmbargoState] = {}
        self._claims: dict[str, ClaimState] = {}
        self._translations: dict[str, TranslationState] = {}
        self._carriers: dict[str, CarrierState] = {}
        self._requests: dict[str, RequestState] = {}
        self._variants: dict[str, VariantState] = {}
        self._tasks: dict[str, TaskState] = {}
        self._task_seq = 0
        for event in store.events():
            self._apply(event)

    # ------------------------------------------------------------------
    # 基础工具

    @staticmethod
    def _require(mapping: dict, key: str, label: str):
        try:
            return mapping[key]
        except KeyError:
            raise DomainError(f"{label}不存在：{key}") from None

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str, summary: str, **payload) -> dict:
        event = self._store.append(event_type, aggregate_type, aggregate_id, summary, **payload)
        self._apply(event)
        return event

    def events(self) -> list[dict]:
        return self._store.events()

    # ------------------------------------------------------------------
    # 登记：单元、文物、记录、贡献人、借展条款、禁发期、主张、翻译、载体

    def register_unit(self, unit_id: str, name: str, site: str) -> None:
        with self._lock:
            if unit_id in self._units:
                raise DomainError(f"发掘单元已存在：{unit_id}")
            self._emit("UNIT_REGISTERED", "excavation_unit", unit_id, f"登记发掘单元 {name}", name=name, site=site)

    def register_artifact(self, artifact_id: str, unit_id: str, name: str) -> None:
        with self._lock:
            if artifact_id in self._artifacts:
                raise DomainError(f"文物已存在：{artifact_id}")
            self._require(self._units, unit_id, "发掘单元")
            self._emit("ARTIFACT_REGISTERED", "artifact", artifact_id, f"登记文物 {name}", unit_id=unit_id, name=name)

    def register_contributor(self, contributor_id: str, name: str, side: str, roles: list[str]) -> None:
        with self._lock:
            if contributor_id in self._contributors:
                raise DomainError(f"贡献人已存在：{contributor_id}")
            if side not in SIDES:
                raise DomainError(f"未知方别：{side}")
            if not roles:
                raise DomainError("贡献人至少需要一个角色")
            self._emit("CONTRIBUTOR_REGISTERED", "contributor", contributor_id, f"登记贡献人 {name}",
                       name=name, side=side, roles=list(roles))

    def register_record(self, record_id: str, artifact_id: str, kind: str, content: object, registered_by: str) -> None:
        with self._lock:
            if record_id in self._records:
                raise DomainError(f"原始记录已存在：{record_id}，修订请使用 amend_record")
            self._require(self._artifacts, artifact_id, "文物")
            self._require(self._contributors, registered_by, "贡献人")
            self._emit("RECORD_REGISTERED", "excavation_record", record_id, f"登记{kind}记录",
                       artifact_id=artifact_id, kind=kind, content=content,
                       record_version=1, registered_by=registered_by)

    def amend_record(self, record_id: str, content: object, amended_by: str, reason: str = "") -> None:
        """修订原始记录：内容变化必须新建版本，旧版本保留用于追溯。"""
        with self._lock:
            record = self._require(self._records, record_id, "原始记录")
            self._require(self._contributors, amended_by, "贡献人")
            number = record.current + 1
            self._emit("RECORD_AMENDED", "excavation_record", record_id, reason or f"修订记录至第 {number} 版",
                       record_version=number, content=content, amended_by=amended_by, reason=reason)

    def record_loan_terms(self, loan_id: str, artifact_id: str, lender: str,
                          allowed_record_kinds: list[str], notes: str = "") -> None:
        with self._lock:
            self._require(self._artifacts, artifact_id, "文物")
            self._emit("LOAN_TERMS_RECORDED", "loan_terms", loan_id, f"记录 {lender} 借展条款",
                       artifact_id=artifact_id, lender=lender,
                       allowed_record_kinds=list(allowed_record_kinds), notes=notes)

    def set_embargo(self, embargo_id: str, artifact_id: str, not_before, reason: str = "") -> None:
        with self._lock:
            if embargo_id in self._embargoes:
                raise DomainError(f"禁发期已存在：{embargo_id}")
            self._require(self._artifacts, artifact_id, "文物")
            moment = _parse_time(not_before, "not_before")
            self._emit("EMBARGO_SET", "embargo", embargo_id, reason or "设置禁发期",
                       artifact_id=artifact_id, not_before=moment.isoformat(), reason=reason)

    def propose_claim(self, claim_id: str, artifact_id: str, text: str, author_id: str,
                      record_ids: list[str] | tuple = (), uncertainty: str | None = None) -> None:
        with self._lock:
            if claim_id in self._claims:
                raise DomainError(f"解释主张已存在：{claim_id}，修订请使用 correct_claim")
            self._require(self._artifacts, artifact_id, "文物")
            self._require(self._contributors, author_id, "贡献人")
            for record_id in record_ids:
                self._require(self._records, record_id, "原始记录")
            self._emit("CLAIM_PROPOSED", "research_claim", claim_id, "提出解释主张",
                       artifact_id=artifact_id, text=text, uncertainty=uncertainty,
                       author_id=author_id, record_ids=list(record_ids), claim_version=1)

    def correct_claim(self, claim_id: str, text: str, author_id: str, reason: str,
                      uncertainty: str | None = None) -> None:
        """更正解释主张：新版本关联旧版，旧版内容保留。"""
        with self._lock:
            claim = self._require(self._claims, claim_id, "解释主张")
            self._require(self._contributors, author_id, "贡献人")
            if not reason:
                raise DomainError("更正需要说明理由")
            number = claim.current + 1
            self._emit("CLAIM_CORRECTED", "research_claim", claim_id, reason,
                       text=text, uncertainty=uncertainty, author_id=author_id,
                       claim_version=number, corrects=claim.current, reason=reason)

    def submit_translation(self, translation_id: str, claim_id: str, language: str,
                           text: str, translator_id: str) -> None:
        with self._lock:
            if translation_id in self._translations:
                raise DomainError(f"翻译稿已存在：{translation_id}，修订请使用 revise_translation")
            self._require(self._claims, claim_id, "解释主张")
            self._require(self._contributors, translator_id, "贡献人")
            self._emit("TRANSLATION_SUBMITTED", "translation", translation_id, f"提交{language}译稿",
                       claim_id=claim_id, language=language, text=text,
                       translator_id=translator_id, translation_version=1)

    def revise_translation(self, translation_id: str, text: str, translator_id: str, reason: str = "") -> None:
        with self._lock:
            translation = self._require(self._translations, translation_id, "翻译稿")
            self._require(self._contributors, translator_id, "贡献人")
            number = translation.current + 1
            self._emit("TRANSLATION_REVISED", "translation", translation_id, reason or f"修订译稿至第 {number} 版",
                       text=text, translator_id=translator_id, translation_version=number, reason=reason)

    def register_carrier(self, carrier_id: str, kind: str, artifact_ids: list[str],
                         requires_physical_display: bool) -> None:
        with self._lock:
            if carrier_id in self._carriers:
                raise DomainError(f"公开载体已存在：{carrier_id}")
            if kind not in CARRIER_KINDS:
                raise DomainError(f"未知载体类型：{kind}")
            for artifact_id in artifact_ids:
                self._require(self._artifacts, artifact_id, "文物")
            self._emit("CARRIER_REGISTERED", "public_carrier", carrier_id, f"登记公开载体 {kind}",
                       kind=kind, artifact_ids=list(artifact_ids),
                       requires_physical_display=bool(requires_physical_display))

    # ------------------------------------------------------------------
    # 释出申请与双边会签

    def submit_release_request(self, key: str, submitted_by: str, summary: str,
                               record_refs: list[str] | tuple = (),
                               claim_refs: list[str] | tuple = (),
                               translation_refs: list[str] | tuple = (),
                               carrier_ids: list[str] | tuple = ()) -> dict:
        """提交释出方案并冻结引用版本。

        引用写作 ``id``（冻结当前版本）或 ``id@版本``（冻结指定版本）。
        相同申请（同一 key 且内容一致）重试复用原流程；内容变化新建版本，
        此前版本的双方确认不带入新版本。
        """
        with self._lock:
            self._require(self._contributors, submitted_by, "贡献人")
            if not summary:
                raise DomainError("释出方案需要摘要")
            declared = {
                "records": self._declared(record_refs),
                "claims": self._declared(claim_refs),
                "translations": self._declared(translation_refs),
            }
            carriers = sorted(dict.fromkeys(carrier_ids))
            for carrier_id in carriers:
                self._require(self._carriers, carrier_id, "公开载体")
            content_hash = hashlib.sha256(json.dumps(
                {"declared": declared, "carriers": carriers, "summary": summary},
                sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            existing = self._requests.get(key)
            if existing is not None and existing.version().content_hash == content_hash:
                return self.get_request(key)
            frozen = {
                "records": self._freeze(declared["records"], self._records, "原始记录"),
                "claims": self._freeze(declared["claims"], self._claims, "解释主张"),
                "translations": self._freeze(declared["translations"], self._translations, "翻译稿"),
            }
            self._check_loan_terms(frozen["records"])
            number = existing.current + 1 if existing else 1
            self._emit("RELEASE_REQUESTED", "release_request", key, summary,
                       request_version=number, declared=declared, frozen=frozen,
                       carrier_ids=carriers, submitted_by=submitted_by, content_hash=content_hash)
            return self.get_request(key)

    def _declared(self, refs) -> dict:
        declared = {}
        for ref in refs:
            rid, pin = _parse_ref(ref)
            if rid in declared:
                raise DomainError(f"重复引用：{rid}")
            declared[rid] = pin
        return declared

    def _freeze(self, declared: dict, states: dict, label: str) -> dict:
        frozen = {}
        for rid, pin in declared.items():
            state = self._require(states, rid, label)
            if pin is None:
                frozen[rid] = state.current
            elif pin in state.versions:
                frozen[rid] = pin
            else:
                raise DomainError(f"{label} {rid} 没有版本 {pin}")
        return frozen

    def _check_loan_terms(self, frozen_records: dict) -> None:
        for record_id in frozen_records:
            record = self._records[record_id]
            terms = self._loan_terms.get(record.artifact_id)
            if terms and record.kind not in terms.allowed_record_kinds:
                raise DomainError(f"借展条款不允许释出 {record.kind} 类记录：{record_id}")

    def approve(self, request_id: str, steward_id: str, request_version: int) -> dict:
        """数据责任人确认。双方各确认一次当前冻结版本后方为批准。"""
        with self._lock:
            request = self._require(self._requests, request_id, "释出申请")
            steward = self._require(self._contributors, steward_id, "贡献人")
            if ROLE_DATA_STEWARD not in steward.roles:
                raise DomainError(f"{steward_id} 不是数据责任人")
            if request_version != request.current:
                raise DomainError("申请内容已变更，需基于最新冻结版本确认")
            version = request.version()
            if steward.side in version.approvals:
                raise DomainError(f"{steward.side} 方已确认该版本")
            self._emit("BILATERAL_APPROVAL_RECORDED", "release_request", request_id,
                       f"{steward.side} 方数据责任人确认",
                       request_version=request_version, side=steward.side, steward_id=steward_id)
            if set(version.approvals) == set(SIDES):
                self._emit("RELEASE_APPROVED", "release_request", request_id,
                           "双边会签完成", request_version=request_version)
            return self.get_request(request_id)

    # ------------------------------------------------------------------
    # 公开版本

    def issue_variant(self, variant_id: str, request_id: str, carrier_id: str,
                      editor_id: str, text: str, used: dict) -> dict:
        """在批准范围内生成面向公众的版本。"""
        with self._lock:
            if variant_id in self._variants:
                raise DomainError(f"公开版本已存在：{variant_id}，修订请使用 correct_variant")
            req_version, _, used_norm = self._prepare_issue(request_id, carrier_id, editor_id, used)
            self._emit("PUBLIC_VARIANT_ISSUED", "public_variant", variant_id, f"发布公开版本 {variant_id}",
                       carrier_id=carrier_id, request_id=request_id,
                       request_version=req_version.number, editor_id=editor_id,
                       text=text, used=used_norm, variant_version=1)
            return self.get_variant(variant_id)

    def correct_variant(self, variant_id: str, request_id: str, editor_id: str,
                        text: str, used: dict, reason: str) -> dict:
        """更正已发布内容：新版本关联旧版，曾经展出的内容不被覆盖。"""
        with self._lock:
            variant = self._require(self._variants, variant_id, "公开版本")
            if not reason:
                raise DomainError("更正需要说明理由")
            req_version, _, used_norm = self._prepare_issue(request_id, variant.carrier_id, editor_id, used)
            number = variant.current + 1
            self._emit("VARIANT_CORRECTED", "public_variant", variant_id, reason,
                       request_id=request_id, request_version=req_version.number,
                       editor_id=editor_id, text=text, used=used_norm,
                       variant_version=number, corrects=variant.current, reason=reason)
            return self.get_variant(variant_id)

    def _prepare_issue(self, request_id: str, carrier_id: str, editor_id: str, used: dict):
        request = self._require(self._requests, request_id, "释出申请")
        req_version = request.version()
        if req_version.status != REQUEST_APPROVED:
            raise DomainError("双边会签未完成，不能生成公开版本")
        carrier = self._require(self._carriers, carrier_id, "公开载体")
        if carrier_id not in req_version.carrier_ids:
            raise DomainError(f"载体 {carrier_id} 不在申请 {request_id} 的批准范围")
        editor = self._require(self._contributors, editor_id, "贡献人")
        if ROLE_EDITOR not in editor.roles:
            raise DomainError("仅策展编辑可生成公开版本")
        used_norm = self._normalize_used(used)
        if not any(used_norm.values()):
            raise DomainError("公开版本必须引用批准范围内的材料")
        for category in _CATEGORIES:
            frozen = req_version.frozen[category]
            for rid, pin in used_norm[category].items():
                if rid not in frozen:
                    raise DomainError(f"{rid} 不在批准范围内")
                if frozen[rid] != pin:
                    raise DomainError(f"{rid} 的批准版本为 {frozen[rid]}，不能使用 {pin}")
        artifact_ids = list(carrier.artifact_ids)
        for record_id in used_norm["records"]:
            artifact_ids.append(self._records[record_id].artifact_id)
        for claim_id in used_norm["claims"]:
            artifact_ids.append(self._claims[claim_id].artifact_id)
        self._check_embargo(artifact_ids)
        if carrier.requires_physical_display:
            off_display = [a for a in carrier.artifact_ids if not self._artifacts[a].on_display]
            if off_display:
                raise DomainError(f"文物已撤展，依赖实体展示的载体暂停：{', '.join(off_display)}")
        return req_version, carrier, used_norm

    def _normalize_used(self, used: dict) -> dict:
        unknown = set(used) - set(_CATEGORIES)
        if unknown:
            raise DomainError(f"未知引用类别：{', '.join(sorted(unknown))}")
        normalized = {category: {} for category in _CATEGORIES}
        for category in _CATEGORIES:
            for ref in used.get(category, ()):
                rid, pin = _parse_ref(ref)
                if pin is None:
                    raise DomainError(f"公开版本必须标注引用版本：{ref}")
                normalized[category][rid] = pin
        return normalized

    def _check_embargo(self, artifact_ids) -> None:
        now = self._clock()
        for artifact_id in sorted(set(artifact_ids)):
            for embargo in self._embargoes.values():
                if embargo.artifact_id == artifact_id and embargo.not_before > now:
                    raise DomainError(f"文物 {artifact_id} 禁发期至 {embargo.not_before.isoformat()}")

    # ------------------------------------------------------------------
    # 撤展与复展

    def withdraw_artifact(self, artifact_id: str, reason: str = "") -> list[str]:
        """临时撤展：只暂停依赖实体展示条件的载体，不封锁已获准的学术事实。"""
        with self._lock:
            artifact = self._require(self._artifacts, artifact_id, "文物")
            if not artifact.on_display:
                raise DomainError(f"文物 {artifact_id} 已处于撤展状态")
            self._emit("ARTIFACT_WITHDRAWN", "artifact", artifact_id, reason or "临时撤展", reason=reason)
            suspended = []
            for carrier in self._carriers.values():
                if not carrier.requires_physical_display or artifact_id not in carrier.artifact_ids:
                    continue
                for variant in self._variants.values():
                    if variant.carrier_id == carrier.carrier_id and variant.status == VARIANT_ACTIVE:
                        self._emit("VARIANT_SUSPENDED", "public_variant", variant.variant_id,
                                   f"文物 {artifact_id} 撤展，暂停实体展示载体",
                                   variant_version=variant.current, artifact_id=artifact_id,
                                   reason=reason or "临时撤展")
                        suspended.append(variant.variant_id)
            return suspended

    def reinstate_artifact(self, artifact_id: str) -> list[str]:
        with self._lock:
            artifact = self._require(self._artifacts, artifact_id, "文物")
            if artifact.on_display:
                raise DomainError(f"文物 {artifact_id} 并未撤展")
            self._emit("ARTIFACT_REINSTATED", "artifact", artifact_id, "恢复展出")
            resumed = []
            for variant in self._variants.values():
                if variant.status != VARIANT_SUSPENDED:
                    continue
                carrier = self._carriers[variant.carrier_id]
                if not carrier.requires_physical_display:
                    continue
                if all(self._artifacts[a].on_display for a in carrier.artifact_ids):
                    self._emit("VARIANT_RESUMED", "public_variant", variant.variant_id,
                               "撤展解除，恢复展示",
                               variant_version=variant.current, artifact_id=artifact_id)
                    resumed.append(variant.variant_id)
            return resumed

    # ------------------------------------------------------------------
    # 独立待办：贡献人异议、翻译争议、禁发期延长

    def open_task(self, kind: str, opened_by: str, target: dict, detail: str = "") -> dict:
        with self._lock:
            if kind not in TASK_KINDS:
                raise DomainError(f"未知待办类型：{kind}")
            self._require(self._contributors, opened_by, "贡献人")
            target = self._validate_task_target(kind, target)
            self._task_seq += 1
            task_id = f"task-{self._task_seq:06d}"
            self._emit("TASK_OPENED", "coordination_task", task_id, detail or f"开立待办 {kind}",
                       kind=kind, opened_by=opened_by, target=target, detail=detail)
            return self.get_task(task_id)

    def _validate_task_target(self, kind: str, target: dict) -> dict:
        target = dict(target or {})
        if kind == TASK_CONTRIBUTOR_OBJECTION:
            if "variant_id" in target:
                self._require(self._variants, target["variant_id"], "公开版本")
            elif "claim_id" in target:
                self._require(self._claims, target["claim_id"], "解释主张")
            else:
                raise DomainError("贡献人异议需要指定公开版本或解释主张")
            return target
        if kind == TASK_TRANSLATION_DISPUTE:
            self._require(self._translations, target.get("translation_id", ""), "翻译稿")
            return target
        embargo = self._require(self._embargoes, target.get("embargo_id", ""), "禁发期")
        requested = _parse_time(target.get("requested_not_before"), "requested_not_before")
        return {"embargo_id": embargo.embargo_id, "requested_not_before": requested.isoformat()}

    def resolve_task(self, task_id: str, resolver_id: str, note: str = "",
                     approved: bool = True, resolution: dict | None = None) -> dict:
        with self._lock:
            task = self._require(self._tasks, task_id, "待办")
            if task.status != TASK_OPEN:
                raise DomainError(f"待办 {task_id} 已关闭")
            self._require(self._contributors, resolver_id, "贡献人")
            self._emit("TASK_RESOLVED", "coordination_task", task_id, note or "待办处理完成",
                       resolver_id=resolver_id, note=note, approved=approved,
                       resolution=dict(resolution or {}))
            if approved and task.kind == TASK_EMBARGO_EXTENSION:
                embargo = self._embargoes[task.target["embargo_id"]]
                self._emit("EMBARGO_EXTENDED", "embargo", embargo.embargo_id, "禁发期延长",
                           artifact_id=embargo.artifact_id,
                           not_before=task.target["requested_not_before"],
                           previous_not_before=embargo.not_before.isoformat(),
                           task_id=task_id)
            return self.get_task(task_id)

    # ------------------------------------------------------------------
    # 查询

    def get_request(self, request_id: str) -> dict:
        with self._lock:
            request = self._require(self._requests, request_id, "释出申请")
            version = request.version()
            return {
                "request_id": request.request_id,
                "request_version": version.number,
                "status": version.status,
                "summary": version.summary,
                "submitted_by": version.submitted_by,
                "carrier_ids": list(version.carrier_ids),
                "frozen": {category: dict(refs) for category, refs in version.frozen.items()},
                "declared": {category: dict(refs) for category, refs in version.declared.items()},
                "approvals": {side: dict(entry) for side, entry in version.approvals.items()},
                "content_hash": version.content_hash,
                "versions": sorted(request.versions),
            }

    def get_variant(self, variant_id: str) -> dict:
        with self._lock:
            variant = self._require(self._variants, variant_id, "公开版本")
            return {
                "variant_id": variant.variant_id,
                "carrier_id": variant.carrier_id,
                "current": variant.current,
                "status": variant.status,
                "suspended_reason": variant.suspended_reason,
                "versions": {number: dict(entry) for number, entry in variant.versions.items()},
            }

    def get_claim(self, claim_id: str) -> dict:
        with self._lock:
            claim = self._require(self._claims, claim_id, "解释主张")
            return {
                "claim_id": claim.claim_id,
                "artifact_id": claim.artifact_id,
                "author_id": claim.author_id,
                "record_ids": list(claim.record_ids),
                "current": claim.current,
                "versions": {number: dict(entry) for number, entry in claim.versions.items()},
            }

    def get_task(self, task_id: str) -> dict:
        with self._lock:
            return self._task_view(self._require(self._tasks, task_id, "待办"))

    def pending_tasks(self) -> list[dict]:
        with self._lock:
            return [self._task_view(task) for task in self._tasks.values() if task.status == TASK_OPEN]

    @staticmethod
    def _task_view(task: TaskState) -> dict:
        return {
            "task_id": task.task_id,
            "kind": task.kind,
            "opened_by": task.opened_by,
            "target": dict(task.target),
            "detail": task.detail,
            "status": task.status,
            "resolution": dict(task.resolution) if task.resolution else None,
        }

    def trace_variant(self, variant_id: str, version: int | None = None) -> dict:
        """从一条公开内容（如一句展签）追溯来源、翻译、许可、署名与当时有效的结论。"""
        with self._lock:
            variant = self._require(self._variants, variant_id, "公开版本")
            number = version or variant.current
            if number not in variant.versions:
                raise DomainError(f"公开版本 {variant_id} 没有版本 {number}")
            shown = variant.versions[number]
            carrier = self._carriers[variant.carrier_id]
            request = self._requests[shown["request_id"]]
            req_version = request.versions[shown["request_version"]]
            used = shown["used"]
            contributors: dict[str, dict] = {}

            def credit(contributor_id: str, capacity: str) -> None:
                contributor = self._contributors.get(contributor_id)
                if contributor is None:
                    return
                entry = contributors.setdefault(contributor_id, {
                    "contributor_id": contributor_id,
                    "name": contributor.name,
                    "side": contributor.side,
                    "capacities": [],
                })
                if capacity not in entry["capacities"]:
                    entry["capacities"].append(capacity)

            records = []
            for record_id, record_version in used["records"].items():
                record = self._records[record_id]
                entry = record.versions[record_version]
                records.append({
                    "record_id": record_id,
                    "version": record_version,
                    "kind": record.kind,
                    "artifact_id": record.artifact_id,
                    "registered_by": entry["registered_by"],
                })
                credit(entry["registered_by"], "记录人")
            claims = []
            for claim_id, claim_version in used["claims"].items():
                claim = self._claims[claim_id]
                entry = claim.versions[claim_version]
                claims.append({
                    "claim_id": claim_id,
                    "version": claim_version,
                    "text": entry["text"],
                    "uncertainty": entry["uncertainty"],
                    "author_id": entry["author_id"],
                    "corrects": entry["corrects"],
                    "corrected_by": claim_version + 1 if claim_version < claim.current else None,
                    "superseded": claim_version < claim.current,
                })
                credit(entry["author_id"], "作者")
            translations = []
            for translation_id, translation_version in used["translations"].items():
                translation = self._translations[translation_id]
                entry = translation.versions[translation_version]
                translations.append({
                    "translation_id": translation_id,
                    "version": translation_version,
                    "language": translation.language,
                    "claim_id": translation.claim_id,
                    "translator_id": entry["translator_id"],
                })
                credit(entry["translator_id"], "译者")
            approvals = []
            for side, entry in sorted(req_version.approvals.items()):
                steward = self._contributors.get(entry["steward_id"])
                approvals.append({
                    "side": side,
                    "steward_id": entry["steward_id"],
                    "steward": steward.name if steward else entry["steward_id"],
                    "occurred_at": entry["occurred_at"],
                })
                credit(entry["steward_id"], "数据责任人")
            credit(shown["editor_id"], "策展编辑")
            return {
                "variant": {
                    "variant_id": variant_id,
                    "version": number,
                    "text": shown["text"],
                    "status": variant.status,
                    "issued_at": shown["occurred_at"],
                    "editor_id": shown["editor_id"],
                    "corrects": shown["corrects"],
                    "corrected_by": number + 1 if number < variant.current else None,
                    "request_id": shown["request_id"],
                    "request_version": shown["request_version"],
                },
                "carrier": {
                    "carrier_id": carrier.carrier_id,
                    "kind": carrier.kind,
                    "requires_physical_display": carrier.requires_physical_display,
                    "artifact_ids": list(carrier.artifact_ids),
                },
                "artifacts": [
                    {"artifact_id": a.artifact_id, "name": a.name,
                     "unit_id": a.unit_id, "on_display": a.on_display}
                    for a in (self._artifacts[i] for i in carrier.artifact_ids if i in self._artifacts)
                ],
                "request": {
                    "request_id": request.request_id,
                    "request_version": req_version.number,
                    "status": req_version.status,
                    "summary": req_version.summary,
                    "submitted_by": req_version.submitted_by,
                },
                "approvals": approvals,
                "records": records,
                "claims": claims,
                "translations": translations,
                "contributors": sorted(contributors.values(), key=lambda item: item["contributor_id"]),
            }

    # ------------------------------------------------------------------
    # 事件投影

    def _apply(self, event: dict) -> None:
        handler = getattr(self, f"_on_{event['event_type']}", None)
        if handler is None:
            raise DomainError(f"未知事件类型：{event['event_type']}")
        handler(event)

    def _on_UNIT_REGISTERED(self, e: dict) -> None:
        self._units[e["aggregate_id"]] = UnitState(e["aggregate_id"], e["name"], e["site"])

    def _on_ARTIFACT_REGISTERED(self, e: dict) -> None:
        self._artifacts[e["aggregate_id"]] = ArtifactState(e["aggregate_id"], e["unit_id"], e["name"])

    def _on_RECORD_REGISTERED(self, e: dict) -> None:
        self._records[e["aggregate_id"]] = RecordState(
            e["aggregate_id"], e["artifact_id"], e["kind"],
            versions={e["record_version"]: {"content": e["content"],
                                            "registered_by": e["registered_by"],
                                            "occurred_at": e["occurred_at"]}},
            current=e["record_version"])

    def _on_RECORD_AMENDED(self, e: dict) -> None:
        record = self._records[e["aggregate_id"]]
        record.versions[e["record_version"]] = {"content": e["content"],
                                                "registered_by": e["amended_by"],
                                                "occurred_at": e["occurred_at"],
                                                "reason": e.get("reason", "")}
        record.current = e["record_version"]

    def _on_CONTRIBUTOR_REGISTERED(self, e: dict) -> None:
        self._contributors[e["aggregate_id"]] = ContributorState(
            e["aggregate_id"], e["name"], e["side"], list(e["roles"]))

    def _on_LOAN_TERMS_RECORDED(self, e: dict) -> None:
        self._loan_terms[e["artifact_id"]] = LoanTermsState(
            e["aggregate_id"], e["artifact_id"], e["lender"],
            list(e["allowed_record_kinds"]), e.get("notes", ""))

    def _on_EMBARGO_SET(self, e: dict) -> None:
        self._embargoes[e["aggregate_id"]] = EmbargoState(
            e["aggregate_id"], e["artifact_id"],
            datetime.fromisoformat(e["not_before"]), e.get("reason", ""))

    def _on_EMBARGO_EXTENDED(self, e: dict) -> None:
        embargo = self._embargoes[e["aggregate_id"]]
        embargo.extensions.append({
            "not_before": e["not_before"],
            "previous_not_before": e["previous_not_before"],
            "task_id": e["task_id"],
            "occurred_at": e["occurred_at"],
        })
        embargo.not_before = datetime.fromisoformat(e["not_before"])

    def _on_CLAIM_PROPOSED(self, e: dict) -> None:
        self._claims[e["aggregate_id"]] = ClaimState(
            e["aggregate_id"], e["artifact_id"], e["author_id"], list(e["record_ids"]),
            versions={e["claim_version"]: self._claim_version(e)}, current=e["claim_version"])

    def _on_CLAIM_CORRECTED(self, e: dict) -> None:
        claim = self._claims[e["aggregate_id"]]
        claim.versions[e["claim_version"]] = self._claim_version(e)
        claim.current = e["claim_version"]

    @staticmethod
    def _claim_version(e: dict) -> dict:
        return {"text": e["text"], "uncertainty": e["uncertainty"],
                "author_id": e["author_id"], "corrects": e.get("corrects"),
                "occurred_at": e["occurred_at"]}

    def _on_TRANSLATION_SUBMITTED(self, e: dict) -> None:
        self._translations[e["aggregate_id"]] = TranslationState(
            e["aggregate_id"], e["claim_id"], e["language"], e["translator_id"],
            versions={e["translation_version"]: self._translation_version(e)},
            current=e["translation_version"])

    def _on_TRANSLATION_REVISED(self, e: dict) -> None:
        translation = self._translations[e["aggregate_id"]]
        translation.versions[e["translation_version"]] = self._translation_version(e)
        translation.current = e["translation_version"]

    @staticmethod
    def _translation_version(e: dict) -> dict:
        return {"text": e["text"], "translator_id": e["translator_id"],
                "occurred_at": e["occurred_at"]}

    def _on_CARRIER_REGISTERED(self, e: dict) -> None:
        self._carriers[e["aggregate_id"]] = CarrierState(
            e["aggregate_id"], e["kind"], list(e["artifact_ids"]),
            bool(e["requires_physical_display"]))

    def _on_RELEASE_REQUESTED(self, e: dict) -> None:
        request = self._requests.get(e["aggregate_id"])
        version = RequestVersionState(
            number=e["request_version"], declared=e["declared"], frozen=e["frozen"],
            content_hash=e["content_hash"], carrier_ids=list(e["carrier_ids"]),
            submitted_by=e["submitted_by"], summary=e["summary"])
        if request is None:
            request = RequestState(e["aggregate_id"])
            self._requests[e["aggregate_id"]] = request
        request.versions[version.number] = version
        request.current = version.number

    def _on_BILATERAL_APPROVAL_RECORDED(self, e: dict) -> None:
        version = self._requests[e["aggregate_id"]].versions[e["request_version"]]
        version.approvals[e["side"]] = {"steward_id": e["steward_id"],
                                        "occurred_at": e["occurred_at"]}

    def _on_RELEASE_APPROVED(self, e: dict) -> None:
        self._requests[e["aggregate_id"]].versions[e["request_version"]].status = REQUEST_APPROVED

    def _on_PUBLIC_VARIANT_ISSUED(self, e: dict) -> None:
        self._variants[e["aggregate_id"]] = VariantState(
            e["aggregate_id"], e["carrier_id"],
            versions={e["variant_version"]: self._variant_version(e)},
            current=e["variant_version"])

    def _on_VARIANT_CORRECTED(self, e: dict) -> None:
        variant = self._variants[e["aggregate_id"]]
        variant.versions[e["variant_version"]] = self._variant_version(e)
        variant.current = e["variant_version"]

    @staticmethod
    def _variant_version(e: dict) -> dict:
        return {"text": e["text"], "used": e["used"], "request_id": e["request_id"],
                "request_version": e["request_version"], "editor_id": e["editor_id"],
                "corrects": e.get("corrects"), "occurred_at": e["occurred_at"]}

    def _on_VARIANT_SUSPENDED(self, e: dict) -> None:
        variant = self._variants[e["aggregate_id"]]
        variant.status = VARIANT_SUSPENDED
        variant.suspended_reason = e["reason"]
        variant.suspended_artifact_id = e["artifact_id"]

    def _on_VARIANT_RESUMED(self, e: dict) -> None:
        variant = self._variants[e["aggregate_id"]]
        variant.status = VARIANT_ACTIVE
        variant.suspended_reason = None
        variant.suspended_artifact_id = None

    def _on_ARTIFACT_WITHDRAWN(self, e: dict) -> None:
        self._artifacts[e["aggregate_id"]].on_display = False

    def _on_ARTIFACT_REINSTATED(self, e: dict) -> None:
        self._artifacts[e["aggregate_id"]].on_display = True

    def _on_TASK_OPENED(self, e: dict) -> None:
        self._tasks[e["aggregate_id"]] = TaskState(
            e["aggregate_id"], e["kind"], e["opened_by"], dict(e["target"]),
            e.get("detail", ""))
        self._task_seq = max(self._task_seq, int(e["aggregate_id"].rsplit("-", 1)[1]))

    def _on_TASK_RESOLVED(self, e: dict) -> None:
        task = self._tasks[e["aggregate_id"]]
        task.status = TASK_RESOLVED
        task.resolution = {"resolver_id": e["resolver_id"], "note": e["note"],
                           "approved": e["approved"], **e["resolution"]}
