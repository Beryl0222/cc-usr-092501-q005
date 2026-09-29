"""服务恢复：从事件日志重建状态，继续未完成的会签。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import FakeClock, approve_both, bootstrap, make_service, submit_request


class RecoveryTest(unittest.TestCase):
    def test_unfinished_cosign_continues_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            clock = FakeClock()
            service, _ = make_service(path, clock)
            bootstrap(service)
            submit_request(service)
            service.approve("req-1", "cn-steward", 1)
            # 服务恢复：从事件日志重建
            restored, _ = make_service(path, clock)
            view = restored.get_request("req-1")
            self.assertEqual(view["status"], "PENDING")
            self.assertIn("CN", view["approvals"])
            restored.approve("req-1", "eg-steward", 1)  # 继续未完成会签
            self.assertEqual(restored.get_request("req-1")["status"], "APPROVED")
            # 恢复后相同申请重试仍复用原流程
            again = submit_request(restored)
            self.assertEqual(again["request_version"], 1)
            issued = restored.issue_variant("v-cat", "req-1", "catalog-1", "editor", "条目",
                                            {"claims": ["claim-1@1"]})
            self.assertEqual(issued["status"], "ACTIVE")
            # 再次恢复后公开版本与待办仍在
            restored.open_task("CONTRIBUTOR_OBJECTION", "translator",
                               {"variant_id": "v-cat"}, "署名遗漏")
            rerestored, _ = make_service(path, clock)
            self.assertEqual(rerestored.get_variant("v-cat")["status"], "ACTIVE")
            self.assertEqual(len(rerestored.pending_tasks()), 1)

    def test_events_are_valid_after_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            clock = FakeClock()
            service, _ = make_service(path, clock)
            bootstrap(service)
            submit_request(service)
            approve_both(service)
            restored, _ = make_service(path, clock)
            self.assertEqual(len(restored.events()), len(service.events()))


if __name__ == "__main__":
    unittest.main()
