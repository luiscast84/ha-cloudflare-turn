"""Keep Cloudflare TURN credentials fresh."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging

from webrtc_models import RTCIceServer

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CloudflareTurnAuthError,
    CloudflareTurnClient,
    CloudflareTurnError,
    CloudflareTurnKeyNotFoundError,
    CloudflareTurnRateLimitError,
    TurnCredentials,
)
from .const import (
    CONF_TRANSPORTS,
    CREDENTIAL_TTL,
    DEFAULT_TRANSPORTS,
    DOMAIN,
    EXPIRY_MARGIN,
    ISSUE_REFRESH_FAILING,
    ISSUE_WHEN_LEFT,
    REFRESH_INTERVAL,
    RETRY_MAX,
    RETRY_MIN,
    STATUS_URL,
    TITLE,
)

_LOGGER = logging.getLogger(__name__)

# Longest wait honoured from a Retry-After header.
RATE_LIMIT_MAX = timedelta(hours=6)

type CloudflareTurnConfigEntry = ConfigEntry[CloudflareTurnCoordinator]


class CloudflareTurnCoordinator(DataUpdateCoordinator[TurnCredentials]):
    """Fetch TURN credentials and renew them before they expire.

    Refreshes are scheduled here instead of through ``update_interval``: the base
    class only polls while an entity listens and polling is enabled for the
    entry, and the credentials must be renewed either way.
    """

    config_entry: CloudflareTurnConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CloudflareTurnConfigEntry,
        client: CloudflareTurnClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=TITLE, update_interval=None
        )
        self.client = client
        self.failures = 0
        self.failing_since: datetime | None = None
        self.next_refresh: datetime | None = None
        self.last_error: str | None = None
        self.auth_failed = False
        self._wait_requested: timedelta | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._stopped = False
        self._timer_job = HassJob(
            self._async_timer_fired,
            f"{TITLE} credential refresh",
            cancel_on_shutdown=True,
        )

    async def _async_update_data(self) -> TurnCredentials:
        """Ask Cloudflare for new credentials."""
        self.auth_failed = False
        self._wait_requested = None
        try:
            credentials = await self.client.async_generate_credentials(CREDENTIAL_TTL)
        except CloudflareTurnError as err:
            self.last_error = str(err)
            if isinstance(err, CloudflareTurnAuthError):
                self.auth_failed = True
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key=(
                        "key_not_found"
                        if isinstance(err, CloudflareTurnKeyNotFoundError)
                        else "invalid_auth"
                    ),
                ) from err
            if isinstance(err, CloudflareTurnRateLimitError):
                self._wait_requested = err.retry_after
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="refresh_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.last_error = None
        chosen = self.config_entry.options.get(CONF_TRANSPORTS, DEFAULT_TRANSPORTS)
        if not credentials.transports & set(chosen):
            _LOGGER.warning(
                "Cloudflare no longer offers the chosen relay transports (%s), "
                "so all it offers are used: %s",
                ", ".join(chosen),
                ", ".join(sorted(credentials.transports)),
            )
        return credentials

    @callback
    def async_get_ice_servers(self) -> list[RTCIceServer]:
        """Return the relay servers for a new WebRTC session.

        Home Assistant calls this for every session: for the client configuration
        the frontend and the apps ask for, and for every offer it sends to go2rtc.
        """
        credentials = self.data
        if dt_util.utcnow() >= credentials.expires_at - EXPIRY_MARGIN:
            return []
        return credentials.ice_servers(
            self.config_entry.options.get(CONF_TRANSPORTS, DEFAULT_TRANSPORTS)
        )

    @callback
    def async_schedule_refresh(self) -> None:
        """Plan the next refresh from the outcome of the last one."""
        self._async_cancel_timer()
        if self._stopped:
            return
        now = dt_util.utcnow()
        if self.last_update_success:
            self.failures = 0
            self.failing_since = None
            delay = REFRESH_INTERVAL
            self._async_abort_reauth_flows()
        else:
            # After an authentication failure the reauthentication flow is open,
            # but retrying continues in case Cloudflare's refusal was temporary.
            self.failures += 1
            if self.failing_since is None:
                self.failing_since = now
            delay = min(RETRY_MIN * 2 ** min(self.failures - 1, 10), RETRY_MAX)
            if self._wait_requested is not None:
                delay = min(max(delay, self._wait_requested), RATE_LIMIT_MAX)
        self.next_refresh = now + delay
        self._unsub_timer = async_call_later(self.hass, delay, self._timer_job)
        self._async_update_issue(now)

    async def _async_timer_fired(self, _now: datetime) -> None:
        """Refresh the credentials when the timer fires."""
        self._unsub_timer = None
        await self.async_refresh()
        self.async_schedule_refresh()

    async def async_shutdown(self) -> None:
        """Stop refreshing when the entry is unloaded."""
        self._stopped = True
        self._async_cancel_timer()
        await super().async_shutdown()

    @callback
    def _async_cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _async_abort_reauth_flows(self) -> None:
        """Close reauthentication requests that a later success made moot."""
        for flow in self.config_entry.async_get_active_flows(
            self.hass, {SOURCE_REAUTH}
        ):
            self.hass.config_entries.flow.async_abort(flow["flow_id"])

    @callback
    def _async_update_issue(self, now: datetime) -> None:
        """Report in Repairs when failing refreshes put the relay at risk."""
        credentials = self.data
        left = credentials.expires_at - now
        if self.last_update_success or self.auth_failed or left > ISSUE_WHEN_LEFT:
            # An authentication failure has its own reauthentication notice.
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_REFRESH_FAILING)
            return
        expired = left <= EXPIRY_MARGIN
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_REFRESH_FAILING,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR if expired else ir.IssueSeverity.WARNING,
            translation_key=(
                f"{ISSUE_REFRESH_FAILING}_expired" if expired else ISSUE_REFRESH_FAILING
            ),
            translation_placeholders={
                "error": self.last_error or "-",
                "expires": _local_time(credentials.expires_at),
                "since": _local_time(self.failing_since or now),
                "status_url": STATUS_URL,
            },
        )


def _local_time(value: datetime) -> str:
    """Format a time for a Repairs message."""
    return dt_util.as_local(value).strftime("%Y-%m-%d %H:%M")
