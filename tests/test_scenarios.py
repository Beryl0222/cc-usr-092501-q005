"""场景测试：局部撤展、禁发延期、争议解决、已发布更正、
重试复用、并发会签、崩溃恢复与一句展签溯源。"""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.events import (
    AGG_REQUEST,
    CHANNEL_ACADEMIC_PAPER,
    CHANNEL_CATALOG,
    CHANNEL_GALLERY_LABEL,
    EVENT_BILATERAL_APPROVAL_RECORDED,
    make_event,
)
from src.model import reduce_events
from src.service import (
    ROLE_CURATORIAL_EDITOR,
    ROLE_DATA_STEWARD,
    ReleaseCoordinationService,
    ServiceError,
)
from src.store import ConcurrencyError, EventStore
from src.trace import Tracer

CN = {"id": "steward-cn-li", "role": ROLE_DATA_STEWARD}
EG = {"id": "steward-eg-hassan", "role": ROLE_DATA_STEWARD}
EDITOR = {"id": "editor-wang", "role": ROLE_CURATORIAL_EDITOR}
OTHER_CN = {"id": "steward-cn-zhao", "role": ROLE_DATA_STEWARD}

BASE = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
PUBLIC_AT = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)

RECORD_DATA_V1 = {
    "unit_id": "T3",
    "photos": ["IMG_0021.cr3"],
    "measurements": {"height_mm": 42.3, "width_mm": 31.0},
    "stratigraphy": "第 4 层",
}


def build_world(path: str | Path, now: datetime = BASE) -> ReleaseCoordinationService:
    svc = ReleaseCoordinationService(EventStore(path))
    svc.register_record("rec-0021", "T3", "3 号探方出土原始记录", RECORD_DATA_V1, now=now)
    svc.register_loan_terms(
        "loan-77", "obj-005", "借展条款：展签/图录/讲座/论文均可释出，撤展期间实体载体暂停",
        [CHANNEL_GALLERY_LABEL, CHANNEL_CATALOG, "public_lecture", CHANNEL_ACADEMIC_PAPER],
        now=now)
    svc.register_object("obj-005", "rec-0021", "青铜护身符",
                        loan_terms_id="loan-77", now=now)
    svc.propose_claim(
        "claim-9", "年代初步判断为中王国时期，证据仍在整理",
        "provisional", ["researcher-ma", "researcher-nada"],
        record_ids=["rec-0021"], object_ids=["obj-005"], now=now)
    svc.draft_translation(
        "tr-9", "claim-9", "ar", "تقدير مبدئي: عصر الدولة الوسطى",
        "translator-nada", now=now)
    return svc


def request_refs(claim_version: int = 1) -> list[dict]:
    return [
        {"type": "excavation_record", "id": "rec-0021", "version": 1},
        {"type": "research_claim", "id": "claim-9", "version": claim_version},
        {"type": "translation", "id": "tr-9"},
    ]


def approve_both(svc: ReleaseCoordinationService, request_id: str, now: datetime) -> None:
    svc.record_approval(request_id, "CN", CN, now=now)
    svc.record_approval(request_id, "EG", EG, now=now + timedelta(minutes=5))


class ScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- 场景 1：局部撤展 ----------------------------------------------

    def test_partial_withdrawal_only_suspends_physical_channels(self) -> None:
        svc = build_world(self.path)
        svc.submit_release_request(
            "req-1", [CHANNEL_GALLERY_LABEL, CHANNEL_ACADEMIC_PAPER],
            request_refs(), ["researcher-ma"], now=BASE + timedelta(days=1))
        approve_both(svc, "req-1", BASE + timedelta(days=2))
        svc.issue_variant("var-label", "req-1", CHANNEL_GALLERY_LABEL,
                          "obj-005-intro", "展签：中王国时期青铜护身符（初步判断）",
                          EDITOR, now=PUBLIC_AT)
        svc.issue_variant("var-paper", "req-1", CHANNEL_ACADEMIC_PAPER,
                          "paper-fig-12", "论文图版及初步年代结论",
                          EDITOR, now=PUBLIC_AT)

        svc.withdraw_object("obj-005", "展厅恒温系统检修", now=PUBLIC_AT + timedelta(days=1))
        state = svc._state()

        # 实体载体（展签）暂停；学术事实（论文）不被自动封锁。
        self.assertEqual(state.variants["var-label"].status, "suspended")
        self.assertTrue(state.variants["var-label"].suspend_reason.startswith("object_withdrawal:"))
        self.assertEqual(state.variants["var-paper"].status, "issued")
        # 已获准的申请与学术事实仍然有效。
        self.assertEqual(state.requests["req-1"].status, "approved")
        self.assertEqual(state.objects["obj-005"]["display_status"], "withdrawn")

        # 撤展期间不能新发展签，但论文仍可签发。
        with self.assertRaisesRegex(ServiceError, "已撤展"):
            svc.issue_variant("var-label-2", "req-1", CHANNEL_GALLERY_LABEL,
                              "obj-005-side", "撤展期间的新展签", EDITOR,
                              now=PUBLIC_AT + timedelta(days=1, hours=1))
        svc.issue_variant("var-paper-2", "req-1", CHANNEL_ACADEMIC_PAPER,
                          "paper-table-3", "补充测量数据表", EDITOR,
                          now=PUBLIC_AT + timedelta(days=1, hours=2))

        # 文物返展后展签自动恢复。
        svc.return_object("obj-005", now=PUBLIC_AT + timedelta(days=3))
        state = svc._state()
        self.assertEqual(state.objects["obj-005"]["display_status"], "on_display")
        self.assertEqual(state.variants["var-label"].status, "issued")

    def test_return_during_embargo_keeps_label_suspended(self) -> None:
        svc = build_world(self.path)
        svc.submit_release_request("req-1", [CHANNEL_GALLERY_LABEL], request_refs(),
                                   ["researcher-ma"], now=BASE + timedelta(days=1))
        approve_both(svc, "req-1", BASE + timedelta(days=2))
        svc.issue_variant("var-label", "req-1", CHANNEL_GALLERY_LABEL, "obj-005-intro",
                          "已发布展签", EDITOR, now=BASE + timedelta(days=3))
        # 发布之后登记/延长的禁发期（独立流程），随后文物撤展导致展签暂停。
        svc.register_embargo("emb-1", ["obj-005"], BASE + timedelta(days=60),
                             channels=[CHANNEL_GALLERY_LABEL], now=BASE + timedelta(days=4))
        svc.withdraw_object("obj-005", "外借点交", now=BASE + timedelta(days=5))
        self.assertEqual(svc._state().variants["var-label"].status, "suspended")

        # 禁发期未过即返展：展签保持暂停，不自动恢复。
        events = svc.return_object("obj-005", now=BASE + timedelta(days=10))
        kinds = [e["event_type"] for e in events]
        self.assertIn("OBJECT_RETURNED", kinds)
        self.assertNotIn("PUBLICATION_REINSTATED", kinds)
        self.assertEqual(svc._state().variants["var-label"].status, "suspended")

    # -- 场景 2：禁发延期 ----------------------------------------------

    def test_embargo_extension_blocks_until_new_date(self) -> None:
        svc = build_world(self.path)
        svc.register_embargo("emb-1", ["obj-005", "rec-0021"],
                             BASE + timedelta(days=30),
                             channels=[CHANNEL_GALLERY_LABEL, CHANNEL_CATALOG], now=BASE)
        svc.submit_release_request(
            "req-1", [CHANNEL_GALLERY_LABEL, CHANNEL_CATALOG], request_refs(),
            ["researcher-ma"], now=BASE + timedelta(days=1))
        approve_both(svc, "req-1", BASE + timedelta(days=2))

        # 禁发期内不能发布。
        with self.assertRaisesRegex(ServiceError, "禁发期未到"):
            svc.issue_variant("v1", "req-1", CHANNEL_GALLERY_LABEL, "k1", "内容",
                              EDITOR, now=BASE + timedelta(days=10))

        # 延期申请形成独立待办；批准前不溯及生效：原期限（30 天）过后、待办未决时不阻断。
        task = svc.request_embargo_extension(
            "emb-1", BASE + timedelta(days=90), "埃方未发表研究需更多时间",
            requested_by="steward-eg-hassan", now=BASE + timedelta(days=12))
        task_id = task["aggregate_id"]
        self.assertEqual(svc._state().tasks[task_id]["status"], "open")
        # 待办尚未批准：第 40 天已过原期限，发布不被延期申请追溯阻断。
        svc.issue_variant("v-interim", "req-1", CHANNEL_CATALOG, "interim-note",
                          "原期限后的图录条目", EDITOR, now=BASE + timedelta(days=40))

        # 批准延期后，新期限前再次阻断（作用于尚未发布的条目）。
        svc.decide_embargo_extension(task_id, True, now=BASE + timedelta(days=13))
        state = svc._state()
        self.assertEqual(state.tasks[task_id]["status"], "closed")
        self.assertEqual(len(state.embargos["emb-1"]["history"]), 2)

        # 原期限之后、新期限之前仍阻断；新期限之后放行。
        with self.assertRaisesRegex(ServiceError, "禁发期未到"):
            svc.issue_variant("v1", "req-1", CHANNEL_GALLERY_LABEL, "k1", "内容",
                              EDITOR, now=BASE + timedelta(days=60))
        svc.issue_variant("v1", "req-1", CHANNEL_GALLERY_LABEL, "k1", "正式展签",
                          EDITOR, now=BASE + timedelta(days=91))
        self.assertEqual(svc._state().variants["v1"].status, "issued")

    def test_embargo_extension_denied_keeps_original_until(self) -> None:
        svc = build_world(self.path)
        svc.register_embargo("emb-1", ["obj-005"], BASE + timedelta(days=30),
                             channels=[CHANNEL_CATALOG], now=BASE)
        task = svc.request_embargo_extension(
            "emb-1", BASE + timedelta(days=90), "单方请求",
            requested_by="x", now=BASE + timedelta(days=5))
        svc.decide_embargo_extension(task["aggregate_id"], False,
                                     resolution="理由不充分", now=BASE + timedelta(days=6))
        embargo = svc._state().embargos["emb-1"]
        self.assertEqual(len(embargo["history"]), 1)  # 未写入 EMBARGO_EXTENDED
        self.assertEqual(embargo["until"], (BASE + timedelta(days=30)).isoformat())

    # -- 场景 3：争议解决（异议与翻译争议各自独立待办） ----------------

    def test_disputes_have_independent_tasks_and_lifecycles(self) -> None:
        svc = build_world(self.path)
        _, task_obj = svc.file_dispute(
            "dsp-contrib", "contributor_objection", "research_claim", "claim-9",
            "researcher-ma", "要求暂缓公开以核实署名顺序", now=BASE + timedelta(days=2))
        _, task_tr = svc.file_dispute(
            "dsp-trans", "translation_dispute", "translation", "tr-9",
            "translator-nada", "阿语译名存在歧义", now=BASE + timedelta(days=2, hours=1))
        self.assertNotEqual(task_obj["aggregate_id"], task_tr["aggregate_id"])

        state = svc._state()
        self.assertEqual(state.tasks[task_obj["aggregate_id"]]["status"], "open")
        self.assertEqual(state.tasks[task_tr["aggregate_id"]]["status"], "open")

        # 解决贡献人异议只关闭它自己的待办，翻译争议待办不受影响。
        svc.resolve_dispute("dsp-contrib", "与贡献人确认署名顺序",
                            now=BASE + timedelta(days=3))
        state = svc._state()
        self.assertEqual(state.disputes["dsp-contrib"]["status"], "resolved")
        self.assertEqual(state.tasks[task_obj["aggregate_id"]]["status"], "closed")
        self.assertEqual(state.disputes["dsp-trans"]["status"], "open")
        self.assertEqual(state.tasks[task_tr["aggregate_id"]]["status"], "open")

        # 重复开立同一主题的未决争议被拒绝；已解决后可重新提出。
        with self.assertRaisesRegex(ServiceError, "已有未解决争议"):
            svc.file_dispute("dsp-trans-2", "translation_dispute", "translation", "tr-9",
                             "nada", "再次提出", now=BASE + timedelta(days=4))
        svc.resolve_dispute("dsp-trans", "采用双方商定译名", now=BASE + timedelta(days=5))
        svc.file_dispute("dsp-trans-3", "translation_dispute", "translation", "tr-9",
                         "nada", "译名修订后续", now=BASE + timedelta(days=6))

    # -- 场景 4：已发布更正 --------------------------------------------

    def test_published_correction_links_versions_without_overwriting(self) -> None:
        svc = build_world(self.path)
        svc.submit_release_request("req-1", [CHANNEL_GALLERY_LABEL], request_refs(),
                                   ["researcher-ma"], now=BASE + timedelta(days=1))
        approve_both(svc, "req-1", BASE + timedelta(days=2))
        svc.issue_variant("var-v1", "req-1", CHANNEL_GALLERY_LABEL, "obj-005-intro",
                          "中王国时期（初步判断）", EDITOR, now=PUBLIC_AT)

        # 研究推进：主张出新版（旧版保留）。
        svc.correct_claim(
            "claim-9", "修订为第二中间期，碳样测年支持更晚年代", "confirmed",
            ["researcher-ma", "researcher-nada"], reason="碳十四结果返回",
            record_ids=["rec-0021"], object_ids=["obj-005"],
            now=PUBLIC_AT + timedelta(days=10))

        # 内容变化必须以修订方式新建申请版本。
        with self.assertRaisesRegex(ServiceError, "内容变化必须用新编号新建版本"):
            svc.submit_release_request("req-1", [CHANNEL_GALLERY_LABEL],
                                       request_refs(claim_version=2), ["researcher-ma"],
                                       now=PUBLIC_AT + timedelta(days=11))
        svc.submit_release_request(
            "req-2", [CHANNEL_GALLERY_LABEL], request_refs(claim_version=2),
            ["researcher-ma"], revision_of="req-1", now=PUBLIC_AT + timedelta(days=11))
        approve_both(svc, "req-2", PUBLIC_AT + timedelta(days=12))

        # 更正必须挂在修订申请上；不能用原申请直接发更正。
        with self.assertRaisesRegex(ServiceError, "revision_of"):
            svc.issue_correction("var-v2", "var-v1", "req-1", "新版", EDITOR,
                                 now=PUBLIC_AT + timedelta(days=13))

        svc.issue_correction("var-v2", "var-v1", "req-2",
                             "第二中间期（据碳十四修订）", EDITOR,
                             now=PUBLIC_AT + timedelta(days=13))
        state = svc._state()
        # 旧版内容原样保留，仅挂更正指针。
        self.assertEqual(state.variants["var-v1"].content, "中王国时期（初步判断）")
        self.assertEqual(state.variants["var-v1"].corrected_by, "var-v2")
        self.assertEqual(state.variants["var-v2"].correction_of, "var-v1")
        # 同一展签键指向当前最新版。
        self.assertEqual(state.variants_by_label[(CHANNEL_GALLERY_LABEL, "obj-005-intro")],
                         "var-v2")
        # 不能对旧版重复更正。
        with self.assertRaisesRegex(ServiceError, "已有关联更正"):
            svc.issue_correction("var-v3", "var-v1", "req-2", "再改", EDITOR,
                                 now=PUBLIC_AT + timedelta(days=14))

        # 旧版溯源仍展示当时展出的内容，并能看到后续更正。
        old_trace = Tracer(svc.store).trace_variant("var-v1")
        self.assertEqual(old_trace["variant"]["content"], "中王国时期（初步判断）")
        self.assertEqual(old_trace["variant"]["corrected_by"], "var-v2")
        claim_src = next(s for s in old_trace["sources"]["claims"] if s["id"] == "claim-9")
        self.assertEqual(claim_src["frozen_version"], 1)
        self.assertEqual(len(claim_src["later_corrections"]), 1)
        self.assertEqual(claim_src["later_corrections"][0]["version"], 2)

    # -- 场景 5：相同申请重试复用，内容变化新建版本 --------------------

    def test_identical_retry_reuses_flow_but_changed_content_versions(self) -> None:
        svc = build_world(self.path)
        req, reused = svc.submit_release_request(
            "req-1", [CHANNEL_GALLERY_LABEL], request_refs(), ["researcher-ma"],
            client_ref="POST /releases #7781", now=BASE + timedelta(days=1))
        self.assertFalse(reused)
        before = len(svc.store.events)

        # 网络重试：相同内容、相同编号 → 复用，不产生任何事件。
        again, reused = svc.submit_release_request(
            "req-1", [CHANNEL_GALLERY_LABEL], request_refs(), ["researcher-ma"],
            client_ref="POST /releases #7781", now=BASE + timedelta(days=1, minutes=1))
        self.assertTrue(reused)
        self.assertEqual(again.id, "req-1")
        self.assertEqual(len(svc.store.events), before)

        # 相同内容换编号重试（客户端丢了编号）也回到同一流程。
        other, reused = svc.submit_release_request(
            "req-retry-x", [CHANNEL_GALLERY_LABEL], request_refs(), ["researcher-ma"],
            now=BASE + timedelta(days=1, minutes=2))
        self.assertTrue(reused)
        self.assertEqual(other.id, "req-1")
        self.assertEqual(len(svc.store.events), before)

        # 载体顺序变化不构成内容变化（规范化后指纹相同）。
        _, reused = svc.submit_release_request(
            "req-retry-y", [CHANNEL_GALLERY_LABEL], request_refs(), ["researcher-ma"],
            now=BASE + timedelta(days=1, minutes=3))
        self.assertTrue(reused)

        # 内容变化（贡献人不同）必须新建版本。
        req2, reused = svc.submit_release_request(
            "req-2", [CHANNEL_GALLERY_LABEL], request_refs(),
            ["researcher-ma", "researcher-khaled"], revision_of="req-1",
            now=BASE + timedelta(days=2))
        self.assertFalse(reused)
        self.assertEqual(req2.revision_of, "req-1")

    # -- 场景 6：并发批准不得越过双边确认 ------------------------------

    def test_concurrent_approvals_cannot_skip_bilateral_confirmation(self) -> None:
        svc = build_world(self.path)
        svc.submit_release_request("req-1", [CHANNEL_GALLERY_LABEL], request_refs(),
                                   ["researcher-ma"], now=BASE + timedelta(days=1))
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def approve_cn_twice() -> None:
            barrier.wait()
            try:
                svc.record_approval("req-1", "CN", CN, now=BASE + timedelta(days=2))
                outcomes.append("cn-1-ok")
            except ServiceError as exc:
                outcomes.append(exc)

        def approve_cn_other_person() -> None:
            barrier.wait()
            try:
                svc.record_approval("req-1", "CN", OTHER_CN, now=BASE + timedelta(days=2))
                outcomes.append("cn-2-ok")
            except ServiceError as exc:
                outcomes.append(exc)

        t1 = threading.Thread(target=approve_cn_twice)
        t2 = threading.Thread(target=approve_cn_other_person)
        t1.start(); t2.start(); t1.join(); t2.join()

        state = svc._state()
        # 只有一个 CN 会签落库；EG 尚未确认，申请不得被批准。
        self.assertEqual(len(state.requests["req-1"].approvals), 1)
        self.assertEqual(state.requests["req-1"].status, "submitted")
        self.assertIn("CN", state.requests["req-1"].approvals)
        winner_id = state.requests["req-1"].approvals["CN"]["by"]
        loser = OTHER_CN if winner_id == CN["id"] else CN
        # 落败一方重复会签被拒。
        with self.assertRaisesRegex(ServiceError, "不能换人重复会签"):
            svc.record_approval("req-1", "CN", loser, now=BASE + timedelta(days=2, minutes=1))

        # 存储层乐观锁：过期版本号的并发写入被拒。
        current = svc.store.version_of("req-1")
        stale = make_event(
            "evt-stale", EVENT_BILATERAL_APPROVAL_RECORDED, AGG_REQUEST, "stale",
            BASE + timedelta(days=3), current + 1, "stale", party="CN", by="z")
        with self.assertRaises(ConcurrencyError):
            svc.store.append(stale, expected_version=current - 1)

        # EG 确认后才批准；中标方重试会签幂等。
        winner = CN if winner_id == CN["id"] else OTHER_CN
        self.assertEqual(svc.record_approval("req-1", "CN", winner,
                                             now=BASE + timedelta(days=2, minutes=2)), [])
        svc.record_approval("req-1", "EG", EG, now=BASE + timedelta(days=2, minutes=3))
        self.assertEqual(svc._state().requests["req-1"].status, "approved")

    # -- 场景 7：崩溃恢复后继续未完成会签 ------------------------------

    def test_recovery_completes_countersign_after_restart(self) -> None:
        svc = build_world(self.path)
        svc.submit_release_request("req-1", [CHANNEL_GALLERY_LABEL], request_refs(),
                                   ["researcher-ma"], now=BASE + timedelta(days=1))
        svc.record_approval("req-1", "CN", CN, now=BASE + timedelta(days=2))

        # 模拟崩溃：EG 会签事件已落盘，但 REQUEST_APPROVED 未及写入。
        current = svc.store.version_of("req-1")
        svc.store.append(make_event(
            "evt-eg-crash", EVENT_BILATERAL_APPROVAL_RECORDED, AGG_REQUEST, "req-1",
            BASE + timedelta(days=2, minutes=5), current + 1,
            "EG 方数据责任人确认（崩溃前落盘）", party="EG", by=EG["id"]))

        # 新进程从 JSONL 重放：申请仍是 submitted，但双边会签齐备。
        restarted = ReleaseCoordinationService(EventStore(self.path))
        state = restarted._state()
        self.assertEqual(state.requests["req-1"].status, "submitted")
        self.assertEqual(set(state.requests["req-1"].approvals), {"CN", "EG"})

        recovered = restarted.recover_pending_decisions(now=BASE + timedelta(days=2, minutes=6))
        self.assertEqual([e["event_type"] for e in recovered], ["REQUEST_APPROVED"])
        self.assertEqual(restarted._state().requests["req-1"].status, "approved")

        # 恢复幂等：再次恢复不产生事件。
        self.assertEqual(restarted.recover_pending_decisions(now=BASE + timedelta(days=2, minutes=7)), [])
        # 会签也可安全重试。
        self.assertEqual(restarted.record_approval("req-1", "CN", CN,
                                                   now=BASE + timedelta(days=2, minutes=8)), [])

    # -- 场景 8：一句展签追到数据、翻译、许可、署名、当时结论 ----------

    def test_trace_from_label_covers_full_chain(self) -> None:
        svc = build_world(self.path)
        svc.submit_release_request("req-1",
                                   [CHANNEL_GALLERY_LABEL, CHANNEL_ACADEMIC_PAPER],
                                   request_refs(), ["researcher-ma"],
                                   now=BASE + timedelta(days=1))
        approve_both(svc, "req-1", BASE + timedelta(days=2))
        svc.issue_variant("var-label", "req-1", CHANNEL_GALLERY_LABEL, "obj-005-intro",
                          "青铜护身符｜年代为初步判断", EDITOR, now=PUBLIC_AT)

        trace = Tracer(svc.store).trace_label(CHANNEL_GALLERY_LABEL, "obj-005-intro")

        # 展签本身与发布时间。
        self.assertEqual(trace["label"], {"channel": CHANNEL_GALLERY_LABEL,
                                          "label_key": "obj-005-intro"})
        self.assertEqual(trace["variant"]["content"], "青铜护身符｜年代为初步判断")

        # 数据来源：冻结的原始记录版本与测量数据。
        record = trace["sources"]["records"][0]
        self.assertEqual(record["id"], "rec-0021")
        self.assertEqual(record["frozen_version"], 1)
        self.assertTrue(record["frozen_at_publication"])
        self.assertEqual(record["data"]["measurements"], RECORD_DATA_V1["measurements"])

        # 当时有效的研究结论，且不确定性被保留。
        claim = trace["sources"]["claims"][0]
        self.assertEqual(claim["frozen_version"], 1)
        self.assertIn("中王国时期", claim["text_as_published"])
        self.assertEqual(claim["confidence_as_published"], "provisional")

        # 翻译稿。
        translation = trace["sources"]["translations"][0]
        self.assertEqual(translation["language"], "ar")
        self.assertIn("الدولة الوسطى", translation["text"])
        self.assertEqual(translation["claim_id"], "claim-9")

        # 许可链路：借展条款 + 中埃双边会签。
        loan = trace["permissions"]["loans"][0]
        self.assertEqual(loan["loan_terms_id"], "loan-77")
        self.assertIn(CHANNEL_GALLERY_LABEL, loan["permitted_channels"])
        parties = {a["party"]: a["by"] for a in trace["request"]["bilateral_approvals"]}
        self.assertEqual(parties, {"CN": CN["id"], "EG": EG["id"]})

        # 贡献署名包含研究者与双边责任人。
        contributor_ids = {c["id"] for c in trace["contributors"]}
        self.assertIn("researcher-ma", contributor_ids)
        self.assertIn(CN["id"], contributor_ids)
        self.assertIn(EG["id"], contributor_ids)

        # 文物（经主张隐式关联）在溯源中可见。
        self.assertTrue(any(o["id"] == "obj-005" for o in trace["sources"]["objects"]))

    def test_frozen_version_is_isolated_from_later_record_revision(self) -> None:
        svc = build_world(self.path)
        svc.submit_release_request("req-1", [CHANNEL_ACADEMIC_PAPER], request_refs(),
                                   ["researcher-ma"], now=BASE + timedelta(days=1))
        approve_both(svc, "req-1", BASE + timedelta(days=2))
        # 原始记录后来勘误测量值（同一聚合新版本，不覆盖旧版）。
        svc.register_record(
            "rec-0021", "T3", "3 号探方出土原始记录（勘误）",
            {**RECORD_DATA_V1, "measurements": {"height_mm": 43.2, "width_mm": 31.0}},
            now=PUBLIC_AT - timedelta(days=1))
        svc.issue_variant("var-paper", "req-1", CHANNEL_ACADEMIC_PAPER, "paper-tab-1",
                          "论文引用冻结数据", EDITOR, now=PUBLIC_AT)

        trace = Tracer(svc.store).trace_variant("var-paper")
        record = trace["sources"]["records"][0]
        self.assertEqual(record["frozen_version"], 1)
        self.assertEqual(record["latest_version"], 2)
        self.assertEqual(record["data"]["measurements"]["height_mm"], 42.3)

    # -- 编辑权限与批准范围 --------------------------------------------

    def test_editor_confined_to_approved_scope(self) -> None:
        svc = build_world(self.path)
        svc.submit_release_request("req-1", [CHANNEL_ACADEMIC_PAPER], request_refs(),
                                   ["researcher-ma"], now=BASE + timedelta(days=1))
        # 未获批不能发布。
        with self.assertRaisesRegex(ServiceError, "尚未获批"):
            svc.issue_variant("v", "req-1", CHANNEL_ACADEMIC_PAPER, "k", "x",
                              EDITOR, now=BASE + timedelta(days=2))
        # 非策展编辑角色不能发布。
        with self.assertRaisesRegex(ServiceError, "curatorial_editor"):
            svc.issue_variant("v", "req-1", CHANNEL_ACADEMIC_PAPER, "k", "x",
                              CN, now=BASE + timedelta(days=2))
        approve_both(svc, "req-1", BASE + timedelta(days=3))
        # 超出批准载体范围被拒。
        with self.assertRaisesRegex(ServiceError, "不在批准范围"):
            svc.issue_variant("v", "req-1", CHANNEL_GALLERY_LABEL, "k", "x",
                              EDITOR, now=BASE + timedelta(days=4))
        # 借展条款不允许的载体被拒（用收紧条款的第二件文物验证）。
        svc.register_loan_terms("loan-strict", "obj-006", "仅论文",
                                [CHANNEL_ACADEMIC_PAPER], now=BASE)
        svc.register_object("obj-006", "rec-0021", "石印",
                            loan_terms_id="loan-strict", now=BASE)
        svc.submit_release_request(
            "req-2", [CHANNEL_CATALOG],
            [{"type": "find_object", "id": "obj-006"}], ["researcher-ma"],
            now=BASE + timedelta(days=5))
        approve_both(svc, "req-2", BASE + timedelta(days=6))
        with self.assertRaisesRegex(ServiceError, "借展条款不允许"):
            svc.issue_variant("v2", "req-2", CHANNEL_CATALOG, "k", "x",
                              EDITOR, now=BASE + timedelta(days=7))

    # -- 存储重放 ------------------------------------------------------

    def test_store_replays_across_construction(self) -> None:
        svc = build_world(self.path)
        count = len(svc.store.events)
        replayed = EventStore(self.path)
        self.assertEqual(len(replayed.events), count)
        state = reduce_events(replayed.events)
        self.assertIn("obj-005", state.objects)
        with self.assertRaisesRegex(ValueError, "事件标识不得复用"):
            replayed.append(replayed.events[0])


if __name__ == "__main__":
    unittest.main()
