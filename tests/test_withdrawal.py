"""局部撤展：只暂停依赖实体展示条件的载体。"""

from __future__ import annotations

import unittest

from src.errors import DomainError
from tests.helpers import approve_both, bootstrap, make_service, submit_request


class WithdrawalTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        bootstrap(self.service)
        submit_request(self.service)  # req-1：label-1、catalog-1、paper-1
        submit_request(self.service, key="req-2", record_refs=["rec-3"],
                       claim_refs=[], translation_refs=[],
                       carrier_ids=["label-2"], summary="石碑展签释出")
        approve_both(self.service, "req-1")
        approve_both(self.service, "req-2")
        self.service.issue_variant("v-label-1", "req-1", "label-1", "editor", "陶俑展签",
                                   {"records": ["rec-1@1"], "claims": ["claim-1@1"],
                                    "translations": ["tr-1@1"]})
        self.service.issue_variant("v-label-2", "req-2", "label-2", "editor", "石碑展签",
                                   {"records": ["rec-3@1"]})
        self.service.issue_variant("v-catalog", "req-1", "catalog-1", "editor", "图录条目",
                                   {"claims": ["claim-1@1"]})
        self.service.issue_variant("v-paper", "req-1", "paper-1", "editor", "论文草稿",
                                   {"claims": ["claim-1@1"], "records": ["rec-2@1"]})

    def test_partial_withdrawal_suspends_only_display_dependent_carriers(self):
        suspended = self.service.withdraw_artifact("art-1", "陶俑彩绘层临时保护")
        self.assertEqual(suspended, ["v-label-1"])
        self.assertEqual(self.service.get_variant("v-label-1")["status"], "SUSPENDED")
        self.assertEqual(self.service.get_variant("v-label-2")["status"], "ACTIVE")  # 其他文物不受影响
        self.assertEqual(self.service.get_variant("v-catalog")["status"], "ACTIVE")  # 图录不依赖实体展示
        self.assertEqual(self.service.get_variant("v-paper")["status"], "ACTIVE")

    def test_approved_facts_remain_usable_during_withdrawal(self):
        self.service.withdraw_artifact("art-1", "保护性撤展")
        # 已获准的学术事实不被封锁：非实体载体仍可引用同一批准范围
        view = self.service.issue_variant("v-paper-2", "req-1", "paper-1", "editor", "论文增补",
                                          {"claims": ["claim-1@1"]})
        self.assertEqual(view["status"], "ACTIVE")
        # 但依赖实体展示的新载体不能生成
        with self.assertRaises(DomainError):
            self.service.issue_variant("v-label-new", "req-1", "label-1", "editor", "新展签",
                                       {"claims": ["claim-1@1"]})

    def test_reinstate_resumes_suspended_variants(self):
        self.service.withdraw_artifact("art-1", "保护性撤展")
        resumed = self.service.reinstate_artifact("art-1")
        self.assertEqual(resumed, ["v-label-1"])
        self.assertEqual(self.service.get_variant("v-label-1")["status"], "ACTIVE")

    def test_double_withdraw_rejected(self):
        self.service.withdraw_artifact("art-1", "保护性撤展")
        with self.assertRaises(DomainError):
            self.service.withdraw_artifact("art-1", "再次撤展")


if __name__ == "__main__":
    unittest.main()
