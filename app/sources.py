"""Normalized holiday source adapters with SSRF-safe URL validation."""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit

import requests

from .holidays_parser import HolidaysParser

DEFAULT_CALENDRU_DAY_URL = "https://calend.ru/day/{date}/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)


class SourceError(ValueError):
    """An invalid source definition or source operation."""


class URLValidationError(SourceError):
    """A source URL is unsafe to request."""


@dataclass(frozen=True)
class SourceItem:
    """Canonical event returned by a source adapter.

    ``source_id``, ``external_id`` and ``event_date`` form the stable
    idempotency/deduplication key.  The ``source`` field is retained as a
    convenient backwards-compatible label for consumers that only need it.
    """

    source_id: str
    external_id: str
    event_date: str
    title: str
    description: str = ""
    url: str = ""
    image_url: str = ""
    category: str = ""
    source: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source:
            object.__setattr__(self, "source", self.source_id)

    @property
    def stable_id(self) -> str:
        return self.external_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def source_dedupe_key(item: SourceItem) -> tuple[str, ...]:
    """Return a deterministic key for duplicate events in one collection.

    A canonical event URL is preferred because two adapters can expose the
    same underlying holiday.  External IDs are source-local unless the
    adapter also provides a URL, so they remain scoped by ``source_id``.
    """
    event_date = _validate_day(item.event_date)
    if item.url:
        parsed = urlsplit(item.url.strip())
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        canonical_url = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            path=parsed.path.rstrip("/") or "/",
            query=query,
            fragment="",
        ).geturl()
        return (event_date, "url", canonical_url)
    external_id = item.external_id.strip().lower()
    if external_id:
        return (event_date, "id", item.source_id.strip().lower(), external_id)
    return (event_date, "title", item.source_id.strip().lower(), item.title.strip().casefold())


def deduplicate_source_items(items: Sequence[SourceItem]) -> list[SourceItem]:
    """Keep the first valid item for each :func:`source_dedupe_key`."""
    unique: list[SourceItem] = []
    seen: set[tuple[str, ...]] = set()
    for item in items:
        if not item.title.strip():
            raise SourceError("source event title is required")
        key = source_dedupe_key(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


# Names used by early consumers/spec drafts.
SourceEvent = SourceItem
_source_dedupe_key = source_dedupe_key
NormalizedEvent = SourceItem


class SourceAdapter(Protocol):
    kind: str
    url: str

    def fetch(self, day: str) -> list[SourceItem]: ...

    def test(self, day: str | None = None) -> int: ...


def _hostname_is_allowed(hostname: str, allowed_hosts: Sequence[str] | None) -> bool:
    if not allowed_hosts:
        return True
    host = hostname.rstrip(".").lower()
    return any(
        host == allowed.rstrip(".").lower()
        or host.endswith("." + allowed.rstrip(".").lower())
        for allowed in allowed_hosts
    )


def _special(address: ipaddress._BaseAddress) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def _reject_ip_literal(hostname: str) -> None:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if _special(address):
        raise URLValidationError("source URL must not target a private or special IP")


def _resolve(hostname: str, port: int) -> set[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise URLValidationError(f"cannot resolve source host {hostname!r}") from exc
    addresses = set()
    for info in infos:
        try:
            addresses.add(ipaddress.ip_address(info[4][0]))
        except (IndexError, ValueError):
            continue
    if not addresses:
        raise URLValidationError(f"cannot resolve source host {hostname!r}")
    return addresses


def validate_url(
    url: str,
    *,
    allowed_hosts: Sequence[str] | None = None,
    resolve: bool = False,
) -> str:
    """Validate and normalize a source URL.

    Definitions are validated without DNS resolution; adapters repeat the
    validation with resolution immediately before making a request.  This
    avoids credentials/userinfo, redirects encoded as URLs, non-HTTPS traffic,
    non-default ports, and private/link-local/metadata destinations.
    """
    if not isinstance(url, str) or not url.strip():
        raise URLValidationError("source URL is required")
    raw = url.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise URLValidationError("source URL contains control characters")
    parsed: SplitResult = urlsplit(raw)
    if parsed.scheme.lower() != "https":
        raise URLValidationError("source URL must use https")
    if not parsed.hostname:
        raise URLValidationError("source URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise URLValidationError("source URL must not contain credentials")
    if parsed.fragment:
        raise URLValidationError("source URL must not contain a fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise URLValidationError("source URL has an invalid port") from exc
    if port not in (None, 443):
        raise URLValidationError("source URL must use the default HTTPS port")
    hostname = parsed.hostname.rstrip(".").lower()
    if (
        not re.fullmatch(r"[a-z0-9.-]+", hostname, re.IGNORECASE)
        or hostname.startswith(".")
        or ".." in hostname
    ):
        raise URLValidationError("source URL hostname is malformed")
    _reject_ip_literal(hostname)
    if not _hostname_is_allowed(hostname, allowed_hosts):
        raise URLValidationError(f"source host {hostname!r} is not allow-listed")
    if resolve and any(_special(address) for address in _resolve(hostname, port or 443)):
        raise URLValidationError("source URL resolves to a private or special IP")
    return parsed._replace(
        scheme="https", netloc=hostname + (f":{port}" if port else "")
    ).geturl()


def _validate_day(day: str) -> str:
    try:
        return date.fromisoformat(day).isoformat()
    except (TypeError, ValueError) as exc:
        raise SourceError("date must use YYYY-MM-DD format") from exc


def _external_id(url: str, title: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    match = re.search(r"/(\d+)$", path)
    return match.group(1) if match else (path or title.strip().lower())


class CalendruDaySource:
    """Adapter for the existing :class:`HolidaysParser` implementation."""

    kind = "calendru_day"

    def __init__(
        self,
        url: str = DEFAULT_CALENDRU_DAY_URL,
        *,
        source_id: str | None = None,
        parser: HolidaysParser | None = None,
        timeout: int = 30,
        allowed_hosts: Sequence[str] | None = ("calend.ru",),
    ) -> None:
        self.url = validate_url(url, allowed_hosts=allowed_hosts)
        parsed_url = urlsplit(self.url)
        if parsed_url.query or parsed_url.path != "/day/{date}/":
            raise SourceError("calendru_day URL must be https://calend.ru/day/{date}/")
        if self.url.count("{date}") != 1:
            raise SourceError("calendru_day URL must contain one {date} placeholder")
        if "{" in parsed_url.path.replace("{date}", "") or "}" in parsed_url.path.replace("{date}", ""):
            raise SourceError("calendru_day URL contains an unsupported placeholder")
        if timeout <= 0 or timeout > 120:
            raise SourceError("timeout must be between 1 and 120 seconds")
        self.source_id = source_id or self.kind
        self.parser = parser or HolidaysParser()
        self.timeout = timeout
        self.allowed_hosts = tuple(allowed_hosts or ())

    def fetch(self, day: str) -> list[SourceItem]:
        day = _validate_day(day)
        request_url = self.url.format(date=day)
        validate_url(request_url, allowed_hosts=self.allowed_hosts or None, resolve=True)
        # Fetch here instead of delegating to ``fetch_day_page`` so the
        # adapter can enforce no redirects and avoid proxy/environment
        # settings changing the destination after validation.
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                request_url,
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
                allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise SourceError("source URL redirects are not allowed")
            response.raise_for_status()
            html = response.text
        finally:
            session.close()
        holidays = self.parser.parse_day_page(html)
        return [
            SourceItem(
                source_id=self.source_id,
                external_id=_external_id(item.url, item.title),
                event_date=day,
                title=item.title,
                description=item.description,
                url=item.url,
                image_url=item.image_url,
                category=item.category,
            )
            for item in holidays
        ]

    def test(self, day: str | None = None) -> int:
        return len(self.fetch(day or date.today().isoformat()))

    fetch_day = fetch
    fetch_holidays = fetch


CalendruDayAdapter = CalendruDaySource
_ADAPTERS: dict[str, type[CalendruDaySource]] = {
    CalendruDaySource.kind: CalendruDaySource,
    "calendru-day": CalendruDaySource,
}


def adapter_types() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


def make_adapter(
    kind: str,
    url: str,
    config: Mapping[str, Any] | None = None,
    *,
    source_id: str | None = None,
) -> SourceAdapter:
    try:
        adapter_type = _ADAPTERS[kind]
    except KeyError as exc:
        raise SourceError(f"unsupported source kind: {kind}") from exc
    options = dict(config or {})
    allowed = options.pop("allowed_hosts", ("calend.ru",))
    supported = {"timeout"}
    unknown = set(options) - supported
    if unknown:
        raise SourceError(f"unsupported adapter parameters: {', '.join(sorted(unknown))}")
    return adapter_type(
        url=url,
        source_id=source_id,
        allowed_hosts=allowed,
        **{key: options[key] for key in supported if key in options},
    )
