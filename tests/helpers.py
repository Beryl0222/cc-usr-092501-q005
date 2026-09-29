"""测试公共装置：可控时钟与一套基础领域数据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.service import ReleaseCoordinationService
from src.store import EventStore

CN_TZ = timezone(timedelta(hours=8))
START = datetime(2026, 9, 20, 9, 0, tzinfo=CN_TZ)


class FakeClock:
    def __init__(self, start: datetime = START):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


def make_service(path=None, clock=None):
    clock = clock or FakeClock()
    store = EventStore(path, clock=clock)
    return ReleaseCoordinationService(store, clock=clock), clock


def bootstrap(service):
    """登记两件文物、三类原始记录、一条初步结论、一份翻译与四种载体。"""
    service.register_unit("unit-1", "卢克索北区探方", "卢克索")
    service.register_artifact("art-1", "unit-1", "彩绘陶俑")
    service.register_artifact("art-2", "unit-1", "石碑残片")
    service.register_contributor("cn-steward", "李岚", "CN", ["DATA_STEWARD"])
    service.register_contributor("eg-steward", "Omar Rahal", "EG", ["DATA_STEWARD"])
    service.register_contributor("researcher", "王研", "CN", ["RESEARCHER"])
    service.register_contributor("editor", "陈策", "CN", ["EDITOR"])
    service.register_contributor("translator", "Layla Hassan", "EG", ["TRANSLATOR"])
    service.register_record("rec-1", "art-1", "photo", {"file": "fig-01.tif"}, "researcher")
    service.register_record("rec-2", "art-1", "measurement", {"height_cm": 32.5}, "researcher")
    service.register_record("rec-3", "art-2", "photo", {"file": "fig-02.tif"}, "researcher")
    service.propose_claim("claim-1", "art-1", "陶俑风格指向第18王朝早期", "researcher",
                          record_ids=["rec-1", "rec-2"], uncertainty="初步判断，待热释光测年复核")
    service.submit_translation("tr-1", "claim-1", "ar", "الترجمة الأولية للاستنتاج", "translator")
    service.register_carrier("label-1", "EXHIBITION_LABEL", ["art-1"], True)
    service.register_carrier("label-2", "EXHIBITION_LABEL", ["art-2"], True)
    service.register_carrier("catalog-1", "CATALOG", ["art-1", "art-2"], False)
    service.register_carrier("paper-1", "PAPER", ["art-1"], False)


def submit_request(service, key="req-1", **overrides):
    params = {
        "submitted_by": "researcher",
        "summary": "陶俑展签、图录与论文释出",
        "record_refs": ["rec-1", "rec-2"],
        "claim_refs": ["claim-1"],
        "translation_refs": ["tr-1"],
        "carrier_ids": ["label-1", "catalog-1", "paper-1"],
    }
    params.update(overrides)
    return service.submit_release_request(key, **params)


def approve_both(service, key="req-1"):
    view = service.get_request(key)
    service.approve(key, "cn-steward", view["request_version"])
    service.approve(key, "eg-steward", view["request_version"])
    return service.get_request(key)
