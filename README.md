# EnergyTrak for Home Assistant

Home Assistant integration for **EnergyTrak**-connected standby generators
(Briggs & Stratton / genmon cellular monitors). Replaces the standalone Node
wrapper service — everything runs inside Home Assistant, no extra Pi required.

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/brentb2529/ha-energytrak` as an **Integration**.
2. Install **EnergyTrak**, then restart Home Assistant.
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

The setup flow then lists the sites your account can see. If the list is empty
(some accounts cannot enumerate the site collection), enter site IDs manually
as a comma-separated list.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| Polling interval | 30 s | How often each site is read. |
| Staleness threshold | 15 min | How old the last full equipment upload may be before live readings are suppressed. |

## Entities

One device per site. Enabled by default:

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

Disabled by default (enable per entity if your unit reports them): per-phase
voltages, currents, apparent/reactive power, power factor, fuel type,
ignition, health, utility monitor string.

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

## Disclaimer

Not affiliated with or endorsed by EnergyTrak or Briggs & Stratton. It talks to
the same private backend the mobile app uses; that backend can change without
notice.
