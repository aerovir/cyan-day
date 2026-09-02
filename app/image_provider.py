"""Image URL generation for Pollinations AI."""

from __future__ import annotations

import math
from numbers import Real
from typing import Callable, Optional
from urllib.parse import quote, urlencode, urlsplit

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
