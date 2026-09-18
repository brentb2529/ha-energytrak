"""Replay the bridge's flash event log into Home Assistant history.

WHAT THIS RECOVERS

When the bridge is unreachable, ESPHome drops state -- the API carries the
current value and nothing else. For a numeric sensor that costs resolution.
For an ALARM it costs the only evidence: a shutdown that asserts and clears
during an outage leaves no trace anywhere.

The bridge writes every alarm transition to flash the moment it happens. This
module pulls those records back out and puts them where they can be seen.

TWO DESTINATIONS, BECAUSE NEITHER ALONE IS ENOUGH

  Events   Each transition is fired as a Home Assistant event carrying its
           ORIGINAL timestamp. Automations and notifications can act on it,
           and nothing is aggregated away.

  Statistics
           `active_alarm_count` is imported as hourly statistics via
           async_import_statistics, so the history graph shows that something
           was wrong during the gap rather than a flat line.

WHY NOT JUST BACKFILL THE BINARY SENSORS

Because Home Assistant cannot. Backdated state is only accepted through the
statistics API, and statistics require a `state_class` -- which binary_sensor
does not have and cannot have. A numeric proxy is the only route, which is why
the count carries the timeline and the events carry the detail.

RESOLUTION, HONESTLY

async_import_statistics is hourly. So the graph says "an alarm was active
during this hour", not "at 14:32:07". The fired events keep the exact time; the
statistics exist to make the gap visible at a glance.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Matches components/backlog/backlog.h
EVENT_ALARM_SET = 1
EVENT_ALARM_CLEAR = 2
EVENT_BOOT = 3
EVENT_BUS_LOST = 4
EVENT_BUS_BACK = 5
EVENT_LINK_LOST = 6
EVENT_LINK_BACK = 7

TYPE_NAMES = {
    EVENT_ALARM_SET: "alarm_set",
    EVENT_ALARM_CLEAR: "alarm_clear",
    EVENT_BOOT: "boot",
    EVENT_BUS_LOST: "bus_lost",
    EVENT_BUS_BACK: "bus_restored",
    EVENT_LINK_LOST: "link_lost",
    EVENT_LINK_BACK: "link_restored",
}

HA_EVENT = f"{DOMAIN}_backlog"

# EXTERNAL statistics, not entity statistics.
#
# async_import_statistics attaches history to an existing entity, which is the
# wrong tool here: the recovered alarm timeline has no live entity to hang off.
# `active_alarm_count` only exists once the bus reports it, and on a bridge
# whose RS-485 is down -- the exact case this feature exists for -- it does not
# exist at all. async_add_external_statistics needs only a `domain:object_id`
# identifier, so the recovered history can be filed whether or not the sensor
# is currently alive.

# Stop after this many rounds in one pass. A full 4096-record ring at 8 per
# batch would otherwise monopolise a coordinator update; the rest simply drains
# on the next one.
MAX_ROUNDS = 24


def parse_batch(raw: str) -> list[dict[str, int]]:
    """Decode `seq:ts:type:index|...` into records.

    Tolerant by design: a malformed field means one lost event, and dropping
    the whole batch over it would stall the cursor forever on a record that
    will never parse.
    """
    out: list[dict[str, int]] = []
    if not raw:
        return out
    for chunk in raw.split("|"):
        parts = chunk.split(":")
        if len(parts) != 4:
            continue
        try:
            seq, ts, typ, idx = (int(p) for p in parts)
        except ValueError:
            continue
        out.append({"seq": seq, "ts": ts, "type": typ, "index": idx})
    return out


class BacklogDrain:
    """Pulls the bridge's event log across and files it in two places."""

    def __init__(
        self,
        hass: HomeAssistant,
        bridge: Any,
        alarm_names: list[str],
        count_entity_id: str | None,
    ) -> None:
        """Prepare a drain; no I/O until async_drain."""
        self.hass = hass
        self.bridge = bridge
        self.alarm_names = alarm_names
        self.count_entity_id = count_entity_id
        # Alarms believed active, rebuilt as the log replays.
        self._active: set[int] = set()
        self._seen_any = False

    async def async_drain(self) -> int:
        """Pull everything pending. Returns how many records were handled."""
        if self.bridge is None or not self.bridge.connected:
            return 0

        handled = 0
        buckets: dict[datetime, list[int]] = {}

        for _ in range(MAX_ROUNDS):
            raw = self.bridge.raw("backlog_batch")
            records = parse_batch(raw if isinstance(raw, str) else "")
            if not records:
                break

            last_seq = 0
            for rec in records:
                self._handle(rec, buckets)
                last_seq = max(last_seq, rec["seq"])
                handled += 1

            # Acknowledge only after the events are fired and bucketed. If this
            # call fails the bridge keeps them and we replay next time --
            # duplicates are harmless, silence is not.
            if not await self.bridge.async_call("backlog_ack", seq=last_seq):
                _LOGGER.debug("Bridge did not accept backlog_ack; will retry")
                break

        if buckets:
            self._import_statistics(buckets)
        if handled:
            _LOGGER.info("Replayed %d record(s) from the bridge event log", handled)
        return handled

    def _handle(self, rec: dict[str, int], buckets: dict[datetime, list[int]]) -> None:
        typ, idx, ts = rec["type"], rec["index"], rec["ts"]
        name = (
            self.alarm_names[idx]
            if typ in (EVENT_ALARM_SET, EVENT_ALARM_CLEAR) and idx < len(self.alarm_names)
            else None
        )

        # ts == 0 means the record predates a clock sync. The event is still
        # worth firing -- "this happened, time unknown" beats discarding it --
        # but it must not be bucketed, or it lands in 1970.
        when = datetime.fromtimestamp(ts, UTC) if ts else None

        self.hass.bus.async_fire(
            HA_EVENT,
            {
                "seq": rec["seq"],
                "type": TYPE_NAMES.get(typ, f"type_{typ}"),
                "alarm": name,
                "timestamp": when.isoformat() if when else None,
                "time_known": when is not None,
            },
        )

        if typ == EVENT_ALARM_SET:
            self._active.add(idx)
            self._seen_any = True
        elif typ == EVENT_ALARM_CLEAR:
            self._active.discard(idx)
            self._seen_any = True
        elif typ == EVENT_BOOT:
            # A reboot does not clear a latched fault on the controller, but we
            # cannot know what is still asserted until the bus is read again.
            # Reset rather than assert something that may have gone away.
            self._active.clear()

        if when is not None and self._seen_any:
            hour = when.replace(minute=0, second=0, microsecond=0)
            buckets.setdefault(hour, []).append(len(self._active))

    def _import_statistics(self, buckets: dict[datetime, list[int]]) -> None:
        """File the alarm-count timeline as hourly statistics."""
        if not self.count_entity_id:
            return
        stats: list[StatisticData] = []
        for hour in sorted(buckets):
            samples = buckets[hour]
            stats.append(
                StatisticData(
                    start=hour,
                    mean=sum(samples) / len(samples),
                    min=min(samples),
                    max=max(samples),
                )
            )
        metadata = StatisticMetaData(
            # ARITHMETIC because we supply `mean`. Omitting mean_type relies on
            # the deprecated has_mean path, which Home Assistant warns about
            # and removes in 2026.11 -- and in the meantime the import is
            # rejected, so the statistics silently never appear.
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name="Active alarms (recovered)",
            source=DOMAIN,
            statistic_id=self.count_entity_id,
            unit_class=None,
            unit_of_measurement=None,
        )
        try:
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug("Imported %d hourly alarm buckets", len(stats))
        except Exception as err:  # noqa: BLE001
            # A statistics failure must not stop the events being delivered --
            # those are the part that cannot be reconstructed later.
            _LOGGER.warning("Could not import recovered alarm statistics: %s", err)
