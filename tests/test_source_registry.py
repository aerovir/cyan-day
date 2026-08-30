import pytest

from app.source_registry import SourceRegistry
from app.sources import SourceError

URL = "https://calend.ru/day/{date}/"


def test_registry_lifecycle_and_audit(tmp_path):
    with SourceRegistry(tmp_path / "sources.sqlite3") as registry:
        record = registry.add("Calendru", adapter_type="calendru-day", url=URL)
        assert record.adapter_type == "calendru_day"
        assert not record.enabled
        assert registry.list() == [record]
        with pytest.raises(SourceError):
            registry.remove(record.id)
        enabled = registry.enable(record.id)
        assert enabled.enabled
        disabled = registry.disable("calendru")
        assert not disabled.enabled
        removed = registry.remove(record.id, confirm=True)
        assert removed.removed and not removed.enabled
        with pytest.raises(SourceError):
            registry.enable(record.id)
        actions = [entry["action"] for entry in registry.audit(record.id)]
        assert actions == ["add", "enable", "disable", "remove"]


def test_registry_filters_enabled_and_removed(tmp_path):
    with SourceRegistry(tmp_path / "sources.sqlite3") as registry:
        first = registry.add("First", adapter_type="calendru_day", url=URL)
        second = registry.add("Second", adapter_type="calendru_day", url=URL)
        registry.enable(first.id)
        assert [r.name for r in registry.list(enabled_only=True)] == ["First"]
        registry.remove(first.id, confirm=True)
        assert registry.list(enabled_only=True) == []
        assert [r.name for r in registry.list(include_removed=True)] == ["First", "Second"]


def test_registry_rejects_duplicate_and_invalid_sources(tmp_path):
    with SourceRegistry(tmp_path / "sources.sqlite3") as registry:
        registry.add("Same", adapter_type="calendru_day", url=URL)
        with pytest.raises(Exception):
            registry.add("same", adapter_type="calendru_day", url=URL)
        with pytest.raises(SourceError):
            registry.add("Bad", adapter_type="unknown", url=URL)
