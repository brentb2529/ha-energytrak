"""Config flow for EnergyTrak."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    ConfigEntry,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    EnergyTrakAuthError,
    EnergyTrakClient,
    EnergyTrakConnectionError,
    EnergyTrakError,
)
from .const import (
    CONF_API_KEY,
    CONF_EMAIL,
    CONF_MAGIC_LINK,
    CONF_MANUAL_SITE_IDS,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SITE_IDS,
    CONF_SITE_NAMES,
    CONF_STALE_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_MINUTES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="email")
        ),
        vol.Required(CONF_MAGIC_LINK): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MAGIC_LINK): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
    }
)


class EnergyTrakConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the EnergyTrak config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._client: EnergyTrakClient | None = None
        self._discovered: list[dict[str, str]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the account email and a fresh sign-in link."""
        errors: dict[str, str] = {}

        if user_input is not None:
            client = EnergyTrakClient(
                async_get_clientsession(self.hass), user_input[CONF_EMAIL].strip()
            )
            try:
                await client.async_sign_in_with_magic_link(user_input[CONF_MAGIC_LINK])
            except EnergyTrakAuthError as err:
                errors["base"] = _auth_error_key(err)
            except EnergyTrakConnectionError:
                errors["base"] = "cannot_connect"
            except EnergyTrakError:
                _LOGGER.exception("Unexpected error during EnergyTrak sign-in")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(client.user_id or client.email)
                self._abort_if_unique_id_configured()
                self._client = client
                return await self.async_step_sites()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={"domain": "energytrak.app"},
        )

    async def async_step_sites(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which sites to bring into Home Assistant."""
        assert self._client is not None
        errors: dict[str, str] = {}

        if not self._discovered:
            try:
                self._discovered = await self._client.async_list_sites()
            except EnergyTrakError as err:
                # Site listing needs collection-level read access, which some
                # accounts do not have. Fall back to typing ids by hand.
                _LOGGER.debug("Site discovery failed, falling back to manual: %s", err)
                self._discovered = []

        if user_input is not None:
            selected: list[str] = list(user_input.get(CONF_SITE_IDS, []))
            manual = user_input.get(CONF_MANUAL_SITE_IDS, "")
            selected.extend(
                part.strip() for part in str(manual).split(",") if part.strip()
            )
            # Preserve order, drop duplicates.
            site_ids = list(dict.fromkeys(selected))

            if not site_ids:
                errors["base"] = "no_sites_selected"
            else:
                names = {
                    site["site_id"]: site["name"]
                    for site in self._discovered
                    if site["site_id"] in site_ids
                }
                return self.async_create_entry(
                    title=self._client.email,
                    data={
                        CONF_EMAIL: self._client.email,
                        CONF_API_KEY: self._client.api_key,
                        CONF_REFRESH_TOKEN: self._client.refresh_token,
                        CONF_SITE_IDS: site_ids,
                        CONF_SITE_NAMES: names,
                    },
                )

        schema_dict: dict[Any, Any] = {}
        if self._discovered:
            schema_dict[vol.Optional(CONF_SITE_IDS, default=[])] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=site["site_id"],
                            label=f"{site['name']} ({site['site_id']})",
                        )
                        for site in self._discovered
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        schema_dict[vol.Optional(CONF_MANUAL_SITE_IDS, default="")] = TextSelector()

        return self.async_show_form(
            step_id="sites",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication (the refresh token was rejected)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a new magic link and swap in the new refresh token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            client = EnergyTrakClient(
                async_get_clientsession(self.hass), entry.data[CONF_EMAIL]
            )
            try:
                await client.async_sign_in_with_magic_link(user_input[CONF_MAGIC_LINK])
            except EnergyTrakAuthError as err:
                errors["base"] = _auth_error_key(err)
            except EnergyTrakConnectionError:
                errors["base"] = "cannot_connect"
            except EnergyTrakError:
                _LOGGER.exception("Unexpected error during EnergyTrak re-auth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_API_KEY: client.api_key,
                        CONF_REFRESH_TOKEN: client.refresh_token,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return EnergyTrakOptionsFlow()


class EnergyTrakOptionsFlow(OptionsFlow):
    """Tune polling and staleness behaviour."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=15, max=900, step=5, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_STALE_MINUTES,
                    default=options.get(CONF_STALE_MINUTES, DEFAULT_STALE_MINUTES),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=1440, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


def _auth_error_key(err: EnergyTrakAuthError) -> str:
    """Map an upstream auth failure onto a translated error string."""
    message = str(err).upper()
    if "INVALID_MAGIC_LINK" in message:
        return "invalid_magic_link"
    if "EXPIRED" in message:
        return "expired_link"
    if "EMAIL" in message:
        return "email_mismatch"
    return "invalid_auth"
