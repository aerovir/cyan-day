def test_daily_options_forwards_photos_and_registry(monkeypatch):
    monkeypatch.setenv("VK_TOKEN", "token")
    monkeypatch.setenv("VK_GROUP_ID", "123")
    monkeypatch.setenv("MISTRAL_API_KEY", "key")
    monkeypatch.setenv("WITH_PHOTOS", "yes")
    monkeypatch.setenv("SOURCE_REGISTRY_DB", "/app/state/sources.sqlite3")
    monkeypatch.setenv("SOURCE_REGISTRY_MODE", "registry")

    from scheduler import _daily_options

    assert _daily_options() == {
        "vk_token": "token",
        "vk_group_id": 123,
        "mistral_api_key": "key",
        "max_holidays": 3,
        "mistral_model": "mistral-small-latest",
        "with_photos": True,
        "registry_path": "/app/state/sources.sqlite3",
        "registry_mode": "registry",
    }
