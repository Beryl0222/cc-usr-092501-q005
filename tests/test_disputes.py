"""贡献人异议与翻译争议：彼此独立的待办与解决。"""

from __future__ import annotations

import unittest
from datetime import timedelta

from src.errors import DomainError
from tests.helpers import approve_both, bootstrap, make_service, submit_request


class DisputeTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        bootstrap(self.service)
        submit_request(self.service, carrier_ids=["catalog-1"])
        approve_both(self.service)
        self.service.issue_variant("v-cat", "req-1", "catalog-1", "editor", "图录条目",
                                   {"claims": ["claim-1@1"], "translations": ["tr-1@1"]})

    def test_translation_dispute_resolved_by_revised_translation(self):
        task = self.service.open_task("TRANSLATION_DISPUTE", "eg-steward",
                                      {"translation_id": "tr-1"}, "阿语译稿断代表述存疑")
        self.assertEqual(task["status"], "OPEN")
        self.service.revise_translation("tr-1", "ترجمة منقحة", "translator", "按争议意见修订")
        resolved = self.service.resolve_task(task["task_id"], "cn-steward", "采纳修订译稿",
                                             resolution={"translation_version": 2})
        self.assertEqual(resolved["status"], "RESOLVED")
        self.assertEqual(resolved["resolution"]["translation_version"], 2)

    def test_tasks_are_independent(self):
        self.service.set_embargo("emb-1", "art-1",
                                 self.clock.now + timedelta(days=30), "初始禁发")
        objection = self.service.open_task("CONTRIBUTOR_OBJECTION", "translator",
                                           {"variant_id": "v-cat"}, "署名遗漏译者")
        dispute = self.service.open_task("TRANSLATION_DISPUTE", "eg-steward",
                                         {"translation_id": "tr-1"}, "译稿表述存疑")
        extension = self.service.open_task(
            "EMBARGO_EXTENSION", "eg-steward",
            {"embargo_id": "emb-1", "requested_not_before": self.clock.now + timedelta(days=90)},
            "借展方要求延期")
        self.assertEqual(len(self.service.pending_tasks()), 3)
        self.service.resolve_task(dispute["task_id"], "cn-steward", "已修订译稿")
        remaining = {t["task_id"] for t in self.service.pending_tasks()}
        self.assertEqual(remaining, {objection["task_id"], extension["task_id"]})
        # 翻译争议的解决不影响贡献人异议与禁发延期
        self.assertEqual(self.service.get_task(objection["task_id"])["status"], "OPEN")
        self.assertEqual(self.service.get_task(extension["task_id"])["status"], "OPEN")

    def test_resolved_task_cannot_be_resolved_again(self):
        task = self.service.open_task("CONTRIBUTOR_OBJECTION", "translator",
                                      {"variant_id": "v-cat"}, "署名问题")
        self.service.resolve_task(task["task_id"], "cn-steward", "已更正署名")
        with self.assertRaises(DomainError):
            self.service.resolve_task(task["task_id"], "cn-steward", "重复处理")

    def test_open_task_validates_target(self):
        with self.assertRaises(DomainError):
            self.service.open_task("TRANSLATION_DISPUTE", "eg-steward",
                                   {"translation_id": "tr-404"})
        with self.assertRaises(DomainError):
            self.service.open_task("CONTRIBUTOR_OBJECTION", "translator", {}, "没有对象")
        with self.assertRaises(DomainError):
            self.service.open_task("EMBARGO_EXTENSION", "eg-steward",
                                   {"embargo_id": "emb-404",
                                    "requested_not_before": self.clock.now})


if __name__ == "__main__":
    unittest.main()
