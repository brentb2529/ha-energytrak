"""Polling coordinator for EnergyTrak sites."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    EnergyTrakAuthError,
    EnergyTrakClient,
    EnergyTrakError,
)
from .const import (
    CONF_LOCAL_HOST,
    CONF_LOCAL_SITE_ID,
    CONF_SCAN_INTERVAL,
    LOCAL_SITE_PREFIX,
    CONF_SITE_IDS,
    CONF_SITE_NAMES,
    CONF_STALE_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_MINUTES,
    DEVICE_COLLECTION,
    DOMAIN,
    FRESHNESS_STORE_VERSION,
    SITE_COLLECTION,
)
from .normalize import EquipmentFreshness, extract_device_ids, normalize_site

_LOGGER = logging.getLogger(__name__)

# Values that a healthy unit is expected to move within an hour or so. If the
# whole tuple is byte-identical poll after poll, EnergyTrak itself is stuck —
# which is a different failure from "our polling stopped", and users need to
# be able to tell them apart.
_FRESHNESS_KEYS = (
    "clean_state_last_updated",
    "equipment_data_timestamp",
    "battery_voltage",
    "engine_hours",
    "status",
    "active",
    "status_color",
    "last_exercise_at",
)


@dataclass
class SiteRuntime:
    """Per-site cached state that survives a failed poll."""

    site_name: str | None = None
    device_ids: list[str] = field(default_factory=list)
    last_received_at: datetime | None = None
    last_changed_at: datetime | None = None
    signature: tuple[Any, ...] | None = field(default=None, repr=False)
    freshness: EquipmentFreshness = field(default_factory=EquipmentFreshness)
    # Scheduled-exercise start timestamps observed from the controller, newest
    # last. Only a handful are kept; two is enough to learn the interval.
    exercise_seen: list[str] = field(default_factory=list)


class EnergyTrakCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Fetch every configured site once per interval."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EnergyTrakClient | None,
        local: Any = None,
    ) -> None:
        """Initialise the coordinator.

        Either half may be absent. `client` is None on a bridge-only install
        (no EnergyTrak account at all); `local` is None on a cloud-only one,
        which is nearly every existing user. At least one must be present --
        async_setup_entry refuses the entry otherwise.
        """
        self.client = client
        # Optional LocalBridge. None on every cloud-only installation, which
        # is almost all of them.
        self.local = local
        self._local_was_active: bool | None = None
        # Unsubscribes the urgent-change hook. See async_start_urgent_updates.
        self._unsub_urgent: Any = None
        # Replays the bridge's flash event log. Built lazily on first use so a
        # cloud-only entry never imports the recorder statistics API.
        self._drain: Any = None
        # Sites that are actually fetched from EnergyTrak. Empty when there is
        # no account -- the polling loop then simply has nothing to do, rather
        # than needing a special case.
        self.cloud_site_ids: list[str] = list(entry.data.get(CONF_SITE_IDS, []))
        self.site_ids: list[str] = list(self.cloud_site_ids)

        # Must come AFTER site_ids exists -- this reads and appends to it.
        self.local_site_id: str | None = entry.data.get(CONF_LOCAL_SITE_ID)
        if local is not None:
            if self.local_site_id is None:
                # A bridge with no cloud site to attach to IS the device.
                self.local_site_id = (
                    self.cloud_site_ids[0]
                    if self.cloud_site_ids
                    else f"{LOCAL_SITE_PREFIX}:{entry.data.get(CONF_LOCAL_HOST)}"
                )
            if self.local_site_id not in self.site_ids:
                self.site_ids.append(self.local_site_id)
        site_names: dict[str, str] = entry.data.get(CONF_SITE_NAMES, {})
        self.sites: dict[str, SiteRuntime] = {
            site_id: SiteRuntime(site_name=site_names.get(site_id))
            for site_id in self.site_ids
        }

        # A prolonged vendor outage would otherwise log the same warning on
        # every poll forever, so only report when the failure changes.
        self._last_error: str | None = None
        self._warned_hour_sources: set[str] = set()

        # Liveness is established by watching the equipment block change
        # between polls, which on an idle generator can take until the next
        # weekly exercise. Losing that observation on every restart would
        # mean a week of falsely-stale readings, so it is persisted.
        self._store: Store[dict[str, Any]] = Store(
            hass, FRESHNESS_STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.freshness"
        )
        self._store_loaded = False

        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        self._stale_seconds = (
            int(entry.options.get(CONF_STALE_MINUTES, DEFAULT_STALE_MINUTES)) * 60
        )

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

    async def _async_load_freshness(self) -> None:
        """Restore what earlier runs learned about equipment liveness."""
        self._store_loaded = True
        stored = await self._store.async_load() or {}
        for site_id, record in (stored.get("sites") or {}).items():
            runtime = self.sites.setdefault(site_id, SiteRuntime())
            seen_raw = record.get("seen_at")
            seen_at: datetime | None = None
            if seen_raw:
                try:
                    seen_at = datetime.fromisoformat(seen_raw)
                except ValueError:
                    seen_at = None
            runtime.freshness = EquipmentFreshness(
                signature=record.get("signature"), seen_at=seen_at
            )
            # Observed SCHEDULED exercise starts, newest last. Persisted for the
            # same reason freshness is: the next observation may be a week away.
            runtime.exercise_seen = [
                str(x) for x in (record.get("exercise_seen") or [])
            ][-4:]

    async def _async_save_freshness(self) -> None:
        """Persist the current signature/observation for every site."""
        await self._store.async_save(
            {
                "sites": {
                    site_id: {
                        "signature": runtime.freshness.signature,
                        "seen_at": (
                            runtime.freshness.seen_at.isoformat()
                            if runtime.freshness.seen_at
                            else None
                        ),
                        "exercise_seen": runtime.exercise_seen,
                    }
                    for site_id, runtime in self.sites.items()
                    if runtime.freshness.signature or runtime.exercise_seen
                }
            }
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Poll every configured site."""
        if not self._store_loaded:
            await self._async_load_freshness()

        results: dict[str, dict[str, Any]] = dict(self.data or {})
        errors: list[str] = []
        freshness_changed = False

        for site_id in self.cloud_site_ids:
            before = self.sites.get(site_id, SiteRuntime()).freshness
            try:
                results[site_id] = await self._async_fetch_site(site_id)
                if self.sites[site_id].freshness != before:
                    freshness_changed = True
            except EnergyTrakAuthError as err:
                raise ConfigEntryAuthFailed(
                    f"EnergyTrak authentication failed: {err}"
                ) from err
            except EnergyTrakError as err:
                # Transient upstream blips are common. Keep serving the last
                # good payload for this site — `last_received_at` deliberately
                # does *not* advance, so the staleness sensors keep climbing
                # and tell the honest story.
                errors.append(f"{site_id}: {err}")
                _LOGGER.debug("Poll failed for site %s: %s", site_id, err)

        # Recover anything the bridge logged while we could not see it. Done
        # BEFORE the overlay so a fault that has since cleared is still filed
        # under the time it actually happened, rather than being invisible
        # because the current snapshot looks healthy.
        await self._async_drain_backlog()

        # Overlay the local bridge LAST, so it wins over the cloud for every
        # field it carries -- but only for those fields. The cloud still owns
        # serial number, location, subscription state, firmware status and the
        # next scheduled exercise date, none of which exist on the Modbus bus.
        # Merging rather than replacing is what keeps the device whole.
        self._apply_local(results)

        if errors and not results:
            raise UpdateFailed("; ".join(errors))
        if not results and self.client is None and self.local is not None:
            # Bridge-only and nothing to serve. Fail ONLY when the bridge is
            # unreachable.
            #
            # This used to fail whenever `snapshot()` returned None, which also
            # covers "connected, but the RS-485 bus is quiet" -- and that is
            # exactly backwards. A bridge with a dead bus is the case you most
            # need to SEE: refusing to set up hides the very diagnostics that
            # say the bus is dead, and leaves no device card at all. It cost a
            # setup_retry loop on a bridge that was answering perfectly.
            if not self.local.connected:
                raise UpdateFailed(f"local bridge at {self.local.host} is unreachable")

        summary = "; ".join(errors) if errors else None
        if summary and summary != self._last_error:
            _LOGGER.warning(
                "Some EnergyTrak sites failed to update, serving last known values: %s",
                summary,
            )
        elif summary:
            _LOGGER.debug("EnergyTrak sites still failing: %s", summary)
        elif self._last_error:
            _LOGGER.info("EnergyTrak polling recovered")
        self._last_error = summary

        # Rare by construction — the signature only moves when the vendor
        # actually pushes new equipment telemetry — so this is not a
        # per-poll write.
        if freshness_changed:
            await self._async_save_freshness()

        return results

    async def _async_drain_backlog(self) -> None:
        """Pull the bridge's event log into events and statistics."""
        if self.local is None or not self.local.connected:
            return
        if self._drain is None:
            from .backlog import BacklogDrain
            from .local_source import load_alarm_names

            # Reading a file blocks; Home Assistant will not tolerate that on
            # the event loop and raises rather than merely warning.
            names = await self.hass.async_add_executor_job(load_alarm_names)
            site_id = self.local_site_id or (
                self.cloud_site_ids[0] if self.cloud_site_ids else None
            )
            # An EXTERNAL statistic id: "<domain>:<object_id>". Deliberately
            # not tied to an entity -- binary sensors cannot carry statistics,
            # and the numeric alarm-count sensor does not exist while the bus
            # is down, which is precisely when this history matters.
            obj = "".join(
                ch if ch.isalnum() else "_" for ch in (site_id or "bridge")
            ).lower().strip("_")
            count_eid = f"{DOMAIN}:{obj}_recovered_alarms"
            self._drain = BacklogDrain(self.hass, self.local, names, count_eid)
        try:
            await self._drain.async_drain()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Backlog drain failed: %s", err)

    def _apply_local(self, results: dict[str, dict[str, Any]]) -> None:
        """Overlay live bridge readings onto one site's cloud payload."""
        if self.local is None:
            return

        site_id = self.local_site_id
        if site_id is None:
            return

        snapshot = self.local.snapshot()
        active = snapshot is not None

        # Connected but not trusted (bus quiet). With no cloud underneath there
        # would otherwise be no device at all, so publish identity and health
        # and let the bus diagnostics tell the story.
        if not active and self.client is None and self.local.connected:
            payload = results.setdefault(site_id, {})
            for field, value in self.local.device_identity().items():
                if value is not None:
                    payload.setdefault(field, value)
            payload["monitor_online"] = True
            payload["telemetry_source"] = "local"
            payload["local_bus_age_seconds"] = self.local.bus_age

        # Log the transition, not the state. A bridge that has been offline for
        # a month should not say so every thirty seconds.
        if active != self._local_was_active:
            if active:
                _LOGGER.info(
                    "Local B-Infohub bridge is now the source for site %s "
                    "(%d fields, cloud still polled underneath)",
                    site_id,
                    len(snapshot),
                )
            elif self._local_was_active is not None:
                _LOGGER.warning(
                    "Local B-Infohub bridge is no longer usable (connected=%s, "
                    "bus_healthy=%s); falling back to EnergyTrak cloud data",
                    self.local.connected,
                    self.local.bus_healthy,
                )
            self._local_was_active = active

        payload = results.get(site_id)
        if payload is None:
            # No cloud payload for this site -- either bridge-only, or the
            # account has never polled successfully.
            # Cloud has never succeeded for this site. Local alone is still a
            # working generator readout, so serve it rather than nothing.
            if active:
                payload = dict(snapshot)
                for field, value in self.local.device_identity().items():
                    if value is not None:
                        payload.setdefault(field, value)
                results[site_id] = payload
                self._derive_next_exercise(site_id, payload)
                self._stamp_bridge(payload)
            return

        if active:
            payload.update(snapshot)
            # Identity fills blanks only -- see LocalBridge.device_identity.
            for field, value in self.local.device_identity().items():
                if value is not None and payload.get(field) is None:
                    payload[field] = value
        elif self.client is not None:
            # Only claim "cloud" when there IS a cloud. On a bridge-only entry
            # the source is still local -- it is simply not delivering, which
            # is what bus_healthy and bus_age are there to say. Reporting
            # "cloud" for a site with no account was just wrong.
            payload["telemetry_source"] = "cloud"
            payload["local_bus_age_seconds"] = None

        if active:
            self._derive_next_exercise(site_id, payload)
        self._stamp_bridge(payload)

    @callback
    def async_start_urgent_updates(self) -> None:
        """Publish immediately when the bridge reports something urgent.

        WHY THIS IS NOT JUST A SHORTER scan_interval.

        The bridge already delivers a utility failure or a start within
        milliseconds -- subscribe_states is push. Everything after that was
        Home Assistant's own doing: the value landed in LocalBridge._values
        and waited for the next scheduled refresh. On the default 30s interval
        that is up to 30 seconds of invented delay on the outage the whole
        system exists to notice, and it looks exactly like the monitoring
        failed.

        Dropping scan_interval instead would poll EnergyTrak's API 30x harder
        for the same benefit, and still leave a worst case of one interval.
        This removes the wait entirely for the readings that matter and leaves
        every other reading on its existing cadence.

        async_set_updated_data, not async_request_refresh: the local snapshot
        is already current, so there is nothing to fetch. Going out to the
        cloud here would put a network round trip -- and its retries and
        timeouts -- directly in the path of an alarm.
        """
        if self.local is None:
            return
        self._unsub_urgent = self.local.async_add_urgent_listener(
            self._async_publish_urgent
        )

    @callback
    def _async_publish_urgent(self) -> None:
        """Re-overlay the bridge onto the last payload and publish at once."""
        if self.data is None:
            # Nothing has been fetched yet; the first regular refresh is
            # imminent and will carry these values anyway.
            return
        results = {site: dict(payload) for site, payload in self.data.items()}
        self._apply_local(results)
        _LOGGER.debug("Urgent bridge change; publishing without waiting for poll")
        self.async_set_updated_data(results)

    @callback
    def async_stop_urgent_updates(self) -> None:
        """Detach the urgent hook on unload."""
        if self._unsub_urgent is not None:
            self._unsub_urgent()
            self._unsub_urgent = None


    def _derive_next_exercise(self, site_id: str, payload: dict[str, Any]) -> None:
        """Compute the next scheduled exercise from the controller's own record.

        THE CLOUD'S VALUE IS NOT SCHEDULE DATA. EnergyTrak publishes
        `nextExerciseDue` as "the last time the set ran, plus an interval",
        which is wrong the moment anyone runs the generator by hand. Measured
        on a live system: a manual start at 12:51 EDT moved the cloud's next
        exercise from Sunday to Sunday-plus-two-hours, while the machine's
        actual weekly slot never moved.

        The controller is better behaved. It stamps `last_exercise` ONLY for a
        SCHEDULED run -- the same manual start left it untouched at Saturday
        07:23 -- so the bridge is already reporting the one timestamp that
        tracks the real schedule. The schedule itself is not readable: it lives
        in the controller's parameter memory, genmon never found it, the vendor
        manual publishes no register map, and a full FC03 sweep of 0x0001-0x00FF
        returned silence.

        So it is learned by watching instead. Each new scheduled start is
        recorded; the gap between consecutive ones IS the interval, whether the
        installer set weekly or monthly. Until a second one has been seen the
        default is weekly, which is the factory setting and the common case.
        """
        raw = payload.get("last_exercise_at")
        if not raw:
            return
        runtime = self.sites.setdefault(site_id, SiteRuntime())
        stamp = str(raw)
        if stamp not in runtime.exercise_seen:
            runtime.exercise_seen = (runtime.exercise_seen + [stamp])[-4:]
            _LOGGER.info(
                "Recorded a scheduled exercise for site %s at %s (%d observed)",
                site_id,
                stamp,
                len(runtime.exercise_seen),
            )

        try:
            last = datetime.fromisoformat(stamp)
        except ValueError:
            return

        interval = timedelta(days=7)
        if len(runtime.exercise_seen) >= 2:
            try:
                prev = datetime.fromisoformat(runtime.exercise_seen[-2])
                gap = last - prev
                # Guard against a clock jump or a re-stamped value producing a
                # nonsense interval; anything outside a day to ~5 weeks is not
                # a schedule this controller can be set to.
                if timedelta(hours=20) <= gap <= timedelta(days=36):
                    interval = gap
            except ValueError:
                pass

        payload["next_exercise_due"] = (last + interval).isoformat()
        payload["exercise_interval_days"] = round(
            interval.total_seconds() / 86400, 2
        )

    def _stamp_bridge(self, payload: dict[str, Any]) -> None:
        """Record WHICH bridge this entry watches, in every code path.

        "local" on its own does not answer the question you actually ask when a
        reading looks wrong, which is *which box produced this*. With more than
        one bridge on a site, or after one has been swapped, a source label with
        no address is not traceable to hardware.

        Stamped even while the source is cloud, and even while the bridge is
        unreachable: "the bridge we are NOT using is at 192.168.1.62" is exactly
        what you need in order to go and look at it. A field that disappears in
        the failure case is useless for diagnosing the failure case.

        Called from both exits of _apply_local -- the bridge-only path returns
        early, and that is the install shape with no cloud fallback, so it is
        the one that can least afford to be missing this.
        """
        if self.local is None:
            return
        payload["local_bridge_host"] = self.local.host
        payload["local_bridge_port"] = self.local.port
        payload["local_bridge_connected"] = self.local.connected
        payload["local_bus_healthy"] = self.local.bus_healthy
        # Seconds since the bridge last SAID anything, as opposed to seconds
        # since the generator last answered the bridge. The two fail
        # independently and only this one catches a bridge that has gone quiet
        # while holding its socket open.
        payload["local_rx_age_seconds"] = self.local.rx_age

    async def _async_fetch_site(self, site_id: str) -> dict[str, Any]:
        """Read one site and all of its devices, normalised into one payload."""
        runtime = self.sites.setdefault(site_id, SiteRuntime())

        # The site document is read every poll, not just to discover devices:
        # it carries the exercise history, which is the only record that the
        # generator ran when the equipment feed has gone dormant.
        site_doc = await self.client.async_get_document(SITE_COLLECTION, site_id)
        device_ids = extract_device_ids(site_doc) or runtime.device_ids
        if not device_ids:
            raise EnergyTrakError(f"no device linked to site {site_id}")
        runtime.device_ids = device_ids

        # A site's devices hold differently-aged copies of the telemetry, so
        # fetch them all and let normalize_site pick the best source per field.
        device_docs = await asyncio.gather(
            *(
                self.client.async_get_document(DEVICE_COLLECTION, device_id)
                for device_id in device_ids
            )
        )
        data = normalize_site(
            site_id,
            runtime.site_name,
            list(device_docs),
            stale_threshold_seconds=self._stale_seconds,
            site_doc=site_doc,
            freshness=runtime.freshness,
        )
        runtime.freshness = EquipmentFreshness(
            signature=data.get("equipment_signature"),
            seen_at=data.get("equipment_content_seen_at"),
        )

        # A unit whose runtime sources disagree once converted is telling us a
        # conversion factor is wrong for its firmware. Say so in the log
        # rather than only in diagnostics — nobody pulls diagnostics for a
        # number that merely looks a bit off. Once per site per run.
        ratio = data.get("engine_hours_disagreement_ratio")
        if ratio and site_id not in self._warned_hour_sources:
            self._warned_hour_sources.add(site_id)
            _LOGGER.warning(
                "Engine-hour sources disagree by %sx for site %s (%s). One of these "
                "fields is not in the unit this integration assumes; a ratio near "
                "1.9 means the 111-second runtime counter was read as minutes, near "
                "3.2 as tenths of an hour, near 32 as hours, near 3600 means "
                "seconds. Please report this with your diagnostics: %s",
                ratio,
                site_id,
                data.get("engine_hours_source"),
                data.get("engine_hours_candidates_hours"),
            )

        now = datetime.now(UTC)
        signature = tuple(str(data.get(key)) for key in _FRESHNESS_KEYS)
        runtime.last_received_at = now
        if runtime.signature != signature:
            runtime.signature = signature
            runtime.last_changed_at = now

        data["last_received_at"] = runtime.last_received_at
        data["last_changed_at"] = runtime.last_changed_at
        return data
