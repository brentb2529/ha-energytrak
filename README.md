# EnergyTrak for Home Assistant

Home Assistant integration for **EnergyTrak**-connected standby generators
(Briggs & Stratton / genmon cellular monitors). Everything runs inside Home
Assistant — no extra hardware or bridge service.

> [!WARNING]
> **Unofficial, unsupported, and liable to break without notice.**
>
> This project is not affiliated with, endorsed by, or supported by Briggs &
> Stratton, EnergyTrak, or any related company. It works by talking to the same
> private backend their mobile app uses. That backend is undocumented and can
> change, restrict access, or disappear at any time — if it does, this
> integration stops working and there is nothing it can do about it.
>
> **Do not rely on it for anything safety-critical.** A standby generator is
> life-safety equipment in some homes. Treat what you see here as a
> convenience, never as a substitute for the manufacturer's own monitoring,
> alerts, or maintenance schedule. Some units never report parts of the
> telemetry at all — see [About staleness](#about-staleness).
>
> Provided as-is under the MIT licence, with no warranty of any kind.

## Installation (HACS)

This is a **custom repository**. It will not appear in HACS search until you
add it — HACS only searches repositories it already knows about, and inclusion
in the default list is a separate submission to
[hacs/default](https://github.com/hacs/default).

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/brentb2529/ha-energytrak`, category **Integration**.
2. It now appears in HACS search under **EnergyTrak** or **Briggs and
   Stratton**. Install it, then restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → EnergyTrak**.

Manual install: copy `custom_components/energytrak` into your HA
`config/custom_components/` directory and restart.

## Setup

EnergyTrak has no password login — it uses Firebase email magic links.

1. In the EnergyTrak app or website, request a sign-in link for your account.
2. Open the email, **copy the link address** of the sign-in button. Do **not**
   click it: the link is single-use, and opening it in a browser consumes it.
3. Paste the link into the config flow along with your email address.

The link itself is only used once. What gets stored is the long-lived Firebase
refresh token, which the integration uses to mint short-lived access tokens
from then on. If that token is ever revoked, Home Assistant raises a
re-authentication prompt and you paste a fresh link.

### Replacing the sign-in later

**Settings → Devices & Services → EnergyTrak → ⋮ → Reconfigure** takes a fresh
magic link at any time. Use it whenever you sign in to EnergyTrak elsewhere,
in case that invalidates the token Home Assistant holds — you do not have to
wait for polling to start failing, and you do not have to delete and re-add
the integration (which would lose your entity ids, history and automations).

Home Assistant also prompts for a new link on its own if the stored token is
ever rejected. That path reuses the email already on the entry; Reconfigure is
the one that can change it. Signing in as a *different* EnergyTrak account is
refused, since it would silently repoint your existing entities at someone
else's generator — add a second integration entry for that instead.

The setup flow then lists the sites your account can see. If the list is empty
(some accounts cannot enumerate the site collection), enter site IDs manually
as a comma-separated list.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| Polling interval | 30 s | How often each site is read. |
| Staleness threshold | 15 min | How old the last full equipment upload may be before live readings are suppressed. |

## Entities

One device per site. **Entities are only created for fields your generator
actually reports.** Not every unit sends every field — some controllers never
upload equipment telemetry at all — and a row of permanently unknown values
looks like a broken integration rather than an absent feature. A field earns
its entity by being reported at least once, and the check repeats on every
refresh, so if a dormant feed wakes up the entities appear on their own.

The diagnostics that explain *why* data is missing (Status, Running, Fault,
Equipment data age / stale, Last received, Last changed) are always created —
they have to exist precisely when everything else does not.

Created when reported:

| Entity | Notes |
| --- | --- |
| Battery voltage, Engine hours | Engine hours is `total_increasing`, so it works in statistics. |
| Load power, Output voltage, Output frequency, Engine speed | Generator output — see staleness handling below. |
| Grid voltage, Grid frequency, Grid status | Utility side. |
| Status, Operation mode, Fault condition | Controller state. |
| Active alarms | Count, with `active_alarms` and `alarm_flags` attributes. |
| Starts, Trips | Cumulative counters. |
| Running, Grid power, Fault | Binary sensors. |
| Equipment data age / stale, Last received, Last changed | Diagnostics — see below. |

Created but disabled by default (enable per entity): per-phase voltages,
currents, apparent/reactive power, power factor, fuel type, ignition, health,
utility monitor string, network strength, monitor state, firmware update
status.

## Dashboard card

The integration ships generator artwork and hangs it off the **Running** binary
sensor as `entity_picture`, so it changes with state on its own — idle, running
(amber halo, pulsing green lamp), fault (red), unavailable. No custom frontend
resource and nothing to install.

Simplest card:

```yaml
type: picture-entity
entity: binary_sensor.<your_generator>_running
name: Generator
show_state: true
```

Closer to a full status tile — artwork with live readings overlaid:

```yaml
type: picture-elements
image_entity: binary_sensor.<your_generator>_running
elements:
  - type: state-label
    entity: sensor.<your_generator>_status
    style: {top: 12%, left: 50%, font-size: 15px, font-weight: 600,
            text-transform: capitalize, text-shadow: 0 1px 3px rgba(0,0,0,.8)}
  - type: state-label
    entity: sensor.<your_generator>_battery_voltage
    prefix: "Batt "
    style: {top: 88%, left: 20%, font-size: 12px,
            text-shadow: 0 1px 3px rgba(0,0,0,.8)}
  - type: state-label
    entity: sensor.<your_generator>_engine_hours
    prefix: "Hrs "
    style: {top: 88%, left: 50%, font-size: 12px,
            text-shadow: 0 1px 3px rgba(0,0,0,.8)}
  - type: state-label
    entity: sensor.<your_generator>_grid_voltage
    prefix: "Grid "
    style: {top: 88%, left: 80%, font-size: 12px,
            text-shadow: 0 1px 3px rgba(0,0,0,.8)}
  - type: state-icon
    entity: binary_sensor.<your_generator>_grid_power
    style: {top: 12%, left: 88%, "--mdc-icon-size": 20px}
```

Pair it with a plain entities card for the exercise history:

```yaml
type: entities
title: Generator
entities:
  - entity: sensor.<your_generator>_last_exercise
  - entity: sensor.<your_generator>_last_exercise_duration
  - entity: sensor.<your_generator>_next_exercise
  - entity: binary_sensor.<your_generator>_fault
  - entity: sensor.<your_generator>_active_alarms
```

The artwork lives in `custom_components/energytrak/images/` and is served at
`/api/energytrak/static/generator_<state>.svg`, so you can reference a specific
variant directly if you would rather not track state.

## About the multiple devices per site

A site links to several device documents — typically `<id>-generator`,
`<id>-grid` and the genmon monitor. They carry overlapping but **differently
aged** copies of the same telemetry. On a real unit the monitor's snapshot was
190 days fresher than the generator's, with a higher engine-hour and start
count, while the grid device published a live utility voltage on its heartbeat
rather than a months-old one.

Reading only the first device (as the old Node service did) therefore reports
stale counters. This integration fetches them all and merges by role:

| Source | Used for |
| --- | --- |
| generator | the heartbeat: run state, battery, health, smart mode, fault flag |
| grid | live utility voltage and utility-present flag |
| monitor | connectivity, firmware, the live `triggeredFaults` alarm list |
| whichever has the newest snapshot | all equipment telemetry and counters |

The `Equipment data age` sensor carries an `equipment_source` attribute naming
the device the snapshot came from, plus a `devices` list.

## Exercise history

The weekly test run is recorded on the **site** document, not on any device —
so it survives a dormant equipment feed. On a real unit whose
`EquipmentEventData` had not updated in three months, the site document still
correctly showed a 20-minute exercise four days earlier.

| Entity | Source |
| --- | --- |
| Last exercise | `exercise.lastActivity.finishedAt` |
| Last exercise duration | `exercise.lastActivity.duration` (ms, shown in minutes) |
| Next exercise due | `exercise.exerciseNotifications.nextExerciseDue` |
| Malfunction | `malfunction.hasMalfunction`, raised by EnergyTrak's outage manager |

If RPM and output voltage sit at zero but Last exercise is recent, the
generator is fine — it is the vendor's equipment telemetry that has stopped,
not the unit.

## About staleness

EnergyTrak feeds two independent pipelines into the same document:

- **cleanState** — a frequent heartbeat: battery, engine hours, running state.
- **rawState.Event.EquipmentEventData** — the *full* equipment telemetry (RPM,
  output voltage, frequency, per-phase load). Cellular units only upload this
  on state changes and periodic check-ins, so it can be hours or months old.

Trusting the second one blindly paints false zeros. The integration instead:

- **Generator output fields** (RPM, output voltage/frequency, load, per-phase):
  report `0` when the unit is known to be off *and* the snapshot is less than
  a day old — a recent stale zero is the correct current value. Report
  *nothing* when the unit is running but the snapshot is stale, and nothing
  once the snapshot passes a day old, whatever the run state.

  That last bound matters: some units never upload equipment telemetry at all.
  One real generator produced **zero** non-zero readings across 450,000 polls
  spanning five months and twenty-odd exercise runs. Synthesising a confident
  `0 rpm` from a nine-month-old snapshot dresses an absence of data up as a
  measurement — so past a day, these entities go unknown and the gap is
  visible. If yours sit at unknown permanently, disable them; the vendor is
  not sending that data and never will without a hardware fix.
- **Utility fields** (grid voltage/frequency): pass through at any age — the
  grid is ~240 V / 60 Hz whenever it is present, so a stale-but-plausible
  number beats an empty gauge. Whether the utility is actually there *right
  now* is answered separately, and freshly, by the `Grid power` binary sensor,
  and `Equipment data age` tells you how old the number is.
- **Counters and engine hours**: always reported. They stay meaningful at any age.

Note that "the unit is off, so 0 is correct" only applies to fields the
controller actually sends. Some units transmit no equipment block at all, and
returning 0 for each missing field there would invent a dozen measurements —
which would then become a dozen entities, since entities are created for any
field with a value. An absent reading stays absent.

### Engine hours and units

Engine hours comes from the first of four candidate fields that carries a
value, and **they are not all in the same unit despite the naming**.
`EquipmentEventData.EngineHours` is hours; `DeviceEventData.EngineHoursTP` is
minutes — it read `1623` on a generator commissioned five days earlier, where
1,623 hours is impossible by a factor of thirteen and 1,623 minutes (27.05 h)
is not. The conversion is attached to the field, never inferred from the size
of the number: a generator retrofitted with a monitor legitimately carries
hours that dwarf its EnergyTrak commissioning date.

Nothing in the payload observed so far declares its own units, so the field
name is the only signal — which is exactly why the mistake was possible. Two
independent checks now catch a wrong factor without anyone having to know what
the vendor's app displays:

- **Sources cross-check each other.** When a controller populates more than
  one runtime field, they measure the same quantity and must agree once
  converted. A disagreement means a factor is wrong for that firmware, and the
  ratio names the mistake — near 60 is minutes read as hours, near 3600 is
  seconds. This is logged as a warning, not buried in diagnostics, since
  nobody pulls diagnostics for a number that merely looks a bit off. Ordinary
  skew between snapshots taken moments apart is tolerated.
- **The commissioning-age guard** covers the single-source case, where there
  is nothing to compare against.

Diagnostics now also include the raw `EquipmentEventData`, `DeviceEventData`
and `MessageEventData` blocks verbatim (identity keys redacted), so field
semantics can be settled from real payloads instead of inferred from names.

The `Engine hours` sensor exposes `source_field` so you can see which one won,
and `exceeds_time_since_commissioning`, which flags a reading larger than the
unit's own age. That flag informs rather than corrects — retrofits trip it
honestly — but on a new install it is the signature of a source whose unit is
being read wrong. `Download diagnostics` includes `counter_sources`, listing
every candidate's raw value side by side.

### The controller sends several timestamps, and they disagree

`MessageEventData` carries more than one upload time, and they do **not**
agree. On a real unit `ActualDateUTC` and `Created` both sat at the original
message months in the past, while `ActualDate` tracked the live upload to the
second. Reading them as a first-match priority list picked a three-month-old
value and declared minutes-old telemetry ancient — which, because the
output-field gate keys off that age, suppressed every RPM and voltage reading
the unit produced.

The newest parseable timestamp wins, so whichever field a given firmware keeps
current is the one used, with no assumption about which that is.

As a second line of defence for a unit where *every* timestamp is stuck, the
snapshot's age is also treated as a *lower* bound. The integration
also watches whether the block's contents change between polls, and measures
age from whichever evidence is more recent. Two properties matter:

- A **changed** payload proves the block is live. An **unchanged** one proves
  nothing — an idle generator repeats itself for days — so it never counts as
  evidence of freshness, and a genuinely dormant feed still goes stale.
- A first sighting establishes nothing, since there is no earlier payload to
  compare against. Freshness requires observing a *transition*.

The observation is persisted, because on a quiet generator the next change may
not arrive until the following week's exercise, and losing it on every restart
would mean days of falsely-stale readings.

Until that transition is seen, the integration does not guess. On a snapshot
older than any plausible check-in that it has never corroborated, "the feed
died" and "the clock field is stuck" are indistinguishable — so **Equipment
data stale** reports `unknown` rather than raising a Problem, and the age goes
unknown with it. Raising an alarm on a generator that is visibly delivering
data would be a confident wrong answer, which is the failure this whole
section exists to avoid. Readings stay suppressed throughout, so nothing is
published as current that isn't.

A controller whose clock works is unaffected: a plausible timestamp is
believed on its own, with no waiting period. `freshness_basis` on both
entities says which of the three cases you are in — `observed`, `reported` or
`unknown`.

When the reported and observed timestamps disagree, the `Equipment data age`
sensor says so:
`reported_timestamp` is the controller's own claim, `content_last_seen` is when
the data was actually observed moving, and `reported_timestamp_unreliable`
flags the mismatch. That distinction is the difference between "your generator
stopped reporting" and "your generator is fine, its clock field is stuck".

Three diagnostic entities let you tell the failure modes apart:

- **Last received** — our poll succeeded. Stops advancing if HA loses network.
- **Last changed** — EnergyTrak's payload actually moved. Stops advancing if
  the vendor is serving an identical payload over and over, which otherwise
  looks perfectly healthy.
- **Equipment data age / stale** — how old the full telemetry snapshot is.

## Example automation

```yaml
automation:
  - alias: Generator started on utility loss
    triggers:
      - trigger: state
        entity_id: binary_sensor.generator_running
        to: "on"
    conditions:
      - condition: state
        entity_id: binary_sensor.generator_grid_power
        state: "off"
    actions:
      - action: notify.mobile_app
        data:
          message: >-
            Generator started — {{ states('sensor.generator_load_power') }} W load,
            {{ states('sensor.generator_battery_voltage') }} V battery.
```

## Consuming these entities from another app

Every entity carries two attributes so an external consumer can find and
identify them without guessing:

| Attribute | Meaning |
| --- | --- |
| `energytrak_site` | the site id, so multiple generators stay separate |
| `energytrak_field` | the stable field key, e.g. `battery_voltage` |

Home Assistant's REST `/api/states` exposes no integration, device or registry
information, and both friendly names and entity ids move when a device is
renamed or assigned to an area — so neither is safe to key on. These two are
stable for the life of the entity.

## Icon

`custom_components/energytrak/brand/icon.png` (256×256, plus a 512×512 `@2x`)
is a generic generator mark drawn for this project — an enclosure with a power
bolt. It is **not** EnergyTrak's official logo, and is not intended to
represent the company's branding. If you would rather ship the real mark,
replace those two files with the official asset; note that the brand belongs
to its owner, so check you have the right to redistribute it.

## Disclaimer

Not affiliated with, endorsed by, or supported by Briggs & Stratton, EnergyTrak,
or any related company. All trademarks belong to their respective owners.

This integration reads an undocumented private backend — the one the vendor's
own mobile app uses. There is no agreement or guarantee behind it. The vendor
may change, restrict, or withdraw that access at any time, without notice and
without recourse, and the integration will stop working when they do.

The software is provided "as is", without warranty of any kind, express or
implied. You use it at your own risk, and you are responsible for verifying
anything that matters against the manufacturer's own tools. Do not use it as
the sole means of monitoring life-safety equipment.
