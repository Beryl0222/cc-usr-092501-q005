"""释出申请、冻结、幂等重试、双边会签与编辑范围。"""

from __future__ import annotations

import threading
import unittest

from src.errors import DomainError
from tests.helpers import approve_both, bootstrap, make_service, submit_request


class SubmitAndFreezeTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        bootstrap(self.service)

    def test_submit_freezes_referenced_versions(self):
        view = submit_request(self.service)
        self.assertEqual(view["frozen"]["records"], {"rec-1": 1, "rec-2": 1})
        self.assertEqual(view["frozen"]["claims"], {"claim-1": 1})
        self.assertEqual(view["frozen"]["translations"], {"tr-1": 1})
        self.service.amend_record("rec-2", {"height_cm": 33.1}, "researcher", "复测")
        frozen = self.service.get_request("req-1")["frozen"]
        self.assertEqual(frozen["records"]["rec-2"], 1)  # 冻结不随数据修订漂移

    def test_idempotent_retry_reuses_flow(self):
        submit_request(self.service)
        self.service.approve("req-1", "cn-steward", 1)
        again = submit_request(self.service)  # 相同申请重试
        self.assertEqual(again["request_version"], 1)
        self.assertIn("CN", again["approvals"])  # 已完成的确认保留
        requested = [e for e in self.service.events() if e["event_type"] == "RELEASE_REQUESTED"]
        self.assertEqual(len(requested), 1)

    def test_content_change_requires_new_version(self):
        submit_request(self.service)
        self.service.approve("req-1", "cn-steward", 1)
        self.service.amend_record("rec-2", {"height_cm": 33.1}, "researcher", "复测")
        changed = submit_request(self.service, record_refs=["rec-1", "rec-2@2"])
        self.assertEqual(changed["request_version"], 2)
        self.assertEqual(changed["frozen"]["records"]["rec-2"], 2)
        self.assertEqual(changed["approvals"], {})  # 旧版本确认不带入新内容
        self.assertEqual(changed["status"], "PENDING")

    def test_unknown_reference_rejected(self):
        with self.assertRaises(DomainError):
            submit_request(self.service, record_refs=["rec-1", "rec-404"])

    def test_pinned_version_must_exist(self):
        with self.assertRaises(DomainError):
            submit_request(self.service, record_refs=["rec-1@9"])


class BilateralApprovalTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        bootstrap(self.service)
        submit_request(self.service)

    def test_both_sides_required(self):
        self.service.approve("req-1", "cn-steward", 1)
        self.assertEqual(self.service.get_request("req-1")["status"], "PENDING")
        with self.assertRaises(DomainError):
            self.service.issue_variant("v-1", "req-1", "catalog-1", "editor", "图录条目",
                                       {"claims": ["claim-1@1"]})
        self.service.approve("req-1", "eg-steward", 1)
        self.assertEqual(self.service.get_request("req-1")["status"], "APPROVED")

    def test_same_side_cannot_confirm_twice(self):
        self.service.approve("req-1", "cn-steward", 1)
        with self.assertRaises(DomainError):
            self.service.approve("req-1", "cn-steward", 1)

    def test_non_steward_cannot_approve(self):
        with self.assertRaises(DomainError):
            self.service.approve("req-1", "researcher", 1)

    def test_stale_version_approval_rejected(self):
        self.service.amend_record("rec-2", {"height_cm": 33.1}, "researcher", "复测")
        submit_request(self.service, record_refs=["rec-1", "rec-2@2"])
        with self.assertRaises(DomainError):
            self.service.approve("req-1", "cn-steward", 1)  # 旧版本号
        self.service.approve("req-1", "cn-steward", 2)

    def test_concurrent_approvals_still_require_both_sides(self):
        errors = []

        def approve(steward):
            try:
                self.service.approve("req-1", steward, 1)
            except DomainError as error:
                errors.append(str(error))

        threads = [threading.Thread(target=approve, args=(steward,))
                   for steward in ("cn-steward", "eg-steward", "cn-steward", "eg-steward")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        view = self.service.get_request("req-1")
        self.assertEqual(view["status"], "APPROVED")
        self.assertEqual(set(view["approvals"]), {"CN", "EG"})
        self.assertEqual(len(errors), 2)  # 同方重复确认被拒绝
        approved = [e for e in self.service.events() if e["event_type"] == "RELEASE_APPROVED"]
        self.assertEqual(len(approved), 1)  # 并发下不会越过双边确认


class EditorScopeTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        bootstrap(self.service)
        submit_request(self.service)
        approve_both(self.service)

    def test_issue_within_scope(self):
        view = self.service.issue_variant(
            "v-label", "req-1", "label-1", "editor", "彩绘陶俑，第18王朝早期（初步）",
            {"records": ["rec-1@1", "rec-2@1"], "claims": ["claim-1@1"], "translations": ["tr-1@1"]})
        self.assertEqual(view["current"], 1)
        self.assertEqual(view["status"], "ACTIVE")

    def test_out_of_scope_reference_rejected(self):
        self.service.register_record("rec-9", "art-1", "photo", {"file": "x.tif"}, "researcher")
        with self.assertRaises(DomainError):
            self.service.issue_variant("v-2", "req-1", "catalog-1", "editor", "条目",
                                       {"records": ["rec-9@1"]})

    def test_wrong_version_rejected(self):
        with self.assertRaises(DomainError):
            self.service.issue_variant("v-3", "req-1", "catalog-1", "editor", "条目",
                                       {"claims": ["claim-1@9"]})

    def test_carrier_outside_request_rejected(self):
        self.service.register_carrier("lecture-1", "LECTURE", ["art-1"], False)
        with self.assertRaises(DomainError):
            self.service.issue_variant("v-4", "req-1", "lecture-1", "editor", "讲稿",
                                       {"claims": ["claim-1@1"]})

    def test_non_editor_rejected(self):
        with self.assertRaises(DomainError):
            self.service.issue_variant("v-5", "req-1", "catalog-1", "researcher", "条目",
                                       {"claims": ["claim-1@1"]})

    def test_unpinned_reference_rejected(self):
        with self.assertRaises(DomainError):
            self.service.issue_variant("v-6", "req-1", "catalog-1", "editor", "条目",
                                       {"claims": ["claim-1"]})


class LoanTermsTest(unittest.TestCase):
    def test_loan_terms_restrict_record_kinds(self):
        service, _ = make_service()
        bootstrap(service)
        service.record_loan_terms("loan-1", "art-1", "开罗博物馆", ["measurement"],
                                  "借展期间仅允许公布测量数据")
        with self.assertRaises(DomainError):
            submit_request(service)  # 含 photo 记录 rec-1
        view = submit_request(service, record_refs=["rec-2"])
        self.assertEqual(view["frozen"]["records"], {"rec-2": 1})


if __name__ == "__main__":
    unittest.main()
