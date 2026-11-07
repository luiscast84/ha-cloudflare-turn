"""Tests for the Cloudflare TURN API client."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import Any

from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.cloudflare_turn.api import (
    CloudflareTurnAuthError,
    CloudflareTurnClient,
    CloudflareTurnConnectionError,
    CloudflareTurnError,
    CloudflareTurnKeyNotFoundError,
    CloudflareTurnRateLimitError,
    CloudflareTurnResponseError,
    _parse_credentials,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .conftest import API_TOKEN, KEY_ID, cloudflare_response

URL = f"https://rtc.live.cloudflare.com/v1/turn/keys/{KEY_ID}/credentials/generate-ice-servers"
TTL = timedelta(hours=48)


@pytest.fixture
def client(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> CloudflareTurnClient:
    """Return a client whose requests go to the mocker."""
    return CloudflareTurnClient(async_get_clientsession(hass), KEY_ID, API_TOKEN)


@pytest.mark.parametrize("status", [HTTPStatus.CREATED, HTTPStatus.OK])
async def test_generate_credentials(
    client: CloudflareTurnClient,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    status: HTTPStatus,
) -> None:
    """Credentials are parsed, STUN and port 53 dropped, the TTL and token sent."""
    freezer.move_to("2026-11-07 12:00:00+00:00")
    aioclient_mock.post(URL, status=status, json=cloudflare_response("user", "pass"))

    credentials = await client.async_generate_credentials(TTL)

    assert credentials.issued_at == dt_util.utcnow()
    assert credentials.expires_at == dt_util.utcnow() + TTL
    (server,) = credentials.servers
    assert (server.username, server.credential) == ("user", "pass")
    assert [url.transport for url in server.urls] == [
        "udp_3478",
        "udp_443",
        "tcp_3478",
        "tcp_80",
        "tls_5349",
        "tls_443",
    ]
    assert credentials.transports == {
        "udp_3478",
        "udp_443",
        "tcp_3478",
        "tcp_80",
        "tls_5349",
        "tls_443",
    }
    ((method, url, data, headers),) = aioclient_mock.mock_calls
    assert (method.lower(), str(url)) == ("post", URL)
    assert data == {"ttl": 172800}
    assert headers["Authorization"] == f"Bearer {API_TOKEN}"


async def test_key_id_is_escaped_in_the_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A key ID can never change the request path."""
    url = "https://rtc.live.cloudflare.com/v1/turn/keys/..%2Fx%3Fy/credentials/generate-ice-servers"
    aioclient_mock.post(url, json=cloudflare_response("user", "pass"))
    client = CloudflareTurnClient(async_get_clientsession(hass), "../x?y", API_TOKEN)

    await client.async_generate_credentials(TTL)

    assert str(aioclient_mock.mock_calls[0][1]) == url


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (HTTPStatus.NOT_FOUND, CloudflareTurnKeyNotFoundError),
        (HTTPStatus.UNAUTHORIZED, CloudflareTurnAuthError),
        (HTTPStatus.FORBIDDEN, CloudflareTurnAuthError),
        (HTTPStatus.TOO_MANY_REQUESTS, CloudflareTurnRateLimitError),
        (HTTPStatus.BAD_REQUEST, CloudflareTurnResponseError),
        (HTTPStatus.INTERNAL_SERVER_ERROR, CloudflareTurnResponseError),
    ],
)
async def test_http_errors(
    client: CloudflareTurnClient,
    aioclient_mock: AiohttpClientMocker,
    status: HTTPStatus,
    error: type[CloudflareTurnError],
) -> None:
    """Each HTTP status maps to the right error."""
    aioclient_mock.post(URL, status=status, json={"error": "nope"})

    with pytest.raises(error):
        await client.async_generate_credentials(TTL)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("120", timedelta(seconds=120)),
        (" 30 ", timedelta(seconds=30)),
        ("Sat, 07 Nov 2026 12:28:00 GMT", timedelta(minutes=28)),
        ("Sat, 07 Nov 2026 12:05:00 -0000", timedelta(minutes=5)),
        ("Sat, 07 Nov 2026 11:00:00 GMT", timedelta(0)),
        ("soon", None),
    ],
)
async def test_retry_after(
    client: CloudflareTurnClient,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    header: str | None,
    expected: timedelta | None,
) -> None:
    """Retry-After is read in seconds or as an HTTP date."""
    freezer.move_to("2026-11-07 12:00:00+00:00")
    headers = {} if header is None else {"Retry-After": header}
    aioclient_mock.post(URL, status=HTTPStatus.TOO_MANY_REQUESTS, headers=headers)

    with pytest.raises(CloudflareTurnRateLimitError) as err:
        await client.async_generate_credentials(TTL)

    assert err.value.retry_after == expected


async def test_connection_error_hides_secrets(
    client: CloudflareTurnClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Connection errors never carry the key ID or the token."""
    aioclient_mock.post(URL, exc=ClientError(f"failed {URL} with {API_TOKEN}"))

    with pytest.raises(CloudflareTurnConnectionError) as err:
        await client.async_generate_credentials(TTL)

    assert KEY_ID not in str(err.value)
    assert API_TOKEN not in str(err.value)
    assert "**REDACTED**" in str(err.value)


async def test_timeout(
    client: CloudflareTurnClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A timeout is a connection error with a readable message."""
    aioclient_mock.post(URL, exc=TimeoutError())

    with pytest.raises(CloudflareTurnConnectionError, match=r"^TimeoutError$"):
        await client.async_generate_credentials(TTL)


async def test_empty_token_does_not_garble_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Redacting an empty secret must not rewrite the whole message."""
    aioclient_mock.post(URL, exc=ClientError("offline"))
    client = CloudflareTurnClient(async_get_clientsession(hass), KEY_ID, "")

    with pytest.raises(CloudflareTurnConnectionError, match=r"^offline$"):
        await client.async_generate_credentials(TTL)


async def test_invalid_json(
    client: CloudflareTurnClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A body that isn't JSON is a response error."""
    aioclient_mock.post(URL, status=HTTPStatus.CREATED, text="<html>oops</html>")

    with pytest.raises(CloudflareTurnResponseError, match="not valid JSON"):
        await client.async_generate_credentials(TTL)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "no iceServers"),
        ({"iceServers": "nope"}, "no iceServers"),
        ({"iceServers": []}, "no usable TURN server"),
        ({"iceServers": ["turn:x", {"urls": 5}]}, "no usable TURN server"),
        ({"iceServers": [{"urls": ["stun:stun.cloudflare.com:3478"]}]}, "no usable"),
        (
            {"iceServers": [{"urls": "turn:turn.cloudflare.com:3478"}]},
            "without credentials",
        ),
        (
            {
                "iceServers": [
                    {
                        "urls": "turn:turn.cloudflare.com",
                        "username": "u",
                        "credential": 5,
                    }
                ]
            },
            "without credentials",
        ),
        (
            {
                "iceServers": [
                    {
                        "urls": ["turn:bad host:3478", "turns:h:443?transport=udp"],
                        "username": "u",
                        "credential": "c",
                    }
                ]
            },
            "no usable TURN server",
        ),
    ],
)
def test_bad_payloads(payload: Any, message: str) -> None:
    """Malformed answers are rejected instead of reaching go2rtc."""
    now = dt_util.utcnow()
    with pytest.raises(CloudflareTurnResponseError, match=message):
        _parse_credentials(payload, now, now + TTL)


@pytest.mark.parametrize(
    ("url", "transport"),
    [
        ("turn:turn.cloudflare.com", "udp_3478"),
        ("turn:turn.cloudflare.com:3478", "udp_3478"),
        ("turn:turn.cloudflare.com:80?transport=tcp", "tcp_80"),
        ("turns:turn.cloudflare.com", "tls_5349"),
        ("turns:turn.cloudflare.com:443?transport=tcp", "tls_443"),
        ("turn:[2606:4700:48::1]:3478?transport=udp", "udp_3478"),
        ("turn:turn.cloudflare.com:53?transport=udp", None),
        ("turns:turn.cloudflare.com:443?transport=udp", None),
        ("turn:turn.cloudflare.com:0", None),
        ("turn:turn.cloudflare.com:70000", None),
        ("turn:turn.cloudflare.com:3478?transport=sctp", None),
        ("turn:turn.cloudflare.com:3478 ", None),
        ("stun:stun.cloudflare.com:3478", None),
        ("turn:192.0.2.1:3478", "udp_3478"),
        ("turn:...:3478?transport=udp", None),
        ("turn:[::::]:3478?transport=udp", None),
        ("turn:-bad.example:3478", None),
        ("turn:bad-.example:3478", None),
        ("turn:a..b:3478", None),
        ("turn:999.1.1.1:3478", None),
        ("turn:1.2.3:3478", None),
        (f"turn:{'a' * 64}.example:3478", None),
        (f"turn:{'.'.join(['abcdefgh'] * 30)}:3478", None),
    ],
)
def test_url_parsing(url: str, transport: str | None) -> None:
    """Only well-formed TURN URLs are kept, named by protocol and port."""
    now = dt_util.utcnow()
    payload = {
        "iceServers": [
            {
                "urls": [url, "turn:keep.example:3478"],
                "username": "u",
                "credential": "c",
            }
        ]
    }

    (server,) = _parse_credentials(payload, now, now + TTL).servers

    found = [u.transport for u in server.urls if u.url == url]
    assert found == ([] if transport is None else [transport])


def test_ice_servers_filter_and_fallback() -> None:
    """Chosen transports filter the URLs; none offered means all are used."""
    now = dt_util.utcnow()
    payload = {
        "iceServers": [
            {
                "urls": [
                    "turn:a.example:3478?transport=udp",
                    "turns:a.example:443?transport=tcp",
                ],
                "username": "ua",
                "credential": "ca",
            },
            {
                "urls": ["turn:b.example:80?transport=tcp"],
                "username": "ub",
                "credential": "cb",
            },
        ]
    }
    credentials = _parse_credentials(payload, now, now + TTL)

    servers = credentials.ice_servers(["tls_443", "tcp_80"])
    assert [(s.urls, s.username, s.credential) for s in servers] == [
        (["turns:a.example:443?transport=tcp"], "ua", "ca"),
        (["turn:b.example:80?transport=tcp"], "ub", "cb"),
    ]
    assert [s.urls for s in credentials.ice_servers(["udp_3478"])] == [
        ["turn:a.example:3478?transport=udp"]
    ]
    assert [s.urls for s in credentials.ice_servers(["udp_443"])] == [
        [
            "turn:a.example:3478?transport=udp",
            "turns:a.example:443?transport=tcp",
        ],
        ["turn:b.example:80?transport=tcp"],
    ]
