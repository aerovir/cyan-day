"""Image URL generation for Pollinations AI.

The provider deliberately does not import :mod:`app.main`.  Callers that need
an image file can pass the application's already-hardened downloader to
:meth:`PollinationsImageProvider.generate`; callers that only need a URL can
omit it.
"""

from __future__ import annotations

import math
from numbers import Real
from typing import Callable, Optional
from urllib.parse import quote, urlencode


POLLINATIONS_HOST = "image.pollinations.ai"
POLLINATIONS_BASE_URL = f"https://{POLLINATIONS_HOST}/prompt/"
ALLOWED_HOSTS = (POLLINATIONS_HOST,)
DEFAULT_TIMEOUT = 30
MIN_TIMEOUT = 1
MAX_TIMEOUT = 120
MAX_PROMPT_LENGTH = 1000

Downloader = Callable[..., Optional[str]]


class PollinationsImageProvider:
    """Build Pollinations image URLs and optionally download the result.

    ``downloader`` is injected rather than imported so that the provider stays
    independent of the application orchestration layer.  In production it can
    be ``app.main.download_image`` (which performs the bounded download and
    SSRF checks); in tests it can be a simple mock.
    """

    allowed_hosts = ALLOWED_HOSTS

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout: Real = DEFAULT_TIMEOUT,
        max_prompt_length: int = MAX_PROMPT_LENGTH,
    ) -> None:
        self.timeout = self._validate_timeout(timeout)
        self.max_prompt_length = self._validate_max_prompt_length(max_prompt_length)
        if model is not None and not isinstance(model, str):
            raise ValueError("model must be a string or None")
        if isinstance(model, str):
            model = model.strip()
            if not model:
                raise ValueError("model must not be empty")
        self.model = model

    @staticmethod
    def _validate_timeout(timeout: Real) -> Real:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, Real)
            or not math.isfinite(float(timeout))
            or timeout < MIN_TIMEOUT
            or timeout > MAX_TIMEOUT
        ):
            raise ValueError(
                f"timeout must be between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds"
            )
        return timeout

    @staticmethod
    def _validate_max_prompt_length(max_prompt_length: int) -> int:
        if (
            isinstance(max_prompt_length, bool)
            or not isinstance(max_prompt_length, int)
            or max_prompt_length <= 0
        ):
            raise ValueError("max_prompt_length must be a positive integer")
        return max_prompt_length

    def _validate_prompt(self, prompt: str) -> str:
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string")
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if len(prompt) > self.max_prompt_length:
            raise ValueError(
                f"prompt must be at most {self.max_prompt_length} characters"
            )
        return prompt

    def build_url(self, prompt: str) -> str:
        """Return a safe HTTPS Pollinations URL for ``prompt``.

        The prompt is a path segment, so every character unsafe in a path
        (including ``/``, ``?`` and ``#``) is encoded.  Optional model data is
        encoded as a query parameter with :func:`urllib.parse.urlencode`.
        """
        prompt = self._validate_prompt(prompt)
        encoded_prompt = quote(prompt, safe="")
        url = f"{POLLINATIONS_BASE_URL}{encoded_prompt}"
        if self.model is not None:
            url = f"{url}?{urlencode({'model': self.model})}"
        return url

    def generate(
        self,
        prompt: str,
        downloader: Downloader | None = None,
    ) -> Optional[str]:
        """Generate an image URL or pass it to an injected downloader.

        With no downloader, the URL itself is returned.  When supplied, the
        callable receives the URL and this provider's validated timeout; its
        optional local file path (or ``None`` on failure) is returned unchanged.
        """
        url = self.build_url(prompt)
        if downloader is None:
            return url
        if not callable(downloader):
            raise TypeError("downloader must be callable")
        return downloader(
            url,
            timeout=self.timeout,
            allowed_hosts=self.allowed_hosts,
        )
