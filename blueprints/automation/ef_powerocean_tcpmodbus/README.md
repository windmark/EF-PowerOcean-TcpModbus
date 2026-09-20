# Blueprints

Click the import button for the blueprint you want. It opens the import dialog in your own Home
Assistant, no file copying needed.

Manual alternative: copy the `.yaml` to `config/blueprints/automation/ef_powerocean_tcpmodbus/`,
then **Settings → Automations → Blueprints → Create automation**.

---

## Delayed battery charging

`delayed_charging.yaml`

[![Open your Home Assistant instance and show the blueprint import dialog with a specific blueprint pre-filled.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fwindmark%2FEF-PowerOcean-TcpModbus%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fef_powerocean_tcpmodbus%2Fdelayed_charging.yaml)

If your panels can out-produce your feed-in limit, the inverter curtails them around midday and
that energy is never generated. A battery can absorb it — it sits on the DC side of the export
limit — but only if it still has room when the ceiling is reached. Left alone, it fills from the
first surplus of the day and is full hours before it matters.

This holds the battery back in the morning and releases it when clipping actually starts.

It writes one entity, the Charge Limit guard, and never changes the mode. The inverter stays in
Automatic doing self-consumption and MPPT.

### Setup

Import it with the button above, then pick three entities: **Battery Mode**, the **Charge Limit** number on the same device, and **Peak
Forecast Today** from [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar).

Set the **Battery Reserve** guard yourself in the UI first. Modbus control ignores the Min SOC
Limit from the EcoFlow app, so without it you have no floor. The blueprint does not write it —
it is a static preference, not something to recompute.

### How it decides

Every 15 minutes, and whenever export sits at the ceiling:

| Condition | Charge limit |
|-----------|--------------|
| Forecast peak × margin below the cap | 100% — today cannot clip |
| Clipping measured right now | 100% — take everything |
| Past the release time, or sun down | 100% — still fill by evening |
| Less than 2 kWh of room left | 100% — nothing worth protecting |
| Released under 45 minutes ago | 100% — ride out cloud gaps |
| Otherwise | current SoC — hold |

Written only when it moves 4 points or more. On a broken-cloud day that works out at two writes:
hold in the morning, release when the ceiling is hit.

### Settings

| Setting | Default | Notes |
|---------|---------|-------|
| Peak margin | 1.3 | See below |
| Release before sunset | 3 h | Safety net for a wrong forecast |
| Clipping must persist for | 3 min | Keep short — broken cloud dips below the ceiling constantly |
| Minimum time held open | 45 min | Stops a cloud gap re-holding the battery mid-burst |
| Stop protecting below | 2 kWh | Avoids writes chasing the last fraction |
| Write deadband | 4 | Each write resets the guard's 5% hysteresis latch |

#### Peak margin

Not a fudge factor for a bad forecast. Solcast reports half-hourly **means**, and a true
instantaneous peak is always above the average containing it. On broken-cloud days output swings
several kW inside one period, and cloud-edge enhancement adds more. One site measured a forecast
peak of 7 950 W against an actual above 9 850 W.

Bias it high. A false positive costs nothing — the battery waits, nothing clips, the release time
arrives and it still fills. A false negative costs the whole day's clipping.

### Is it worth running?

Solcast forecasts what the array *could* produce; the yield sensor records what it *did*. On a
clipping day the gap is mostly curtailment:

```yaml
template:
  - sensor:
      - name: Clipping Loss Today
        unit_of_measurement: kWh
        state: >
          {% set fc = states('sensor.solcast_pv_forecast_forecast_today') | float(0) %}
          {% set act = states('sensor.ecoflow_powerocean_solar_yield_today') | float(0) %}
          {% if is_state('sun.sun','below_horizon') and fc > 0 %}
            {{ [fc - act, 0] | max | round(2) }}
          {% else %}
            unknown
          {% endif %}
```

Read it after sunset. Single days are noisy — Solcast's own error lives in that gap — so compare
days the battery was held against days it was not, over weeks.

### When it will not help

If your feed-in limit is an app setting rather than a grid requirement, raising it above your
array's true peak is strictly better than any of this. Check that first.
