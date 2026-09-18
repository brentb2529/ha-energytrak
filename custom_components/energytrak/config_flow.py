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
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
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

from .const import (
    CONF_ATTACH_TO_ENTRY,
    CONF_HAS_CLOUD,
    CONF_LOCAL_HOST,
    CONF_LOCAL_KEY,
    CONF_LOCAL_PORT,
    CONF_LOCAL_SITE_ID,
    DEFAULT_LOCAL_PORT,
    LOCAL_PROJECT_NAME,
    LOCAL_SITE_PREFIX,
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
    STANDALONE,
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

    # Set by async_step_zeroconf when a B-Infohub bridge announces itself.
    _discovered_bridge: dict[str, Any] | None = None

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """A B-Infohub bridge appeared on the network.

        The manifest filters discovery on the bridge's mDNS project_name, so
        this only ever fires for our own hardware -- never for somebody's
        unrelated ESPHome device. The check is repeated here because manifest
        filtering is matched by Home Assistant, not guaranteed by us.
        """
        props = discovery_info.properties or {}
        if props.get("project_name") != LOCAL_PROJECT_NAME:
            return self.async_abort(reason="not_b_infohub")

        # The bridge extends an existing EnergyTrak account rather than being
        # an account of its own -- it has no site list, no serial number and no
        # subscription. With no entry to attach to there is nothing to do yet.
        # Prefer attaching to an existing account, but a bridge with no
        # account is a complete installation in its own right, not an error.
        entries = [
            e for e in self._async_current_entries(include_ignore=False)
            if e.data.get(CONF_EMAIL)
        ]
        if mac := props.get("mac"):
            await self.async_set_unique_id(f"{LOCAL_SITE_PREFIX}:{mac}")
            self._abort_if_unique_id_configured(
                updates={CONF_LOCAL_HOST: discovery_info.host}
            )

        host = discovery_info.host
        self._discovered_bridge = {
            CONF_LOCAL_HOST: host,
            CONF_LOCAL_PORT: discovery_info.port or DEFAULT_LOCAL_PORT,
            "mac": props.get("mac"),
            "name": props.get("friendly_name") or discovery_info.name.split(".")[0],
            "entry_id": entries[0].entry_id if entries else None,
        }
        self.context["title_placeholders"] = {"name": self._discovered_bridge["name"]}
        return await self.async_step_bridge()

    async def async_step_bridge(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the bridge and collect its API encryption key."""
        assert self._discovered_bridge is not None
        bridge = self._discovered_bridge
        entry = (
            self.hass.config_entries.async_get_entry(bridge["entry_id"])
            if bridge["entry_id"]
            else None
        )

        errors: dict[str, str] = {}
        if user_input is not None:
            ok, detail = await self._async_probe_bridge(
                bridge[CONF_LOCAL_HOST], bridge[CONF_LOCAL_PORT],
                user_input[CONF_LOCAL_KEY],
            )
            if not ok:
                errors["base"] = detail
                user_input = None

        if user_input is not None and entry is None:
            # No account: the bridge stands alone.
            site_id = f"{LOCAL_SITE_PREFIX}:{bridge.get('mac') or bridge[CONF_LOCAL_HOST]}"
            return self.async_create_entry(
                title=bridge["name"],
                data={
                    CONF_LOCAL_HOST: bridge[CONF_LOCAL_HOST],
                    CONF_LOCAL_PORT: bridge[CONF_LOCAL_PORT],
                    CONF_LOCAL_KEY: user_input[CONF_LOCAL_KEY],
                    CONF_LOCAL_SITE_ID: site_id,
                },
            )

        if user_input is not None:
            data = {
                **entry.data,
                CONF_LOCAL_HOST: bridge[CONF_LOCAL_HOST],
                CONF_LOCAL_PORT: bridge[CONF_LOCAL_PORT],
                CONF_LOCAL_KEY: user_input[CONF_LOCAL_KEY],
            }
            if site_id := user_input.get(CONF_LOCAL_SITE_ID):
                data[CONF_LOCAL_SITE_ID] = site_id
            self.hass.config_entries.async_update_entry(entry, data=data)
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="bridge_added")

        # Which generator the bridge is wired to. Only worth asking when the
        # account has more than one site; otherwise the answer is obvious and
        # the coordinator defaults to it.
        site_names: dict[str, str] = entry.data.get("site_names", {}) if entry else {}
        site_ids: list[str] = list(entry.data.get("site_ids", [])) if entry else []
        schema: dict[Any, Any] = {
            vol.Required(CONF_LOCAL_KEY): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            )
        }
        if len(site_ids) > 1:
            schema[vol.Required(CONF_LOCAL_SITE_ID)] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=s, label=site_names.get(s, s))
                        for s in site_ids
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="bridge",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "name": bridge["name"],
                "host": bridge[CONF_LOCAL_HOST],
            },
        )

    def __init__(self) -> None:
        """Initialise flow state."""
        self._client: EnergyTrakClient | None = None
        self._discovered: list[dict[str, str]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which half of the integration to set up.

        Three kinds of installation are equally valid and none is a degraded
        form of another:

          cloud only    -- an EnergyTrak account and no hardware. What almost
                           everybody has, and how this integration began.
          bridge only   -- a B-Infohub RS-485 bridge and no subscription. The
                           whole point of building the hardware was to stop
                           paying for the cloud, so requiring an account here
                           would defeat it.
          both          -- the bridge is authoritative while its bus is
                           healthy; the cloud carries on underneath and takes
                           over when it is not.

        A second half can be added later from the same menu, so this is not a
        decision anyone is stuck with.
        """
        return self.async_show_menu(
            step_id="user",
            menu_options=["cloud", "bridge_manual"],
        )

    async def async_step_cloud(
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
            step_id="cloud",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={"domain": "energytrak.app"},
        )

    async def async_step_bridge_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up a B-Infohub bridge by address, without waiting for discovery.

        Discovery normally finds it, but mDNS does not cross VLANs and plenty
        of sensible networks put a generator controller on its own segment.

        THIS PATH MUST BE ABLE TO PRODUCE THE "BOTH" INSTALL, NOT JUST
        "BRIDGE ONLY".

        It previously always created a standalone entry, so a user with an
        EnergyTrak account who added a bridge by hand ended up with two
        unrelated entries and two device cards for one generator -- the cloud
        one going stale beside the local one, with no fallback between them,
        because falling back is something a single coordinator does across two
        sources it owns.

        Attaching was only ever reachable through zeroconf. That is precisely
        backwards: mDNS does not cross subnets, so the networks where the
        bridge cannot be discovered are exactly the networks where it must be
        added by hand -- and those users were silently denied the one
        configuration that has both redundancy and full resolution.

        So: if an account entry exists, offer to attach to it. If the user
        declines, or there is no account, the bridge stands alone as before.
        """
        errors: dict[str, str] = {}

        # Cloud entries only. A bridge-only entry has no account to join, and
        # attaching a second bridge to one would silently replace the first.
        account_entries = [
            e
            for e in self._async_current_entries()
            if e.data.get(CONF_EMAIL) and not e.data.get(CONF_LOCAL_HOST)
        ]

        if user_input is not None:
            host = user_input[CONF_LOCAL_HOST].strip()
            port = int(user_input.get(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT))
            ok, detail = await self._async_probe_bridge(
                host, port, user_input[CONF_LOCAL_KEY]
            )
            if not ok:
                errors["base"] = detail
            else:
                attach_to = user_input.get(CONF_ATTACH_TO_ENTRY) or ""
                entry = (
                    self.hass.config_entries.async_get_entry(attach_to)
                    if attach_to and attach_to != STANDALONE
                    else None
                )

                if entry is not None:
                    # "Both": one entry, one coordinator, one device card,
                    # with the bridge authoritative and the cloud underneath.
                    data = {
                        **entry.data,
                        CONF_LOCAL_HOST: host,
                        CONF_LOCAL_PORT: port,
                        CONF_LOCAL_KEY: user_input[CONF_LOCAL_KEY],
                    }
                    site_ids: list[str] = list(entry.data.get(CONF_SITE_IDS, []))
                    chosen = user_input.get(CONF_LOCAL_SITE_ID)
                    if chosen:
                        data[CONF_LOCAL_SITE_ID] = chosen
                    elif len(site_ids) == 1:
                        # Unambiguous, so do not make the user restate it.
                        data[CONF_LOCAL_SITE_ID] = site_ids[0]
                    self.hass.config_entries.async_update_entry(entry, data=data)
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="bridge_added")

                await self.async_set_unique_id(f"{LOCAL_SITE_PREFIX}:{detail}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Generator bridge ({host})",
                    data={
                        CONF_LOCAL_HOST: host,
                        CONF_LOCAL_PORT: port,
                        CONF_LOCAL_KEY: user_input[CONF_LOCAL_KEY],
                        CONF_LOCAL_SITE_ID: f"{LOCAL_SITE_PREFIX}:{detail}",
                    },
                )

        schema: dict[Any, Any] = {
            vol.Required(CONF_LOCAL_HOST): TextSelector(),
            vol.Optional(CONF_LOCAL_PORT, default=DEFAULT_LOCAL_PORT): int,
            vol.Required(CONF_LOCAL_KEY): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }

        if account_entries:
            options = [
                SelectOptionDict(value=e.entry_id, label=f"Add to {e.title}")
                for e in account_entries
            ]
            options.append(
                SelectOptionDict(value=STANDALONE, label="Local bridge only")
            )
            # Defaulting to the account is the right bias: someone who has an
            # account AND hardware almost always wants both, and standalone is
            # the choice that quietly gives up the cloud fallback.
            schema[
                vol.Required(CONF_ATTACH_TO_ENTRY, default=account_entries[0].entry_id)
            ] = SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
            )

            site_names: dict[str, str] = account_entries[0].data.get(CONF_SITE_NAMES, {})
            site_ids = list(account_entries[0].data.get(CONF_SITE_IDS, []))
            if len(site_ids) > 1:
                schema[vol.Optional(CONF_LOCAL_SITE_ID)] = SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=s, label=site_names.get(s, s))
                            for s in site_ids
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )

        return self.async_show_form(
            step_id="bridge_manual",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def _async_probe_bridge(
        self, host: str, port: int, key: str
    ) -> tuple[bool, str]:
        """Connect once to prove the address and key work.

        Worth the round trip: a wrong encryption key otherwise produces an
        entry that looks configured and silently never delivers a reading.
        Returns (True, mac) or (False, error_key).
        """
        from aioesphomeapi import APIClient

        client = APIClient(host, port, password=None, noise_psk=key or None,
                           client_info="ha-energytrak")
        try:
            await client.connect(login=True)
            info = await client.device_info()
        except Exception as err:  # noqa: BLE001 - surfaced as a form error
            _LOGGER.debug("Bridge probe failed for %s: %s", host, err)
            text = str(err).lower()
            if "auth" in text or "psk" in text or "handshake" in text:
                return False, "invalid_key"
            return False, "cannot_connect"
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return True, (getattr(info, "mac_address", None) or host)

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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the stored credentials on demand.

        Re-authentication only fires *after* EnergyTrak rejects the stored
        refresh token, which leaves a gap: signing in again in the phone app
        can invalidate Home Assistant's token, and until the next poll fails
        there is no way to hand over a fresh link. This is the deliberate
        entry point — it also allows changing the account email, which reauth
        cannot, since reauth reuses the email already on the entry.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            client = EnergyTrakClient(async_get_clientsession(self.hass), email)
            try:
                await client.async_sign_in_with_magic_link(user_input[CONF_MAGIC_LINK])
            except EnergyTrakAuthError as err:
                errors["base"] = _auth_error_key(err)
            except EnergyTrakConnectionError:
                errors["base"] = "cannot_connect"
            except EnergyTrakError:
                _LOGGER.exception("Unexpected error during EnergyTrak reconfigure")
                errors["base"] = "unknown"
            else:
                # Signing in as a different account would silently point the
                # existing entities at someone else's generator, so the
                # account must match. A new entry is the way to add another.
                await self.async_set_unique_id(client.user_id or client.email)
                self._abort_if_unique_id_mismatch(reason="account_mismatch")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_EMAIL: email,
                        CONF_API_KEY: client.api_key,
                        CONF_REFRESH_TOKEN: client.refresh_token,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, {CONF_EMAIL: entry.data.get(CONF_EMAIL)}
            ),
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
