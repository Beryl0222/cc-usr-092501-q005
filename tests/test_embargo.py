"""禁发期与禁发延期：延期是独立待办，批准后生效。"""

from __future__ import annotations

import unittest
from datetime import timedelta

from src.errors import DomainError
from tests.helpers import approve_both, bootstrap, make_service, submit_request


class EmbargoTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        bootstrap(self.service)
        submit_request(self.service, carrier_ids=["catalog-1"])
        approve_both(self.service)

    def test_embargo_blocks_publication_until_expiry(self):
        not_before = self.clock.now + timedelta(days=30)
        self.service.set_embargo("emb-1", "art-1", not_before, "等待热释光测年")
        with self.assertRaises(DomainError):
            self.service.issue_variant("v-cat", "req-1", "catalog-1", "editor", "条目",
                                       {"claims": ["claim-1@1"]})
        self.clock.advance(days=31)
        view = self.service.issue_variant("v-cat", "req-1", "catalog-1", "editor", "条目",
                                          {"claims": ["claim-1@1"]})
        self.assertEqual(view["status"], "ACTIVE")

    def test_extension_is_independent_task_and_applies_on_resolution(self):
        not_before = self.clock.now + timedelta(days=30)
        self.service.set_embargo("emb-1", "art-1", not_before, "初始禁发")
        self.clock.advance(days=31)  # 初始禁发期满
        self.service.issue_variant("v-cat", "req-1", "catalog-1", "editor", "条目",
                                   {"claims": ["claim-1@1"]})
        # 借展方申请延长禁发：独立待办
        task = self.service.open_task(
            "EMBARGO_EXTENSION", "eg-steward",
            {"embargo_id": "emb-1", "requested_not_before": self.clock.now + timedelta(days=60)},
            "借展方要求等待专著出版")
        self.assertEqual(task["status"], "OPEN")
        self.assertIn(task["task_id"], {t["task_id"] for t in self.service.pending_tasks()})
        # 待办未决期间原禁发状态不变（已期满，仍可发行）
        self.service.issue_variant("v-cat-2", "req-1", "catalog-1", "editor", "条目二",
                                   {"claims": ["claim-1@1"]})
        # 批准后禁发延长生效
        self.service.resolve_task(task["task_id"], "cn-steward", "同意延长", approved=True)
        with self.assertRaises(DomainError):
            self.service.issue_variant("v-cat-3", "req-1", "catalog-1", "editor", "条目三",
                                       {"claims": ["claim-1@1"]})
        # 已发布内容不回撤
        self.assertEqual(self.service.get_variant("v-cat-2")["status"], "ACTIVE")

    def test_rejected_extension_keeps_original_embargo(self):
        not_before = self.clock.now + timedelta(days=30)
        self.service.set_embargo("emb-1", "art-1", not_before, "初始禁发")
        self.clock.advance(days=31)
        task = self.service.open_task(
            "EMBARGO_EXTENSION", "eg-steward",
            {"embargo_id": "emb-1", "requested_not_before": self.clock.now + timedelta(days=60)})
        self.service.resolve_task(task["task_id"], "cn-steward", "理由不足", approved=False)
        view = self.service.issue_variant("v-cat", "req-1", "catalog-1", "editor", "条目",
                                          {"claims": ["claim-1@1"]})
        self.assertEqual(view["status"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()
