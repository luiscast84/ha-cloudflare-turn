"""Tests for setting up Cloudflare TURN and keeping its credentials fresh."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.cloudflare_turn.api import (
    CloudflareTurnAuthError,
    CloudflareTurnConnectionError,
    CloudflareTurnKeyNotFoundError,
    CloudflareTurnRateLimitError,
)
from custom_components.cloudflare_turn.const import (
    CONF_TRANSPORTS,
    CREDENTIAL_TTL,
    DOMAIN,
    ISSUE_REFRESH_FAILING,
)
from custom_components.cloudflare_turn.coordinator import CloudflareTurnCoordinator
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util

from .conftest import (
    DEFAULT_URLS,
    START,
    FakeCloudflare,
    setup_integration,
    turn_servers,
)


async def advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta
) -> None:
    """Move the clock forward and run whatever became due."""
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def coordinator_of(entry: MockConfigEntry) -> CloudflareTurnCoordinator:
    """Return the entry's coordinator."""
    coordinator: CloudflareTurnCoordinator = entry.runtime_data
    return coordinator


def reauth_flows(hass: HomeAssistant) -> list[str]:
    """Return the IDs of open reauthentication flows."""
    return [
        flow["flow_id"]
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


async def test_setup_and_unload(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Setup offers the relay to new sessions; unloading withdraws it."""
    freezer.move_to(START)
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert cloudflare.calls == [CREDENTIAL_TTL]
    (server,) = turn_servers(hass)
    assert server.urls == DEFAULT_URLS
    assert (server.username, server.credential) == ("user-1", "cred-1")
    coordinator = coordinator_of(mock_config_entry)
    assert coordinator.next_refresh == dt_util.utcnow() + timedelta(hours=12)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert turn_servers(hass) == []
    await advance(hass, freezer, timedelta(hours=12))
    assert len(cloudflare.calls) == 1


async def test_websocket_lists_the_relay(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The ICE servers Home Assistant reports include the relay."""
    await setup_integration(hass, mock_config_entry)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "web_rtc/ice_servers"})
    msg = await client.receive_json()

    assert msg["success"]
    assert {
        "urls": DEFAULT_URLS,
        "username": "user-1",
        "credential": "cred-1",
    } in msg["result"]


async def test_setup_retries_when_cloudflare_is_unreachable(
    hass: HomeAssistant, cloudflare: FakeCloudflare, mock_config_entry: MockConfigEntry
) -> None:
    """A connection error at startup means retrying setup later."""
    cloudflare.error = CloudflareTurnConnectionError("offline")

    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert turn_servers(hass) == []


@pytest.mark.parametrize(
    "error",
    [CloudflareTurnAuthError("bad token"), CloudflareTurnKeyNotFoundError("no key")],
)
async def test_setup_asks_for_the_key_again(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    error: Exception,
) -> None:
    """A rejected key at startup starts reauthentication."""
    cloudflare.error = error

    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert len(reauth_flows(hass)) == 1


async def test_refresh_every_12_hours(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """Credentials renew every 12 hours with no entity and polling turned off."""
    freezer.move_to(START)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"key_id": "0123456789abcdef", "api_token": "token"},
        pref_disable_polling=True,
    )
    entry.add_to_hass(hass)
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_credentials_expire",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.async_entity_ids("sensor") == []

    await advance(hass, freezer, timedelta(hours=11, minutes=59))
    assert len(cloudflare.calls) == 1

    await advance(hass, freezer, timedelta(minutes=1))
    assert len(cloudflare.calls) == 2
    (server,) = turn_servers(hass)
    assert server.username == "user-2"

    await advance(hass, freezer, timedelta(hours=12))
    assert len(cloudflare.calls) == 3


async def test_retries_back_off_then_recover(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Failed refreshes retry after 1, 2, 4 ... 30 minutes, keeping the old ones."""
    freezer.move_to(START)
    await setup_integration(hass, mock_config_entry)
    coordinator = coordinator_of(mock_config_entry)
    cloudflare.error = CloudflareTurnConnectionError("offline")

    await advance(hass, freezer, timedelta(hours=12))
    waits = []
    for _ in range(7):
        wait = coordinator.next_refresh - dt_util.utcnow()
        waits.append(wait)
        await advance(hass, freezer, wait)
    assert waits == [timedelta(minutes=m) for m in (1, 2, 4, 8, 16, 30, 30)]
    assert coordinator.failures == 8
    assert coordinator.last_error == "offline"
    (server,) = turn_servers(hass)
    assert server.username == "user-1"
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING) is None

    cloudflare.error = None
    await advance(hass, freezer, coordinator.next_refresh - dt_util.utcnow())

    assert coordinator.failures == 0
    assert coordinator.failing_since is None
    assert coordinator.last_error is None
    assert coordinator.next_refresh == dt_util.utcnow() + timedelta(hours=12)
    (server,) = turn_servers(hass)
    assert server.username == f"user-{len(cloudflare.calls)}"


@pytest.mark.parametrize(
    ("retry_after", "wait"),
    [
        (timedelta(hours=2), timedelta(hours=2)),
        (timedelta(days=3), timedelta(hours=6)),
        (timedelta(seconds=5), timedelta(minutes=1)),
        (None, timedelta(minutes=1)),
    ],
)
async def test_rate_limit_waits_as_asked(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    retry_after: timedelta | None,
    wait: timedelta,
) -> None:
    """Retry-After is honoured, within reason."""
    freezer.move_to(START)
    await setup_integration(hass, mock_config_entry)
    cloudflare.error = CloudflareTurnRateLimitError(retry_after)

    await advance(hass, freezer, timedelta(hours=12))

    coordinator = coordinator_of(mock_config_entry)
    assert coordinator.next_refresh == dt_util.utcnow() + wait


async def test_repair_issue_and_expiry(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Long failures are reported, and credentials stop being handed out near expiry."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(START)
    await setup_integration(hass, mock_config_entry)
    cloudflare.error = CloudflareTurnConnectionError("offline")

    # 36 hours left at the first failure: nothing to report yet.
    await advance(hass, freezer, timedelta(hours=12))
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING) is None

    # 24 hours left: a warning.
    await advance(hass, freezer, timedelta(hours=12))
    issue = issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == ISSUE_REFRESH_FAILING
    assert issue.translation_placeholders == {
        "error": "offline",
        "expires": "2026-11-09 12:00",
        "since": "2026-11-08 00:00",
        "status_url": "https://www.cloudflarestatus.com/",
    }
    assert len(turn_servers(hass)) == 1

    # One hour left: no longer handed out, and the issue becomes an error.
    await advance(hass, freezer, timedelta(hours=23))
    assert turn_servers(hass) == []
    issue = issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == f"{ISSUE_REFRESH_FAILING}_expired"

    # Recovery clears it.
    cloudflare.error = None
    await advance(hass, freezer, timedelta(minutes=30))
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING) is None
    assert len(turn_servers(hass)) == 1

    # Unloading also clears a pending issue.
    cloudflare.error = CloudflareTurnConnectionError("offline")
    await advance(hass, freezer, timedelta(hours=40))
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING) is not None
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING) is None


async def test_auth_failure_reauths_and_keeps_retrying(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A rejected key opens reauthentication; a later success closes it again."""
    freezer.move_to(START)
    await setup_integration(hass, mock_config_entry)
    coordinator = coordinator_of(mock_config_entry)
    cloudflare.error = CloudflareTurnAuthError("revoked")

    await advance(hass, freezer, timedelta(hours=12))
    assert coordinator.auth_failed
    assert len(reauth_flows(hass)) == 1
    assert coordinator.next_refresh == dt_util.utcnow() + timedelta(minutes=1)

    # Still rejected a day later: reauthentication covers it, no Repairs issue.
    await advance(hass, freezer, timedelta(hours=24))
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING) is None
    assert len(reauth_flows(hass)) == 1

    cloudflare.error = None
    await advance(hass, freezer, coordinator.next_refresh - dt_util.utcnow())
    assert not coordinator.auth_failed
    assert reauth_flows(hass) == []
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_unload_during_refresh_leaves_no_timer(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A refresh that finishes after unloading schedules nothing more."""
    freezer.move_to(START)
    await setup_integration(hass, mock_config_entry)
    coordinator = coordinator_of(mock_config_entry)
    cloudflare.gate = asyncio.Event()

    freezer.tick(timedelta(hours=12))
    async_fire_time_changed(hass)
    await asyncio.sleep(0)
    assert len(cloudflare.calls) == 2
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    cloudflare.gate.set()
    await hass.async_block_till_done()

    assert coordinator._unsub_timer is None
    await advance(hass, freezer, timedelta(days=3))
    assert len(cloudflare.calls) == 2


async def test_unknown_transports_fall_back(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If Cloudflare drops the chosen transports, every offered one is used."""
    cloudflare.urls = ["turns:turn.cloudflare.com:8443?transport=tcp"]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"key_id": "0123456789abcdef", "api_token": "token"},
        options={CONF_TRANSPORTS: ["udp_3478"]},
    )

    with caplog.at_level(logging.WARNING):
        await setup_integration(hass, entry)

    (server,) = turn_servers(hass)
    assert server.urls == ["turns:turn.cloudflare.com:8443?transport=tcp"]
    assert "no longer offers the chosen relay transports (udp_3478)" in caplog.text
