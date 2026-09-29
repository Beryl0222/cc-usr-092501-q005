"""命令入口：校验事件文件，或从事件存储对一条公众载体条目做溯源查询。

用法：
  python3 -m src.cli <事件文件>                  # 校验单条事件（兼容原用法）
  python3 -m src.cli trace <事件存储.jsonl> <载体> <展签键>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .envelope import validate_event
from .store import EventStore
from .trace import TraceNotFound, Tracer


def _validate(path: str) -> int:
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"无法读取事件：{error}", file=sys.stderr)
        return 2
    errors = validate_event(record)
    if errors:
        print("；".join(errors), file=sys.stderr)
        return 1
    print(f"事件有效：{record['event_id']}")
    return 0


def _trace(store_path: str, channel: str, label_key: str) -> int:
    try:
        result = Tracer(EventStore(store_path)).trace_label(channel, label_key)
    except TraceNotFound as error:
        print(f"未找到：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    args = sys.argv[1:]
    if len(args) == 1:
        return _validate(args[0])
    if len(args) == 4 and args[0] == "trace":
        return _trace(args[1], args[2], args[3])
    print("用法：python3 -m src.cli <事件文件>", file=sys.stderr)
    print("      python3 -m src.cli trace <事件存储> <载体> <展签键>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
