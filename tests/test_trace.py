"""展签追溯：来源、翻译、许可、贡献署名与当时有效的研究结论。"""

from __future__ import annotations

import unittest

from tests.helpers import approve_both, bootstrap, make_service, submit_request


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        bootstrap(self.service)
        submit_request(self.service)
        approve_both(self.service)
        self.service.issue_variant("v-label", "req-1", "label-1", "editor",
                                   "彩绘陶俑，第18王朝早期（初步）",
                                   {"records": ["rec-1@1", "rec-2@1"],
                                    "claims": ["claim-1@1"],
                                    "translations": ["tr-1@1"]})

    def test_label_traces_everything(self):
        trace = self.service.trace_variant("v-label")
        # 载体
        self.assertEqual(trace["carrier"]["kind"], "EXHIBITION_LABEL")
        self.assertTrue(trace["carrier"]["requires_physical_display"])
        self.assertEqual(trace["artifacts"][0]["name"], "彩绘陶俑")
        # 数据来源
        self.assertEqual({r["record_id"] for r in trace["records"]}, {"rec-1", "rec-2"})
        self.assertEqual({r["version"] for r in trace["records"]}, {1})
        self.assertEqual({r["kind"] for r in trace["records"]}, {"photo", "measurement"})
        # 翻译
        self.assertEqual(trace["translations"][0]["language"], "ar")
        self.assertEqual(trace["translations"][0]["translator_id"], "translator")
        # 许可：双边确认
        self.assertEqual({a["side"] for a in trace["approvals"]}, {"CN", "EG"})
        self.assertEqual({a["steward"] for a in trace["approvals"]}, {"李岚", "Omar Rahal"})
        # 贡献署名
        credited = {c["name"] for c in trace["contributors"]}
        self.assertIn("王研", credited)          # 作者、记录人
        self.assertIn("Layla Hassan", credited)  # 译者
        self.assertIn("李岚", credited)          # 中方数据责任人
        self.assertIn("Omar Rahal", credited)    # 埃方数据责任人
        self.assertIn("陈策", credited)          # 策展编辑
        # 当时有效的研究结论
        claim = trace["claims"][0]
        self.assertEqual(claim["version"], 1)
        self.assertEqual(claim["uncertainty"], "初步判断，待热释光测年复核")
        self.assertFalse(claim["superseded"])
        # 申请信息
        self.assertEqual(trace["request"]["request_id"], "req-1")
        self.assertEqual(trace["request"]["status"], "APPROVED")

    def test_trace_reflects_later_correction_without_rewriting_history(self):
        self.service.correct_claim("claim-1", "热释光测年支持第18王朝中期",
                                   "researcher", "测年结果返回")
        trace = self.service.trace_variant("v-label")
        self.assertEqual(trace["claims"][0]["version"], 1)  # 当时有效的版本
        self.assertTrue(trace["claims"][0]["superseded"])
        self.assertEqual(trace["claims"][0]["corrected_by"], 2)


if __name__ == "__main__":
    unittest.main()
