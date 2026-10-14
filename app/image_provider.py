"""Image URL generation for Pollinations AI."""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import tempfile
from numbers import Real
from typing import Callable, Optional
from urllib.parse import quote, urlencode, urlsplit

import requests

from .sources import validate_url

POLLINATIONS_HOST = "image.pollinations.ai"
POLLINATIONS_BASE_URL = f"https://{POLLINATIONS_HOST}/prompt/"
ALLOWED_HOSTS = (POLLINATIONS_HOST,)
DEFAULT_TIMEOUT = 30
MIN_TIMEOUT = 1
MAX_TIMEOUT = 120
MAX_PROMPT_LENGTH = 1000
Downloader = Callable[..., Optional[str]]


class ImageProviderError(ValueError):
    """Base error for image provider configuration or generation."""


class ImageConfigError(ImageProviderError):
    """Invalid image provider configuration."""


class ImageGenerationError(ImageProviderError):
    """Image generation failed; callers should use text fallback."""


class PollinationsImageProvider:
    """Build Pollinations URLs and optionally download generated images."""

    allowed_hosts = ALLOWED_HOSTS

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout: Real = DEFAULT_TIMEOUT,
        max_prompt_length: int = MAX_PROMPT_LENGTH,
        base_url: str = POLLINATIONS_BASE_URL,
    ) -> None:
        self.timeout = self._validate_timeout(timeout)
        self.max_prompt_length = self._validate_max_prompt_length(max_prompt_length)
        self.base_url = self._validate_base_url(base_url)
        if model is not None and not isinstance(model, str):
            raise ValueError("model must be a string or None")
        if isinstance(model, str):
            model = model.strip()
            if not model:
                raise ValueError("model must not be empty")
        self.model = model

    @staticmethod
    def _validate_timeout(timeout: Real) -> Real:
        if (isinstance(timeout, bool) or not isinstance(timeout, Real)
                or not math.isfinite(float(timeout)) or timeout < MIN_TIMEOUT or timeout > MAX_TIMEOUT):
            raise ValueError(f"timeout must be between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds")
        return timeout

    @staticmethod
    def _validate_max_prompt_length(max_prompt_length: int) -> int:
        if (isinstance(max_prompt_length, bool) or not isinstance(max_prompt_length, int)
                or max_prompt_length <= 0):
            raise ValueError("max_prompt_length must be a positive integer")
        return max_prompt_length

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must not be empty")
        normalized = base_url.strip().rstrip("/") + "/"
        validate_url(normalized, allowed_hosts=ALLOWED_HOSTS, resolve=False)
        if urlsplit(normalized).path.rstrip("/") != "/prompt":
            raise ValueError("base_url must point to the Pollinations /prompt endpoint")
        return normalized

    def _validate_prompt(self, prompt: str) -> str:
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string")
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if len(prompt) > self.max_prompt_length:
            raise ValueError(f"prompt must be at most {self.max_prompt_length} characters")
        return prompt

    def build_url(self, prompt: str) -> str:
        """Return a safe HTTPS URL with prompt encoded as one path segment."""
        prompt = self._validate_prompt(prompt)
        url = f"{self.base_url}{quote(prompt, safe='')}"
        if self.model is not None:
            url = f"{url}?{urlencode({'model': self.model})}"
        return url

    def generate(self, prompt: str, downloader: Downloader | None = None) -> Optional[str]:
        """Return URL or downloaded temporary path using an injected downloader."""
        url = self.build_url(prompt)
        if downloader is None:
            return url
        if not callable(downloader):
            raise TypeError("downloader must be callable")
        return downloader(url, timeout=self.timeout, allowed_hosts=self.allowed_hosts)


CLOUDFLARE_API_BASE_URL = "https://api.cloudflare.com/client/v4/accounts"
CLOUDFLARE_DEFAULT_MODEL = "@cf/black-forest-labs/flux-1-schnell"
MAX_CLOUDFLARE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_CLOUDFLARE_RESPONSE_BYTES = 16 * 1024 * 1024


class CloudflareImageProvider:
    """Generate an image through Cloudflare Workers AI and return a temp path."""

    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        model: str = CLOUDFLARE_DEFAULT_MODEL,
        timeout: Real = 60,
        max_image_bytes: int = MAX_CLOUDFLARE_IMAGE_BYTES,
    ) -> None:
        if not isinstance(account_id, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", account_id.strip()):
            raise ImageConfigError("Cloudflare account ID must be a 32-character hexadecimal ID")
        if not isinstance(api_token, str) or not api_token.strip():
            raise ImageConfigError("Cloudflare API token must not be empty")
        if not isinstance(model, str) or not re.fullmatch(r"@cf/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+", model.strip()):
            raise ImageConfigError("Cloudflare model must be a Workers AI model ID")
        if (isinstance(timeout, bool) or not isinstance(timeout, Real)
                or not math.isfinite(float(timeout)) or timeout < MIN_TIMEOUT or timeout > MAX_TIMEOUT):
            raise ImageConfigError(f"timeout must be between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds")
        if isinstance(max_image_bytes, bool) or not isinstance(max_image_bytes, int) or max_image_bytes < 1:
            raise ImageConfigError("max_image_bytes must be a positive integer")
        self.account_id = account_id.strip()
        self.api_token = api_token.strip()
        self.model = model.strip()
        self.timeout = float(timeout)
        self.max_image_bytes = max_image_bytes

    @property
    def endpoint(self) -> str:
        model_path = quote(self.model, safe="@/-._")
        return f"{CLOUDFLARE_API_BASE_URL}/{self.account_id}/ai/run/{model_path}"

    @staticmethod
    def _image_suffix(image: bytes) -> str:
        if image.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if image.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if len(image) >= 12 and image[:4] == b"RIFF" and image[8:12] == b"WEBP":
            return ".webp"
        raise ImageGenerationError("Cloudflare returned an unsupported image format")

    @staticmethod
    def _read_response_limited(response: requests.Response) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_CLOUDFLARE_RESPONSE_BYTES:
                    raise ImageGenerationError("Cloudflare response is too large")
            except ValueError:
                pass
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_CLOUDFLARE_RESPONSE_BYTES:
                raise ImageGenerationError("Cloudflare response is too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _extract_image(self, response: requests.Response) -> bytes:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        body = self._read_response_limited(response)
        if content_type.startswith("image/"):
            image = body
        else:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ImageGenerationError("Cloudflare returned invalid JSON") from exc
            if not isinstance(payload, dict) or payload.get("success") is False:
                raise ImageGenerationError("Cloudflare image request was not successful")
            result = payload.get("result")
            encoded = result.get("image") if isinstance(result, dict) else None
            if not isinstance(encoded, str) or not encoded:
                raise ImageGenerationError("Cloudflare response has no image data")
            if encoded.startswith("data:"):
                try:
                    encoded = encoded.split(",", 1)[1]
                except IndexError as exc:
                    raise ImageGenerationError("Cloudflare returned invalid image data URI") from exc
            if len(encoded) > ((self.max_image_bytes + 2) // 3) * 4 + 16:
                raise ImageGenerationError("Cloudflare image is too large")
            try:
                image = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ImageGenerationError("Cloudflare returned invalid base64 image data") from exc
        if not image:
            raise ImageGenerationError("Cloudflare returned an empty image")
        if len(image) > self.max_image_bytes:
            raise ImageGenerationError("Cloudflare image is too large")
        self._image_suffix(image)
        return image

    def generate(self, prompt: str) -> str:
        """Generate a prompt image and return its securely created temporary path."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageConfigError("prompt must not be empty")
        if len(prompt) > MAX_PROMPT_LENGTH:
            raise ImageConfigError(f"prompt must be at most {MAX_PROMPT_LENGTH} characters")
        try:
            response = requests.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"},
                json={"prompt": prompt},
                timeout=self.timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            raise ImageGenerationError(f"Cloudflare request failed ({type(exc).__name__})") from exc
        try:
            if response.status_code < 200 or response.status_code >= 300:
                detail = ""
                try:
                    payload = response.json()
                    errors = payload.get("errors", []) if isinstance(payload, dict) else []
                    if errors and isinstance(errors[0], dict):
                        detail = str(errors[0].get("message", ""))[:300]
                except ValueError:
                    pass
                detail = detail.replace(self.api_token, "[REDACTED]")
                suffix = f": {detail}" if detail else ""
                raise ImageGenerationError(f"Cloudflare Workers AI returned HTTP {response.status_code}{suffix}")
            image = self._extract_image(response)
        finally:
            response.close()
        suffix = self._image_suffix(image)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="cloudflare-image-", suffix=suffix, delete=False) as temp:
                temporary_path = temp.name
                temp.write(image)
            return temporary_path
        except OSError as exc:
            if temporary_path:
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass
            raise ImageGenerationError("Could not save Cloudflare image to a temporary file") from exc
