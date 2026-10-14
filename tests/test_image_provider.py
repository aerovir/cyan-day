"""Tests for the Pollinations image provider without real network access."""

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from app.image_provider import (
    ALLOWED_HOSTS,
    MAX_PROMPT_LENGTH,
    CloudflareImageProvider,
    ImageConfigError,
    ImageGenerationError,
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


_ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
_MODEL = "@cf/black-forest-labs/flux-1-schnell"


def test_cloudflare_provider_posts_prompt_dimensions_and_returns_image(tmp_path, requests_mock):
    image = bytes([137]) + b"PNG\r\n\x1a\nimage-bytes"
    requests_mock.post(
        f"https://api.cloudflare.com/client/v4/accounts/{_ACCOUNT_ID}/ai/run/{_MODEL}",
        json={"success": True, "result": {"image": base64.b64encode(image).decode()}},
    )
    provider = CloudflareImageProvider(
        account_id=_ACCOUNT_ID,
        api_token="secret-token",
        model=_MODEL,
        timeout=45,
    )

    path = provider.generate("a blue glass")
    try:
        request = requests_mock.calls[0].request
        body = json.loads(request.body)
        assert request.url.endswith("/ai/run/" + _MODEL)
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert body == {"prompt": "a blue glass"}
        assert Path(path).read_bytes() == image
        assert Path(path).suffix == ".png"
    finally:
        Path(path).unlink(missing_ok=True)


def test_cloudflare_provider_accepts_raw_image_response(requests_mock, tmp_path):
    image = bytes([255, 216, 255]) + b"raw-jpeg"
    requests_mock.post(
        f"https://api.cloudflare.com/client/v4/accounts/{_ACCOUNT_ID}/ai/run/{_MODEL}",
        body=image,
        content_type="image/jpeg",
    )
    provider = CloudflareImageProvider(account_id=_ACCOUNT_ID, api_token="token", model=_MODEL)

    path = provider.generate("a blue glass")
    try:
        assert Path(path).read_bytes() == image
        assert Path(path).suffix == ".jpg"
    finally:
        Path(path).unlink(missing_ok=True)


def test_cloudflare_provider_rejects_missing_credentials_and_invalid_model():
    with pytest.raises(ImageConfigError, match="account ID"):
        CloudflareImageProvider(account_id="bad", api_token="token")
    with pytest.raises(ImageConfigError, match="API token"):
        CloudflareImageProvider(account_id=_ACCOUNT_ID, api_token=" ")
    with pytest.raises(ImageConfigError, match="model"):
        CloudflareImageProvider(account_id=_ACCOUNT_ID, api_token="token", model="../bad")


def test_cloudflare_provider_rejects_invalid_response_and_does_not_leak_token(requests_mock):
    requests_mock.post(
        f"https://api.cloudflare.com/client/v4/accounts/{_ACCOUNT_ID}/ai/run/{_MODEL}",
        json={"success": False, "errors": [{"message": "denied"}]},
        status=401,
    )
    provider = CloudflareImageProvider(account_id=_ACCOUNT_ID, api_token="super-secret", model=_MODEL)

    with pytest.raises(ImageGenerationError) as exc:
        provider.generate("prompt")

    assert "super-secret" not in str(exc.value)
    assert "401" in str(exc.value)


def test_cloudflare_provider_rejects_bad_base64_and_oversized_image(requests_mock):
    requests_mock.post(
        f"https://api.cloudflare.com/client/v4/accounts/{_ACCOUNT_ID}/ai/run/{_MODEL}",
        json={"success": True, "result": {"image": "!!!"}},
    )
    provider = CloudflareImageProvider(account_id=_ACCOUNT_ID, api_token="token", model=_MODEL)
    with pytest.raises(ImageGenerationError, match="base64"):
        provider.generate("prompt")

    image = bytes([137]) + b"PNG\r\n\x1a\n1234567890"
    requests_mock.reset()
    requests_mock.post(
        f"https://api.cloudflare.com/client/v4/accounts/{_ACCOUNT_ID}/ai/run/{_MODEL}",
        json={"success": True, "result": {"image": base64.b64encode(image).decode()}},
    )
    provider = CloudflareImageProvider(account_id=_ACCOUNT_ID, api_token="token", model=_MODEL, max_image_bytes=8)
    with pytest.raises(ImageGenerationError, match="too large"):
        provider.generate("prompt")


def test_cloudflare_provider_converts_timeout_to_generation_error(mocker):
    from app import image_provider

    mocker.patch.object(image_provider.requests, "post", side_effect=requests.Timeout())
    provider = CloudflareImageProvider(account_id=_ACCOUNT_ID, api_token="token", model=_MODEL)
    with pytest.raises(ImageGenerationError, match="request failed"):
        provider.generate("prompt")
