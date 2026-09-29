"""已发布更正：修订以更正关联旧版，曾经展出的内容不被覆盖。"""

from __future__ import annotations

import unittest

from src.errors import DomainError
from tests.helpers import approve_both, bootstrap, make_service, submit_request


class CorrectionTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        bootstrap(self.service)
        submit_request(self.service, carrier_ids=["catalog-1"])
        approve_both(self.service)
        self.service.issue_variant("v-cat", "req-1", "catalog-1", "editor",
                                   "陶俑，第18王朝早期（初步）",
                                   {"claims": ["claim-1@1"], "records": ["rec-2@1"],
                                    "translations": ["tr-1@1"]})

    def test_claim_correction_links_without_overwriting(self):
        self.service.correct_claim("claim-1", "热释光测年支持第18王朝中期",
                                   "researcher", "测年结果返回")
        claim = self.service.get_claim("claim-1")
        self.assertEqual(claim["current"], 2)
        self.assertEqual(claim["versions"][1]["text"], "陶俑风格指向第18王朝早期")  # 旧版保留
        self.assertEqual(claim["versions"][2]["corrects"], 1)

    def test_published_correction_flow(self):
        self.service.correct_claim("claim-1", "热释光测年支持第18王朝中期",
                                   "researcher", "测年结果返回")
        submit_request(self.service, key="req-2", claim_refs=["claim-1@2"],
                       record_refs=["rec-2"], translation_refs=[],
                       summary="图录条目更正", carrier_ids=["catalog-1"])
        approve_both(self.service, "req-2")
        corrected = self.service.correct_variant(
            "v-cat", "req-2", "editor", "陶俑，第18王朝中期（测年确认）",
            {"claims": ["claim-1@2"], "records": ["rec-2@1"]}, "依据测年结果更正")
        self.assertEqual(corrected["current"], 2)
        versions = corrected["versions"]
        self.assertEqual(versions[1]["text"], "陶俑，第18王朝早期（初步）")  # 曾展出内容不被覆盖
        self.assertEqual(versions[2]["corrects"], 1)
        # 旧版追溯仍指向当时有效的结论，并标注已被更正
        trace_v1 = self.service.trace_variant("v-cat", version=1)
        self.assertEqual(trace_v1["claims"][0]["version"], 1)
        self.assertTrue(trace_v1["claims"][0]["superseded"])
        self.assertEqual(trace_v1["claims"][0]["corrected_by"], 2)
        trace_v2 = self.service.trace_variant("v-cat")
        self.assertEqual(trace_v2["claims"][0]["version"], 2)
        self.assertFalse(trace_v2["claims"][0]["superseded"])

    def test_correction_requires_approved_scope(self):
        self.service.correct_claim("claim-1", "第18王朝中期", "researcher", "测年结果返回")
        with self.assertRaises(DomainError):
            # claim-1@2 不在 req-1 的批准范围内
            self.service.correct_variant("v-cat", "req-1", "editor", "更正",
                                         {"claims": ["claim-1@2"]}, "测年结果返回")

    def test_correction_requires_reason(self):
        with self.assertRaises(DomainError):
            self.service.correct_variant("v-cat", "req-1", "editor", "更正",
                                         {"claims": ["claim-1@1"]}, "")


if __name__ == "__main__":
    unittest.main()
