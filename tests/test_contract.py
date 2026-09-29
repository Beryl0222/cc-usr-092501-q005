"""契约一致性：envelope 常量与 JSON Schema 保持同步。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.envelope import AGGREGATE_TYPES, EVENT_TYPES, validate_event

ROOT = Path(__file__).parents[1]


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))

    def test_event_types_match_schema(self):
        self.assertEqual(set(self.schema["properties"]["event_type"]["enum"]), set(EVENT_TYPES))

    def test_aggregate_types_match_schema(self):
        self.assertEqual(set(self.schema["properties"]["aggregate_type"]["enum"]), set(AGGREGATE_TYPES))

    def test_unknown_event_type_rejected(self):
        record = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        record["event_type"] = "SOMETHING_ELSE"
        self.assertIn("未知事件类型：SOMETHING_ELSE", validate_event(record))


if __name__ == "__main__":
    unittest.main()
