import ipaddress

import pytest

from app.sources import (
    SourceError,
    SourceItem,
    URLValidationError,
    deduplicate_source_items,
    source_dedupe_key,
    validate_url,
)


def item(**kwargs):
    values = {
        "source_id": "one",
        "external_id": "1",
        "event_date": "2026-08-30",
        "title": "Holiday",
        "url": "https://calend.ru/holidays/1/",
    }
    values.update(kwargs)
    return SourceItem(**values)


@pytest.mark.parametrize("url", [
    "http://calend.ru/day/{date}/",
    "https://user:pass@calend.ru/day/{date}/",
    "https://calend.ru/day/{date}/#fragment",
    "https://calend.ru:8443/day/{date}/",
    "https://127.0.0.1/day/{date}/",
])
def test_validate_url_rejects_unsafe_urls(url):
    with pytest.raises(URLValidationError):
        validate_url(url, allowed_hosts=("calend.ru",))


def test_validate_url_normalizes_host_and_trailing_dot():
    assert validate_url("HTTPS://CALEND.RU./day/{date}/") == "https://calend.ru/day/{date}/"


def test_validate_url_allowlist_does_not_match_similar_suffix():
    with pytest.raises(URLValidationError):
        validate_url("https://notcalend.ru/day/{date}/", allowed_hosts=("calend.ru",))


def test_validate_url_rejects_resolved_private_address(monkeypatch):
    monkeypatch.setattr("app.sources._resolve", lambda *_: {ipaddress.ip_address("10.0.0.1")})
    with pytest.raises(URLValidationError):
        validate_url("https://calend.ru/day/{date}/", allowed_hosts=("calend.ru",), resolve=True)


def test_dedupe_uses_canonical_url_across_sources():
    first = item(source_id="one")
    duplicate = item(source_id="two", external_id="different", url="https://CALEND.RU/holidays/1/#ignored")
    assert source_dedupe_key(first) == source_dedupe_key(duplicate)
    assert deduplicate_source_items([first, duplicate]) == [first]


def test_dedupe_scopes_id_without_url_to_source():
    first = item(source_id="one", url="", external_id="1")
    second = item(source_id="two", url="", external_id="1")
    assert len(deduplicate_source_items([first, second])) == 2


def test_dedupe_rejects_invalid_event():
    with pytest.raises(SourceError):
        deduplicate_source_items([item(title="")])
