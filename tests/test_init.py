"""Tests for setting up Cloudflare TURN and keeping its credentials fresh."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from unittest.mock import patch

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
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
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
    issue_registry: ir.IssueRegistry,
) -> None:
    """A refresh that fails after unloading schedules and reports nothing."""
    freezer.move_to(START)
    await setup_integration(hass, mock_config_entry)
    coordinator = coordinator_of(mock_config_entry)
    cloudflare.gate = asyncio.Event()
    cloudflare.error = CloudflareTurnConnectionError("offline")

    # 23 hours left when this refresh fails: it would raise a Repairs issue.
    freezer.tick(timedelta(hours=25))
    async_fire_time_changed(hass)
    await asyncio.sleep(0)
    assert len(cloudflare.calls) == 2
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    cloudflare.gate.set()
    await hass.async_block_till_done()

    assert coordinator._unsub_timer is None
    assert coordinator._unsub_issue_timer is None
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING) is None
    await advance(hass, freezer, timedelta(days=3))
    assert len(cloudflare.calls) == 2


async def test_manual_refresh_follows_the_schedule(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Refreshing the sensor by hand starts the backoff, and a success resets it."""
    freezer.move_to(START)
    assert await async_setup_component(hass, "homeassistant", {})
    await setup_integration(hass, mock_config_entry)
    coordinator = coordinator_of(mock_config_entry)

    cloudflare.error = CloudflareTurnAuthError("revoked")
    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": "sensor.cloudflare_turn_credential_expiry"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert coordinator.failures == 1
    assert coordinator.next_refresh == dt_util.utcnow() + timedelta(minutes=1)
    assert len(reauth_flows(hass)) == 1

    # Let the entity's refresh cooldown (10 s) pass first.
    cloudflare.error = None
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": "sensor.cloudflare_turn_credential_expiry"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert coordinator.failures == 0
    assert coordinator.next_refresh == dt_util.utcnow() + timedelta(hours=12)
    assert reauth_flows(hass) == []


async def test_no_request_while_home_assistant_stops(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A refresh that comes due during shutdown doesn't call Cloudflare."""
    freezer.move_to(START)
    await setup_integration(hass, mock_config_entry)
    coordinator = coordinator_of(mock_config_entry)

    hass.set_state(CoreState.stopping)
    try:
        await advance(hass, freezer, timedelta(hours=12))
        assert len(cloudflare.calls) == 1
        coordinator._async_schedule_refresh()
        assert coordinator._unsub_timer is None
    finally:
        hass.set_state(CoreState.running)


async def test_stop_event_cancels_timers(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Home Assistant stopping cancels the refresh and Repairs timers."""
    freezer.move_to(START)
    await setup_integration(hass, mock_config_entry)
    coordinator = coordinator_of(mock_config_entry)
    cloudflare.error = CloudflareTurnConnectionError("offline")
    await advance(hass, freezer, timedelta(hours=12))
    assert coordinator._unsub_timer is not None
    assert coordinator._unsub_issue_timer is not None

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert coordinator._unsub_timer is None
    assert coordinator._unsub_issue_timer is None
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_stop_event_drops_a_queued_manual_refresh(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A manual refresh waiting out the cooldown doesn't run after stopping."""
    freezer.move_to(START)
    assert await async_setup_component(hass, "homeassistant", {})
    await setup_integration(hass, mock_config_entry)
    for _ in range(2):
        await hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": "sensor.cloudflare_turn_credential_expiry"},
            blocking=True,
        )
    await hass.async_block_till_done()
    assert len(cloudflare.calls) == 2

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    await advance(hass, freezer, timedelta(seconds=30))

    assert len(cloudflare.calls) == 2


async def test_repairs_deadlines_without_attempts(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Repairs follows the deadlines even while Retry-After spaces out attempts."""
    freezer.move_to(START)
    await setup_integration(hass, mock_config_entry)
    cloudflare.error = CloudflareTurnRateLimitError(timedelta(hours=5, minutes=30))

    # Attempts at +12, +17.5 and +23 hours: more than 24 hours left each time.
    await advance(hass, freezer, timedelta(hours=12))
    await advance(hass, freezer, timedelta(hours=5, minutes=30))
    await advance(hass, freezer, timedelta(hours=5, minutes=30))
    assert len(cloudflare.calls) == 4
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING) is None

    # +24 hours: no attempt is due, yet the warning appears on time.
    await advance(hass, freezer, timedelta(hours=1))
    assert len(cloudflare.calls) == 4
    issue = issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING

    # Attempts at +28.5, +34, +39.5 and +45 hours keep the warning.
    await advance(hass, freezer, timedelta(hours=4, minutes=30))
    for _ in range(3):
        await advance(hass, freezer, timedelta(hours=5, minutes=30))
    assert len(cloudflare.calls) == 8
    issue = issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING

    # +47 hours, one hour left: it turns into an error without an attempt.
    await advance(hass, freezer, timedelta(hours=2))
    assert len(cloudflare.calls) == 8
    issue = issue_registry.async_get_issue(DOMAIN, ISSUE_REFRESH_FAILING)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert turn_servers(hass) == []


async def test_failed_unload_keeps_refreshing(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """If the sensor can't be unloaded, the relay and its refreshes carry on."""
    freezer.move_to(START)
    await setup_integration(hass, mock_config_entry)

    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False
    ):
        assert not await hass.config_entries.async_unload(mock_config_entry.entry_id)

    assert mock_config_entry.state is ConfigEntryState.FAILED_UNLOAD
    assert len(turn_servers(hass)) == 1
    await advance(hass, freezer, timedelta(hours=12))
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
