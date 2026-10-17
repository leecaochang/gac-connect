# GAC Connect

> **Beta. Use at your own risk.** Remote commands physically act on your vehicle
> (A/C, locks, windows, boot, charging).
>
> Not affiliated with, endorsed by or supported by GAC. GAC and AION are trademarks
> of their respective owners. Use it with a vehicle you own, on your own account.

An unofficial async Python client for **GAC / AION international** connected-car
accounts: read vehicle status, control climate, seats, locks, windows, boot and
charging, and receive command results.

## Install

```bash
pip install gac-connect          # library
pip install "gac-connect[cli]"   # library and the `gac` command
```

Requires Python 3.11 or newer.

## Quick start

```python
import aiohttp
from gac_connect import GacClient
from gac_connect.session import FileStore

async def main():
    async with aiohttp.ClientSession() as http:
        client = GacClient("AU", http, FileStore("~/.config/gac-connect/session.json"))
        await client.load()

        # First run only: solve the slide puzzle, then enter the SMS code.
        captcha = await client.start_captcha()
        # show captcha.background and captcha.piece (base64 PNGs); get the slide offset x
        await client.request_sms("04xxxxxxxx", x)
        await client.login_sms("04xxxxxxxx", "123456")

        vehicle = (await client.list_vehicles())[0]
        status = await client.get_status(vehicle.vin)
        print(status.soc, status.range_km, status.odometer_km)

        await client.lock(vehicle.vin)
```

## Sessions

- Pass a `TokenStore` to keep the session between runs: `FileStore(path)` or your
  own object with `load()` and `save(session)`. The default `MemoryStore` lasts one
  process.
- Each refresh replaces the refresh token, and `save` must persist it before the
  next request. Give every client its own sign-in: two clients sharing one stored
  session sign each other out.
- Signing in signs the official app out once. Sign the app back in and both keep
  working.
- If the vehicle session ends, it is renewed from the saved account session. If
  that fails, `AuthExpiredError` is raised and a new sign-in is needed.

## CLI

```bash
gac login --mobile 04xxxxxxxx --region AU   # opens the puzzle in a browser, then asks for the SMS code
gac vehicles
gac status <VIN>
gac charge <VIN> now [--timezone Australia/Sydney]
```

The session is kept in `~/.config/gac-connect/session.json`.

## API

### GacClient

`GacClient(region, http, store=None, *, material=None, limiter=None, timezone=None)`

- `region`: ISO country code (see [Regions](#regions)).
- `http`: an `aiohttp.ClientSession`.
- `timezone`: IANA zone for charge windows; defaults to the region's zone.

All methods are coroutines unless noted. `vin` is the vehicle's VIN.

**Sign-in and account**

| Method | Description |
|---|---|
| `load()` | Load the stored session. |
| `start_captcha()` | Start sign-in; returns a `Captcha` (`background`, `piece`, `width`, `height`, `piece_width`). |
| `request_sms(mobile, x)` | Submit the puzzle offset `x`; sends the SMS code. |
| `login_sms(mobile, code)` | Finish sign-in with the SMS code. |
| `list_vehicles()` | Vehicles on the account: `Vehicle` (`vin`, `plate`, `model`, `color`, `default`). |
| `session` | The current `Session` (property). |

`gac_connect.puzzle` holds the slide-puzzle step for any front end: `page(host)` is the
browser page, and `PuzzleSession(client, mobile, captcha)` turns each Verify into
`request_sms` (`submit(x)`, and `state()` for the page), showing a new puzzle after a
wrong slide. The CLI and the Home Assistant integration both use it.

**Reading**

| Method | Description |
|---|---|
| `get_status(vin)` | Full status as a `VehicleStatus`. |
| `get_position(vin)` | Raw position record. |
| `get_control_capabilities(vin)` | The car's capability list: what it offers remotely, by feature. |
| `poll_command_result(vin, session_id)` | The car's answer to an accepted command as a `PushResult`, or `None` while it is still running (for use without the push feed). |
| `get_alarms(vin, *, page=1, page_size=20)` | One page of the car's alert history; `parse_alerts(page)` turns it into `Alert`s. Needs the account session (`AccountSessionEnded` once another phone has signed in). |
| `get_departure_plans(vin)`, `get_standby_config(vin)` | Departure plans saved in the app (read only). |

**Climate and seats**

| Method | Description |
|---|---|
| `climate_on(vin, *, temperature=24.0, minutes=30)` | A/C in Auto at a target (18–32 °C) for 5–60 minutes. |
| `climate_manual_on(vin, *, temperature=24.0, minutes=30, fan_speed=None, blowing_mode=None, cycling_mode=None, compressor=True)` | Manual A/C. `fan_speed` 1–10; `blowing_mode` outlet code; `cycling_mode` 1 recirculate, 2 fresh air; `compressor=False` runs the blower only. Omitted settings are kept. |
| `climate_set_temperature(vin, temperature)` | Change the target of a running A/C. |
| `climate_off(vin)` | A/C off. |
| `set_seat_ventilation(vin, seat, enabled)` | Seat 1–4 ventilation on or off. |
| `set_seat_ventilation_level(vin, seat, level)` | Seat ventilation level 0 (off) to 3. |
| `fridge_on(vin, *, mode="refrigerate", temperature=None)` | Fridge mode `refrigerate` (0–20 °C), `heat` (35–50 °C) or `freeze` (−15 to −1 °C). |
| `fridge_off(vin)` | Fridge off. |

**Body**

| Method | Description |
|---|---|
| `lock(vin)` | Lock the doors. |
| `unlock(vin)` | Unlock the doors (no PIN). |

**Charging**

| Method | Description |
|---|---|
| `charge_now(vin, *, until_soc=False)` | Charge whenever plugged in (the schedule off). This does not start a new charge once one has ended. |
| `restart_charging(vin)` | Start a new charge once one has ended (`restart_needed(status)`): a one-time window to the charge limit from the next minute. Send `charge_now` once charging has begun to keep it going. Replaces the saved window. |
| `set_charge_window(vin, start, stop, *, weekly=0, until_soc=False)` | Charge between `start` and `stop` (`"HH:MM"`). `weekly`: Mon=1 … Sun=64, 127 every day, 0 once. A stop before the start runs overnight. |
| `resume_schedule(vin, *, until_soc=None)` | Return to the charge window the car last reported. |

`until_soc=True` stops at the car's own target charge level instead of the stop
time, like the app's "Charge cutoff SOC". No stop time is sent then (`stop` may be
`None`), and the car keeps its saved one.

**Any command**

`command(vin, name, **overrides)` sends a command by name. Overrides replace fields
in the request; invalid values raise `ValueError` before anything is sent.

| Name | Action |
|---|---|
| `aircon-on`, `aircon-manual-on`, `aircon-off`, `aircon-temperature` | A/C (see the climate methods). |
| `ventilate-on`, `ventilate-off` | Cabin ventilation. |
| `steering-on`, `steering-off` | Steering wheel heat. |
| `seat-1-ventilation-on` … `seat-4-ventilation-off` | Seat ventilation; `windStrength` 1–3. |
| `antivirus-on`, `antivirus-off` | Cabin sterilisation. |
| `fridge-on`, `fridge-off` | Fridge. |
| `lock`, `unlock` | Doors. |
| `window-open`, `window-close` | Windows. |
| `sunroof-open`, `sunroof-close` | Sunroof. |
| `tailgate-open`, `tailgate-close` | Boot. |
| `flash-on`, `flash-off` | Lights. |
| `horn-on`, `horn-off` | Horn. |
| `charge-schedule` | Charge reservation. |
| `battery-precondition`, `battery-precondition-off` | Battery preconditioning. |
| `vehicle-power-off` | Switch the car off (no overrides). |
| `powerkeep-stop` | Stop power keep. |
| `charger-unlock` | Unlock the charging connector so it can be pulled out. |
| `power-on`, `power-off` | Need a PIN; raise `PinRequiredError`. |

Commands return once the service accepts them; the car applies them shortly after.
Use `command_session_id(response)` to match a response with its push result.

### VehicleStatus

`None` means not reported. `raw` holds the full report.

| Field | Description |
|---|---|
| `online`, `updated_ms` | Connected; time of the car's report (ms). |
| `power_on`, `power_mode` | Car switched on; raw power mode. |
| `soc`, `range_km`, `odometer_km` | State of charge (%), range, odometer. |
| `charging`, `plugged_in`, `charger_locked` | Charging state. |
| `charge_ended` | The charge has ended (a one-time window finished, or the connector was unlocked); the car will not start another by itself. |
| `charge_current_a`, `charge_voltage_v`, `charge_power_kw`, `estimated_charge_minutes` | Charging figures. |
| `charging_mode` | `ChargingMode.FREE`, `SCHEDULED` (once), `LOOP` (repeating) or `UNKNOWN`. |
| `charge_window_start`, `charge_window_stop`, `charge_weekly`, `charge_end_type` | Reported charge window. |
| `cabin_temp_c`, `outside_temp_c`, `pm25`, `aux_voltage`, `battery_temp_c` | Cabin and outside temperature, cabin PM2.5, 12 V battery, battery temperature. |
| `trip_consumption_kwh_100km` | Average consumption of the drive under way, else the last one (the car's Trip figure). |
| `latitude`, `longitude` | Position. |
| `locked`, `door_open`, `window_open`, `hatch_open`, `hood_open`, `sunroof_open` | Body state. `locked` needs all four doors reported locked. |
| `front_left_door_open` … `rear_right_window_open` | Each door and window by position. |
| `headlights_on`, `high_beam_on`, `rear_fog_on`, `turn_signal_on`, `brake_lights_on` | Lights (the turn signal includes the hazard lights). |
| `brake_fluid_low`, `sterilisation_on` | Brake-fluid alarm; cabin sterilisation running. |
| `ac_on`, `ac_target_temp_c`, `ac_fan_strength`, `ac_auto_on`, `ac_compressor_on`, `ac_blowing_mode`, `ac_cycling_mode` | Climate. |
| `seats` | `Seat` list: `position` (1–4), `ventilation_on`, `ventilation_strength`, `heating_strength`. |
| `tyres` | `Tyre` list: `position` (1–4), `pressure_kpa`, `temperature_c`. |
| `steering_heat_on`, `lights_on`, `anti_theft_on` | Steering heat, lights, alarm. |
| `battery_precondition_on`, `battery_precondition_mode`, `battery_precondition_status` | Battery preconditioning. |
| `fridge_fitted`, `fridge_mode`, `fridge_temp_c`, `fridge_keep_mode`, `fridge_keep_minutes` | Fridge; keep mode `timed` or `unlimited`. |

`decode_status(results)` builds a `VehicleStatus` from a raw report.
`doors_locked(status, model)` reads the lock state with model-specific rules.

### Push results

Command results arrive on the service's push channel:

```python
from gac_connect import PushClient

def on_result(topic, payload, result):
    print(result.event, result.ok, result.code, result.reason)

push = PushClient(client.mqtt_info, on_result, decrypt=client.decrypt_push)
task = asyncio.create_task(push.run())   # reconnects until stop()
...
push.stop()
await task
```

`PushResult` fields: `ok` (`True` applied, `False` refused, `None` unknown), `code`,
`reason`, `message`, `event`, `session_id`, `vin`, `data`, and `details` (what the car
reported as the cause of a refusal, such as `"Driver door ajar"`). Some regions' brokers
are unencrypted; pass `allow_plaintext=True` to connect to them.

### Errors

All derive from `GacError`.

| Error | Raised when |
|---|---|
| `AuthExpiredError` | The session has ended and cannot be renewed; sign in again. |
| `AccountSessionEnded` | GAC's account session has ended (the app signed in elsewhere); only account features such as the alert list need it. |
| `CaptchaError`, `LoginError` | Sign-in failed. |
| `CommandError` | The service refused a command; `code` and `reason` say why. |
| `PinRequiredError` | The command needs a PIN. |
| `RateLimitedError` | A request limit was reached; `retry_after` in seconds. Nothing was sent. |
| `RegionError` | Unknown region. |
| `ResponseError` | Network failure, or a reply that was refused, could not be decrypted or was not as expected. |

### Helpers

| Name | Description |
|---|---|
| `validate_clock(value)` | Check an `"HH:MM"` time. |
| `validate_weekly(value)` | Check a day bitmask (0–127). |
| `window_operation(start, stop, tz, *, weekly=0, until_soc=False)` | The charge-window request body. |
| `local_mobile(value, country_tel)` | A mobile number in national format. |
| `restart_needed(status)` | Whether a plugged-in car's charge has ended and needs `restart_charging`. |
| `equipment_from_capabilities(data)` | Equipment flags from a `get_control_capabilities` response. |
| `ranges_from_capabilities(data)` | Remote choices and spans from a `get_control_capabilities` response: A/C temperature, run time, fan speeds, outlet and air-source codes, seat ventilation levels, battery preconditioning, fridge modes and temperatures. |
| `Capabilities`, `Support` | Equipment detection from the capability list and status reports. |

## Regions

`gac_connect.const.REGIONS` maps 56 country codes to their service hosts,
calling code and time zone.

- `LISTED_REGIONS`: the 21 countries offered in the GAC International app:
  `AE AU BR ES FI GR HK ID IL KH KW MO MX NZ PH PL PT RU SA SG TH`.
- `GB` uses the My AION app's own service and keys.
- Countries that span several time zones (for example AU, BR, MX, RU) use one
  default zone; pass `timezone=` for the car's own.

## Request limits

All clients in a process share one limiter (`DEFAULT_LIMITER`):

- one request at a time, at least 1 s apart;
- at most 20 a minute, 240 an hour and 3000 a day (`const.REQUEST_BUDGETS`);
- commands: at most 6 a minute and 60 an hour (`const.COMMAND_BUDGETS`);
- after a "too many requests" answer, nothing is sent for at least 60 s, or the
  service's `Retry-After` up to a day.

Over a limit, `RateLimitedError` is raised without sending. The limiter's state is
kept in a file shared by every program on the machine: `~/.cache/gac-connect/`
(Linux), `~/Library/Caches/gac-connect/` (macOS), `%LOCALAPPDATA%\gac-connect\`
(Windows), or `$GAC_CONNECT_LIMITS`. Delete it to reset. To keep the state
elsewhere, set `DEFAULT_LIMITER.state_path = None` before the first request and
use `export_state()` and `import_state()`.

## Acknowledgements

This project stands on the shoulders of the community projects that brought other
EV brands into Home Assistant and Python, among them:

- [AwangYes/BYD-re](https://github.com/AwangYes/BYD-re) — BYD
- [Hyundai-Kia-Connect/kia_uvo](https://github.com/Hyundai-Kia-Connect/kia_uvo) — Hyundai / Kia
- [bimmerconnected/bimmer_connected](https://github.com/bimmerconnected/bimmer_connected) — BMW / Mini
- [SAIC-iSmart-API/saic-python-client-ng](https://github.com/SAIC-iSmart-API/saic-python-client-ng) — MG / SAIC
- [kvanbiesen/bmw-cardata-ha](https://github.com/kvanbiesen/bmw-cardata-ha) — BMW CarData

Thanks to their authors for showing what a good community integration looks like.

## Changes

- **0.5.0** — `gac_connect.puzzle`, the sign-in puzzle page and session shared by the CLI
  and Home Assistant. Charging: one-time windows in the app's format (an open window
  starts at the next minute), `restart_charging` and `restart_needed`, more charging
  status codes. New readings: each door and window, lights, hood, brake fluid, battery
  temperature, trip consumption, sterilisation. Alert list, departure plan and command
  result reads. Fixes: a refresh no longer overwrites a sign-in saved meanwhile; a
  partial vehicle-session reply is rejected; an undecryptable reply or a refused status
  read is an error; `locked` needs all four doors; a malformed alert page is an error.
- **0.4.0b6** — UK (`GB`) IoV requests now seal to the same keypair the gateway
  uses for IoV responses, with a 24-byte (AES-192) session key instead of the
  usual 32-byte (AES-256) one; 0.4.0b4/b5 encrypted the request to a separate
  keypair, which the gateway could sign-check but not decrypt, returning
  `decrypt error`. Other regions are unchanged. Awaiting end-to-end
  confirmation from a UK user.

- **0.4.0b5** — UK (`GB`) IoV crypto is now entirely its own, not GAC
  International's. The shared IoV gateway validates the signature per app id, and
  GB runs under its own IoV app id (3, vs. intl's 2), so after the app id fix in
  0.4.0b3 the previously-working shared signing key started failing with
  `sign match fail`. The UK profile now uses its own IoV signing secret and its
  own IoV request keypair, so the request is no longer signed or encrypted with
  the shared intl material. Other regions are unchanged.

- **0.4.0b4** — UK (`GB`) sign-in now carries through the IoV session exchange:
  the UK profile presents its own app id on the IoV calls and decrypts the IoV
  session response (and push messages) with its own response key, and encrypts
  the request it sends with its own request key. Other regions are unchanged.
  Awaiting end-to-end confirmation from a UK user.

- **0.4.0b1** — support for markets that run a separate national app on their own
  backend, via a per-region "profile". The United Kingdom (`GB`) now uses the
  My AION app on its own host (`app-api.aionauto.co.uk`) with its own key material,
  while sharing the IoV gateway and the rest of the protocol. The default GAC
  International build is unchanged (profiles are inert unless their region is
  selected). UK sign-in is not yet confirmed end to end by a UK user.

- **0.3.0b1** — two headline changes over the 0.2 line. **Signing in on the phone
  app and Home Assistant no longer sign each other out:** after minting the vehicle
  session, sign-in refreshes the account token once (as the app does), so a later
  app sign-in can no longer end the session — in Home Assistant, re-authenticating
  and then signing back in on the phone no longer signs the integration out.
  Signing in here still signs the app out once, and a session from an earlier
  version needs one re-authentication to benefit. **All 56 regions** the app is
  configured for are supported (see the b11 note below), with Israel (`IL`)
  reported working. Also: `FileStore` expands `~`.

- **0.2.0b11** — all 56 regions the service supports: `IL` (Israel) among them,
  plus `LISTED_REGIONS`
  and `CONFIRMED_REGIONS` to say how far each one has been checked. Fixes the IoV
  gateway for the Singapore and Middle East regions, which pointed at the EU one.
  `GacClient(..., timezone=...)` (and `gac charge --timezone`) overrides the
  region's default zone for countries that span several.

- **0.2.0b10** — Greece (`GR`) added to the regions (best-effort; shares the EU
  gateway). Thanks to @panosru.

- **0.2.0b9** — request limits: one `Limiter` shared by every client in a process
  (and kept between CLI runs), sending one request at a time with rolling budgets
  per minute, hour and day, a separate command budget, and a pause after any 429
  answer (Retry-After as seconds or a date, up to a day); requests over a limit
  raise `RateLimitedError` without being sent. The shared limiter's state is kept
  in a file in the user's cache folder, so separate runs share it; a client's own
  limiter applies on top of it. Limiter state can be exported and restored. Redirects are
  not followed. Token refreshes are coordinated between clients, rotated tokens are
  saved before use, and a session that can no longer refresh is not retried until
  a new session is stored.

- **0.2.0b8** — the fridge's keep-running setting in the vehicle status:
  `fridge_keep_mode` (timed or unlimited) and `fridge_keep_minutes` (time left).

- **0.2.0b7** — fridge / warmer box: `fridge_on(mode, temperature)` and
  `fridge_off()`, with per-mode temperature ranges checked before sending, and
  the fridge's mode and target temperature in the vehicle status.

## License

MIT.
