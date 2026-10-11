# GAC Connect

> **⚠️ BETA — not fully tested, use entirely at your own risk.** This is an
> unofficial client with no relationship to GAC. Expect bugs; things that
> worked yesterday may not work tomorrow. Remote commands physically act on your
> vehicle (A/C, locks, windows, tailgate, charging) and may misbehave or fail;
> using it alongside the official app may sign one of them out. Nothing here is
> warranted to work, keep working, or be safe. Review what an automation can do
> before you let it touch the car.

An unofficial async Python client for **GAC / Aion international** connected-car
accounts — read your vehicle's status and control charging from your own code.

> This project is not affiliated with, endorsed by, or supported by GAC or its
> affiliates. GAC and AION are third-party trademarks of their respective
> owners. Use it with a vehicle you own, on your own account.

Confirmed against **Australia and New Zealand**. Every other country the service
supports is in the region table too, listed but not yet tested — see
[Regions](#regions). Reports welcome.

## Install

These examples describe this source checkout, including unreleased audit fixes.
From the repository root, install it into an isolated Python environment:

```bash
python -m pip install .          # library from this checkout
python -m pip install '.[cli]'   # + the `gac` command
```

The published beta used by the Home Assistant integration is installed with
`python -m pip install 'gac-connect==0.4.0b1'` (or
`'gac-connect[cli]==0.4.0b1'`). It does not include the fixes in this checkout.
An unqualified `pip install gac-connect` selects a stable release rather than
opting into betas. Publishing a new library version and changing Home Assistant's
dependency pin are separate release steps; neither happens by editing this folder.

## Library

Phone numbers may be local, or international with an explicit `+` or `00`
prefix matching the selected region. Spaces, hyphens and parentheses are accepted.
Bare local numbers beginning with `61` retain those digits.

```python
import aiohttp
from gac_connect.client import GacClient  # also works with the published beta

async def main():
    async with aiohttp.ClientSession() as http:
        client = GacClient("AU", http)

        # First run: sign in. You solve a slide puzzle and enter an SMS code.
        captcha = await client.start_captcha()
        # ... show captcha.background / captcha.piece, get the slide offset x ...
        await client.request_sms("04xxxxxxxx", x)
        await client.login_sms("04xxxxxxxx", "123456")

        vehicles = await client.list_vehicles()
        status = await client.get_status(vehicles[0].vin)
        print(status.soc, status.range_km, status.odometer_km)

        # Optional: choose an explicit daily charging window.
        await client.set_charge_window(vehicles[0].vin, "23:00", "05:00")
```

Pass a `TokenStore` to persist the session (the refresh token is single-use and
rotates on every refresh, so persistence matters):

```python
from gac_connect.session import FileStore
client = GacClient("AU", http, FileStore("~/.config/gac-connect/session.json"))
await client.load()
```

## CLI

The browser puzzle labels its slider and announces verification status. Focus
the slider to adjust it with arrow keys. The service still requires a visual
puzzle; a nonvisual verification alternative has not been established.

```bash
gac login --mobile 04xxxxxxxx     # opens a browser puzzle, then asks for the SMS code
gac vehicles
gac status <VIN>
gac charge <VIN> now              # unrestricted charging when plugged in
```

## What you can read

State of charge, range, odometer, charging state and mode, plugged-in, cabin
temperature, 12 V battery, GPS, doors / windows / boot / lock state, per-tyre
pressure (in kPa) and temperature, and, where fitted, the fridge's mode, target
temperature and keep-running setting (`status.fridge_mode`, `status.fridge_temp_c`,
`status.fridge_keep_mode`, `status.fridge_keep_minutes`).

For model-aware lock decoding, pass `model="Aion UT"` to
`VehicleStatus.from_results(...)`, or use `gac_connect.models.doors_locked(status, model)`.
The verified UT pattern (front doors locked, both rear lock readings `-1`) is
recognized only for an explicitly selected UT; other incomplete reports stay unknown.

Telemetry keeps unknown values as `None`: a group is closed/locked only when its
readings establish that state. Malformed fields do not discard unrelated readings.
`status.tyres` has four stable API-ID slots; `Tyre.position` is `"1"` through `"4"`,
not an unverified physical corner. Duplicate IDs and paired unreported `-1`
pressure/temperature values remain unknown. The original report is retained in `raw`.

## Fridge

On cars with a fridge / warmer box:

```python
await client.fridge_on(vin, mode="refrigerate", temperature=3)   # 0 to 20 °C
await client.fridge_on(vin, mode="heat", temperature=42)         # 35 to 50 °C
await client.fridge_on(vin, mode="freeze", temperature=-12)      # -15 to -1 °C
await client.fridge_off(vin)
```

Each temperature is checked against the mode's range, then rounded to a whole
degree; bad values raise `ValueError` before anything is sent. Without a
temperature, this library's default for the mode is sent (refrigerate 3, heat 42,
freeze -12 °C), which also resets the target if the fridge is already running.
Calling `fridge_on` while the fridge runs changes its mode or temperature.

An accepted request is not proof the car applied it; check `status.fridge_mode`.

How long the fridge keeps running after you leave the car is a setting in the car.
`status.fridge_keep_mode` reports it as `"timed"` or `"unlimited"`. For a timed
setting, `status.fridge_keep_minutes` reports the remaining minutes supplied by the
service, which count down once you leave the car, or `None` when unavailable or
invalid. It is always `None` for unlimited or unknown settings.

## Command results

`PushClient` requires a TLS broker by default. `allow_plaintext=True` explicitly
permits transmitting broker credentials without TLS, exposing them and command
results to network interception. When a decryptor is supplied, unencrypted or
undecryptable messages produce unknown outcomes. Envelope encryption alone does
not authenticate the sender; use TLS for transport integrity.

Commands are accepted asynchronously and their results are delivered by the
service over a separate channel. `PushClient` keeps that channel open and hands
each result (outcome, command event, session id) to your handler:

```python
from gac_connect import PushClient

def on_result(topic, payload, result):
    print(result.event, result.ok, result.session_id)

# The service's broker may be plain TCP; its credentials are short-lived, but
# you must opt in to sending them unencrypted.
push = PushClient(client.mqtt_info, on_result, decrypt=client.decrypt_push, allow_plaintext=True)
task = asyncio.create_task(push.run())   # reconnects on its own until stop()
...
push.stop()
await task
```

## Regions

Custom `Material` still accepts the original five constructor arguments. When
`iov_response` is omitted (or `None`), gateway responses and push messages use
the `iov` keypair. An explicit response keypair remains separate from the request
keypair, as required by the UK profile.

`GacClient(region, ...)` takes an ISO country code. The table in
`gac_connect.const.REGIONS` covers all 56 countries the service is configured
for, with the two hosts, calling code and time zone each one uses:

```python
from gac_connect.const import CONFIRMED_REGIONS, LISTED_REGIONS, REGIONS

REGIONS["IL"]        # {'main': 'nl-app-api...', 'iov': 'eu-iov-sdk-access...', 'tel': '+972', ...}
sorted(LISTED_REGIONS)     # the 21 the app offers in its own country picker
sorted(CONFIRMED_REGIONS)  # the ones this library has actually signed in to
```

Three tiers, narrowest first:

- **Confirmed** (`AU`, `NZ`) — signed in and driven a car with.
- **Listed** — the app's own country picker offers them, so accounts exist:
  `AE AU BR ES FI GR HK ID IL KH KW MO MX NZ PH PL PT RU SA SG TH`. `IL` has a
  second-hand report of working; it stays out of the confirmed set until someone
  says so first-hand.
- **The rest** — wired into the build ahead of sale. The hosts are right; whether
  there is an account behind them is another matter.

Within the GAC International app nothing is region-specific — same keys, same
signing, same endpoints — so a region there is only ever the four values above.
If one does not work, it is worth a report.

Some markets are served by a **separate app on a separate backend with its own
keys**. The library reaches them through a per-region "profile" (see
`PROFILE_MATERIAL` in `const.py`):

- **United Kingdom** — AION Auto UK (`app-api.aionauto.co.uk`) is its own
  platform, served by the My AION app with its own key material and its own IoV
  app id (3). `region="GB"` selects that profile: it
  targets `app-api.aionauto.co.uk`, signs IoV requests with the My AION HMAC
  secret, encrypts them with the My AION request key, and decrypts responses
  with the My AION response key. UK sign-in is still awaiting end-to-end
  confirmation from a UK user.
- **Mainland China** — the domestic Aion app is a separate build too; not
  supported.

The table carries one time zone per country, which is a guess wherever a country
spans several (RU, BR, MX, ID, AU, ES, PT, CL). Charge windows are set in local
clock time, so give the car's own zone when the default is not it:

```python
client = GacClient("RU", http, timezone="Asia/Vladivostok")   # gac charge ... --timezone
```

## Sharing a session

The IoV refresh token is **single-use**: each refresh mints a replacement and
retires the one it was given. A session is therefore a chain, and only one holder
can advance it. Two things working through the same saved session will spend each
other's tokens, and whichever refreshes second is told its token was already used
and has to sign in again.

So give each client its own sign-in. Two Home Assistant entries pointed at one
stored session, the CLI sharing a session file with Home Assistant, or a session
copied between machines will all fail this way, intermittently and confusingly.
The same applies if a rotation is not saved: `TokenStore.save` is called on every
rotation and must durably persist before the next request, which is why the
default `MemoryStore` is only suitable for a single short-lived process.

Temporary HTTP errors or incomplete refresh responses retain the saved credentials
and raise a retryable library error. Only an expired refresh token or an explicit
spent-token rejection requires signing in again.

### Running alongside the official app

Signing in here can sign the phone app out. The client attempts an account-token
rotation after creating the vehicle session so later app sign-ins can coexist.
If that optional step fails, vehicle sign-in still succeeds and logs a warning;
a later app sign-in may end that session. Behavior remains region-dependent.

Signing in mints two things: an account session on the main API and a vehicle
session on the IoV gateway. The account is allowed one current account session,
so a sign-in anywhere replaces the last one — that is what logs the phone app out
when this library signs in, and the app does the same to this library when it
signs back in.

A vehicle session is ended along with the account token that minted it, unless
that token has since been refreshed. So straight after minting the vehicle
session, sign-in refreshes the account token once, as the app itself routinely
does. After a successful rotation, this has allowed the vehicle session to outlive
later app sign-ins in tested regions. It is not a guarantee for every backend.

Versions before 0.3.0b1 skipped that step, so a session they minted was ended by
the next app sign-in — in Home Assistant, a re-authentication followed by signing
back in on the phone signed the integration out again. Re-authenticate once on
0.3.0b1 or later to replace such a session.

## Charging control

`charge_now` and `set_charge_window` control the car's charging schedule.
The car applies commands asynchronously; acceptance does not confirm execution.
Continuous pause is unavailable: `charge_pause`, `pause_operation` and CLI
`gac charge <VIN> pause` raise an error without sending a request. The previous
operation allowed charging every day from 03:00 to 03:30 and was not a true stop.

Windows require distinct `HH:MM` times. Their stop follows the next selected start,
including overnight. `weekly` is a whole-number mask from 0 to 127 (Monday=1,
Sunday=64, 0=every day); the start date honors that mask in the configured zone.

## Request limits

Every request the library sends passes one limiter shared by all clients in the
process (`gac_connect.DEFAULT_LIMITER`); a client given its own `Limiter` is held
to both, so creating clients never resets the limits. Requests go out one at a
time, even across threads, at least a second apart, and no more than 20 a minute,
240 an hour and 3000 a day (`const.REQUEST_BUDGETS`, rolling windows, each
request counted when it finishes). Vehicle commands also have their own budget of
6 a minute and 60 an hour (`const.COMMAND_BUDGETS`). If the service answers "too
many requests", nothing is sent for at least 60 seconds, or for its `Retry-After`
(seconds or a date) up to a day. Past any of these the client raises
`RateLimitedError` (with `retry_after` in seconds) without sending anything.

The shared limiter keeps its state in a small file, locked while each request
runs, so every program and every run on the machine shares one budget and one
pause: a script restarted in a loop is limited like one that keeps running. The
file lives in your cache folder (`~/.cache/gac-connect/limits.json` on Linux,
`~/Library/Caches/gac-connect/` on macOS, `%LOCALAPPDATA%\gac-connect\` on
Windows) or wherever `GAC_CONNECT_LIMITS` points. If it cannot be read or written,
nothing is sent. If it is corrupt, a copy is kept (`limits.corrupt`) and requests
pause for a day, since any pause recorded in it is unknown; delete the file to reset
it sooner. A host that stores the state itself can set
`DEFAULT_LIMITER.state_path = None` before its first request and use
`export_state()` / `import_state()` instead (the Home Assistant integration does).

What is counted: requests this library sends on one machine. Other machines, the
official app, and the push channel's broker connection are not counted. A poll
every few minutes is plenty for most uses.

## Notes and limits

- Sign-in needs a human: a slide puzzle and an SMS code. There is no headless login.
- Door locking and unlocking are available. Door unlock does not require a PIN:
  use `await client.unlock(vin)` or `await client.command(vin, "unlock")`.
  Remote power and charger-release remain PIN-gated and are refused by this client.
- Using the official app and this client on the same account at the same time can
  occasionally sign one of them out.

## Home Assistant

A Home Assistant integration built on this library is available separately.

Consumers can import `GacClient`, `PushClient`, `decode_status`, `doors_locked`,
`local_mobile`, `validate_clock`, `validate_weekly`, `window_operation`, and
`PAUSE_UNSUPPORTED` directly from `gac_connect`. `decode_status` is an alias for
`VehicleStatus.from_results`, the decoder used by the client. The library owns
HTTP/authentication behavior, command acceptance, telemetry, schedule construction,
phone normalization, and push decoding; consumers own their UI and storage lifecycle.

These public behavior exports accompany the consolidated client fixes. The HA
integration uses their availability to select the native library implementation;
its pinned b1 release still needs a separate compatibility adapter. Do not remove
that adapter or change the dependency pin until a new library artifact has been
published and tested without the adapter.

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

## Development

Use an isolated environment with `python -m pip install '.[cli,dev]'`, then run
`python -m pytest` and `ruff check .`. Tests use synthetic responses and do not
send commands to a vehicle. The validation workflow covers Python 3.11, 3.13 and
3.14, includes CLI tests, and builds and installs a wheel in a fresh environment.
Its artifact smoke test runs outside the checkout and checks public imports,
both bundled key profiles, the typing marker, and encrypted response decoding.
Make the workflow a required repository check before relying on it as a merge
gate. Live regional gateway compatibility remains a separate validation step.

## License

MIT.
