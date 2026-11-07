"""Config flow for Cloudflare TURN."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

try:
    import probatio as vol
except ImportError:  # pragma: no cover - Home Assistant 2026.1 to 2026.8
    import voluptuous as vol  # type: ignore[no-redef]

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    KEY_ID_PATTERN,
    CloudflareTurnAuthError,
    CloudflareTurnClient,
    CloudflareTurnConnectionError,
    CloudflareTurnError,
    CloudflareTurnKeyNotFoundError,
    CloudflareTurnRateLimitError,
)
from .const import (
    CONF_API_TOKEN,
    CONF_KEY_ID,
    CONF_TRANSPORTS,
    DASHBOARD_URL,
    DEFAULT_TRANSPORTS,
    DOMAIN,
    TITLE,
    TRANSPORTS,
    VALIDATION_TTL,
)
from .coordinator import CloudflareTurnConfigEntry

_LOGGER = logging.getLogger(__name__)

CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_KEY_ID): TextSelector(TextSelectorConfig(autocomplete="off")),
        vol.Required(CONF_API_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off")
        ),
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRANSPORTS): SelectSelector(
            SelectSelectorConfig(
                options=list(TRANSPORTS),
                multiple=True,
                mode=SelectSelectorMode.LIST,
                translation_key=CONF_TRANSPORTS,
            )
        ),
    }
)


async def _async_validate(
    hass: HomeAssistant, user_input: Mapping[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
    """Check a TURN key with Cloudflare. Return the cleaned data and any errors."""
    data = {
        CONF_KEY_ID: str(user_input[CONF_KEY_ID]).strip(),
        CONF_API_TOKEN: str(user_input[CONF_API_TOKEN]).strip(),
    }
    if not KEY_ID_PATTERN.fullmatch(data[CONF_KEY_ID]):
        return data, {CONF_KEY_ID: "invalid_key_id"}
    client = CloudflareTurnClient(
        async_get_clientsession(hass), data[CONF_KEY_ID], data[CONF_API_TOKEN]
    )
    errors: dict[str, str] = {}
    try:
        await client.async_generate_credentials(VALIDATION_TTL)
    except CloudflareTurnKeyNotFoundError:
        errors[CONF_KEY_ID] = "key_not_found"
    except CloudflareTurnAuthError:
        errors[CONF_API_TOKEN] = "invalid_auth"
    except CloudflareTurnRateLimitError:
        errors["base"] = "rate_limited"
    except CloudflareTurnConnectionError:
        errors["base"] = "cannot_connect"
    except CloudflareTurnError:
        _LOGGER.exception("Unexpected response from Cloudflare")
        errors["base"] = "unexpected_response"
    except Exception:
        _LOGGER.exception("Unexpected error while checking the TURN key")
        errors["base"] = "unknown"
    return data, errors


class CloudflareTurnConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Cloudflare TURN."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: CloudflareTurnConfigEntry,
    ) -> CloudflareTurnOptionsFlow:
        """Return the options flow."""
        return CloudflareTurnOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the TURN key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _async_validate(self.hass, user_input)
            if not errors:
                return self.async_create_entry(
                    title=TITLE,
                    data=data,
                    options={CONF_TRANSPORTS: list(DEFAULT_TRANSPORTS)},
                )
        return self._async_show_credentials_form("user", user_input, errors)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle Cloudflare rejecting the TURN key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the TURN key again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _async_validate(self.hass, user_input)
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self._async_show_credentials_form(
            "reauth_confirm",
            user_input or {CONF_KEY_ID: entry.data[CONF_KEY_ID]},
            errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the TURN key."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _async_validate(self.hass, user_input)
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self._async_show_credentials_form(
            "reconfigure",
            user_input or {CONF_KEY_ID: entry.data[CONF_KEY_ID]},
            errors,
        )

    @callback
    def _async_show_credentials_form(
        self,
        step_id: str,
        suggested_values: Mapping[str, Any] | None,
        errors: dict[str, str],
    ) -> ConfigFlowResult:
        """Show the TURN key form."""
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, suggested_values
            ),
            errors=errors,
            description_placeholders={"dashboard_url": DASHBOARD_URL},
        )


class CloudflareTurnOptionsFlow(OptionsFlow):
    """Choose which relay transports to use."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the transport choice."""
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = set(user_input[CONF_TRANSPORTS])
            if chosen:
                return self.async_create_entry(
                    data={CONF_TRANSPORTS: [t for t in TRANSPORTS if t in chosen]}
                )
            errors[CONF_TRANSPORTS] = "no_transports"
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA,
                user_input
                or {
                    CONF_TRANSPORTS: self.config_entry.options.get(
                        CONF_TRANSPORTS, list(DEFAULT_TRANSPORTS)
                    )
                },
            ),
            errors=errors,
        )
