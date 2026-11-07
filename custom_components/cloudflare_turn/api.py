"""Client for Cloudflare Realtime TURN credentials."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from http import HTTPStatus
import re
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout
from webrtc_models import RTCIceServer

from homeassistant.util import dt as dt_util

from .const import API_BASE_URL, REQUEST_TIMEOUT

# TURN key IDs are 32 hex characters today. The pattern is looser, but keeps the
# ID safe to put in a URL path.
KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,128}")

_TURN_URL = re.compile(
    r"(?P<scheme>turns?):(?P<host>[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])"
    r"(?::(?P<port>\d{1,5}))?(?:\?transport=(?P<transport>udp|tcp))?"
)
_DEFAULT_PORTS = {"turn": 3478, "turns": 5349}
_SKIPPED_PORTS = frozenset({53})
_REDACTED = "**REDACTED**"


class CloudflareTurnError(Exception):
    """Base class for errors from the Cloudflare TURN client."""


class CloudflareTurnConnectionError(CloudflareTurnError):
    """Cloudflare could not be reached."""


class CloudflareTurnResponseError(CloudflareTurnError):
    """Cloudflare sent a response this client does not understand."""


class CloudflareTurnAuthError(CloudflareTurnError):
    """Cloudflare rejected the API token."""


class CloudflareTurnKeyNotFoundError(CloudflareTurnAuthError):
    """Cloudflare does not know the TURN key ID."""


class CloudflareTurnRateLimitError(CloudflareTurnError):
    """Cloudflare asked to slow down."""

    def __init__(self, retry_after: timedelta | None) -> None:
        """Store how long Cloudflare asked to wait, if it said."""
        super().__init__("Cloudflare is rate limiting requests")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class TurnUrl:
    """A TURN server URL and its transport, named like ``udp_3478``."""

    url: str
    transport: str


@dataclass(frozen=True, slots=True)
class TurnServer:
    """TURN URLs that share one username and credential."""

    urls: tuple[TurnUrl, ...]
    username: str
    credential: str


@dataclass(frozen=True, slots=True)
class TurnCredentials:
    """Short-lived credentials for Cloudflare's TURN servers."""

    servers: tuple[TurnServer, ...]
    issued_at: datetime
    expires_at: datetime

    @property
    def transports(self) -> frozenset[str]:
        """Return every transport these credentials can use."""
        return frozenset(
            url.transport for server in self.servers for url in server.urls
        )

    def ice_servers(self, transports: Collection[str]) -> list[RTCIceServer]:
        """Return ICE servers limited to the chosen transports.

        If Cloudflare offers none of the chosen transports any more, all offered
        transports are used rather than none.
        """
        chosen = self.transports & frozenset(transports) or self.transports
        return [
            RTCIceServer(
                urls=urls, username=server.username, credential=server.credential
            )
            for server in self.servers
            if (urls := [url.url for url in server.urls if url.transport in chosen])
        ]


class CloudflareTurnClient:
    """Generate TURN credentials with a Cloudflare TURN key."""

    def __init__(self, session: ClientSession, key_id: str, api_token: str) -> None:
        """Initialize the client."""
        self._session = session
        self._key_id = key_id
        self._api_token = api_token

    async def async_generate_credentials(self, ttl: timedelta) -> TurnCredentials:
        """Ask Cloudflare for new credentials that are valid for ``ttl``."""
        # Taken before the request, so the expiry is never later than Cloudflare's.
        issued_at = dt_util.utcnow()
        url = (
            f"{API_BASE_URL}/{quote(self._key_id, safe='')}"
            "/credentials/generate-ice-servers"
        )
        try:
            async with self._session.post(
                url,
                headers={"Authorization": f"Bearer {self._api_token}"},
                json={"ttl": int(ttl.total_seconds())},
                timeout=ClientTimeout(total=REQUEST_TIMEOUT),
            ) as response:
                status = response.status
                if status == HTTPStatus.NOT_FOUND:
                    raise CloudflareTurnKeyNotFoundError(
                        "Cloudflare does not know this TURN key ID"
                    )
                if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                    raise CloudflareTurnAuthError(
                        f"Cloudflare rejected the API token (HTTP {status})"
                    )
                if status == HTTPStatus.TOO_MANY_REQUESTS:
                    raise CloudflareTurnRateLimitError(
                        _parse_retry_after(response.headers.get("Retry-After"))
                    )
                if status not in (HTTPStatus.OK, HTTPStatus.CREATED):
                    raise CloudflareTurnResponseError(
                        f"Cloudflare answered with HTTP {status}"
                    )
                payload = await response.json(content_type=None)
        except (ClientError, TimeoutError) as err:
            raise CloudflareTurnConnectionError(
                self._redact(str(err) or type(err).__name__)
            ) from err
        except ValueError as err:
            raise CloudflareTurnResponseError(
                "Cloudflare's response is not valid JSON"
            ) from err
        return _parse_credentials(payload, issued_at, issued_at + ttl)

    def _redact(self, text: str) -> str:
        """Remove the key ID and token from an error message."""
        for secret in (self._api_token, self._key_id):
            if secret:
                text = text.replace(secret, _REDACTED)
        return text


def _parse_retry_after(value: str | None) -> timedelta | None:
    """Parse a Retry-After header given in seconds or as an HTTP date."""
    if not value:
        return None
    if value.strip().isdigit():
        return timedelta(seconds=int(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(when - dt_util.utcnow(), timedelta(0))


def _parse_credentials(
    payload: Any, issued_at: datetime, expires_at: datetime
) -> TurnCredentials:
    """Turn Cloudflare's ``iceServers`` answer into credentials.

    Only well-formed TURN URLs are kept: a malformed URL handed to go2rtc would
    make it refuse the whole WebRTC session.
    """
    entries = payload.get("iceServers") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise CloudflareTurnResponseError("Cloudflare's response has no iceServers")
    servers: list[TurnServer] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_urls = entry.get("urls")
        if isinstance(raw_urls, str):
            raw_urls = [raw_urls]
        if not isinstance(raw_urls, list):
            continue
        urls = tuple(
            turn_url
            for raw_url in raw_urls
            if isinstance(raw_url, str) and (turn_url := _parse_turn_url(raw_url))
        )
        if not urls:
            # STUN servers (Home Assistant already has its own) or nothing usable.
            continue
        username, credential = entry.get("username"), entry.get("credential")
        if not (
            isinstance(username, str)
            and username
            and isinstance(credential, str)
            and credential
        ):
            raise CloudflareTurnResponseError(
                "Cloudflare sent a TURN server without credentials"
            )
        servers.append(TurnServer(urls, username, credential))
    if not servers:
        raise CloudflareTurnResponseError("Cloudflare sent no usable TURN server")
    return TurnCredentials(tuple(servers), issued_at, expires_at)


def _parse_turn_url(url: str) -> TurnUrl | None:
    """Return a TURN URL with its transport, or None if it can't be used."""
    if (match := _TURN_URL.fullmatch(url)) is None:
        return None
    scheme = match["scheme"]
    port = int(match["port"]) if match["port"] else _DEFAULT_PORTS[scheme]
    transport = match["transport"] or ("tcp" if scheme == "turns" else "udp")
    if scheme == "turns" and transport != "tcp":
        # TURN over DTLS: browsers don't support it.
        return None
    if not 0 < port < 65536 or port in _SKIPPED_PORTS:
        return None
    protocol = "tls" if scheme == "turns" else transport
    return TurnUrl(url, f"{protocol}_{port}")
