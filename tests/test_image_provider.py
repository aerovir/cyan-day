"""Tests for the Pollinations image provider without real network access."""

from urllib.parse import parse_qs, urlsplit

import pytest

from app.image_provider import (
    ALLOWED_HOSTS,
    MAX_PROMPT_LENGTH,
    PollinationsImageProvider,
)


def test_build_url_encodes_prompt_as_one_path_segment():
    provider = PollinationsImageProvider()

    url = provider.build_url("red wine / sunset? #1")
    parsed = urlsplit(url)

    assert parsed.scheme == "https"
    assert parsed.netloc == "image.pollinations.ai"
    assert parsed.path == "/prompt/red%20wine%20%2F%20sunset%3F%20%231"
    assert parsed.query == ""


def test_build_url_adds_encoded_optional_model():
    provider = PollinationsImageProvider(model="flux & fast")

    parsed = urlsplit(provider.build_url("a glass"))

    assert parsed.path == "/prompt/a%20glass"
    assert parse_qs(parsed.query) == {"model": ["flux & fast"]}


def test_build_url_rejects_empty_and_overlong_prompts():
    provider = PollinationsImageProvider()

    with pytest.raises(ValueError, match="must not be empty"):
        provider.build_url("  ")
    with pytest.raises(ValueError, match="at most"):
        provider.build_url("x" * (MAX_PROMPT_LENGTH + 1))


@pytest.mark.parametrize("timeout", [0, -1, 121, float("inf"), True, "30"])
def test_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="timeout must be between"):
        PollinationsImageProvider(timeout=timeout)


def test_generate_returns_url_without_network_request():
    provider = PollinationsImageProvider()

    assert provider.generate("a toast") == provider.build_url("a toast")


def test_generate_delegates_download_to_injected_callable():
    provider = PollinationsImageProvider(model="flux", timeout=12)
    calls = []

    def downloader(url, **kwargs):
        calls.append((url, kwargs))
        return "/tmp/generated-image.png"

    result = provider.generate("a toast", downloader)

    assert result == "/tmp/generated-image.png"
    assert calls == [
        (
            provider.build_url("a toast"),
            {"timeout": 12, "allowed_hosts": ALLOWED_HOSTS},
        )
    ]


def test_generate_rejects_non_callable_downloader():
    with pytest.raises(TypeError, match="downloader must be callable"):
        PollinationsImageProvider().generate("a toast", downloader="not callable")
