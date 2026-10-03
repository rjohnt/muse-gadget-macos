# Muse Gadget for macOS

An experimental CoreBluetooth adapter for the [Muse Gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk), plus a restricted display gadget and live browser preview. Pair a Mac with the Muse phone app, receive a character and caption, and try small watch/event cards before moving a UI onto hardware.

This is a community project, not an official Meta or Xteink product. The adapter reuses upstream pairing v5, encrypted Noise sessions, device-token rotation and reconnect behavior. It replaces the Linux BlueZ peripheral with a small native Swift process. No shell, arbitrary file, or firmware-update commands are registered.

## Status

The native adapter compiles and CoreBluetooth reports advertising on an Apple Silicon Mac. An independent iPhone BLE scanner found the SDK service on an unnamed peripheral, while Muse did **not** discover the combined name/service advertisement. A name-only advertising mode now attempts to avoid macOS dropping the longer name; Muse discovery in that mode remains unverified. Eight local adapter tests and the pinned SDK's 137 tests pass. End-to-end pairing and delivery remain unverified. Do not interpret the browser preview or an advertising state as a successful Muse connection.

By default, CoreBluetooth advertises only the complete gadget name. The SDK service and characteristics remain published in GATT and available after connecting. `--advertisement name-and-service` also puts the 128-bit service UUID in the advertisement, but macOS can drop the longer name when space is tight. Name-only mode cannot work with a scanner that requires that UUID in the advertisement; combined mode may fail a scanner requiring the name. These app compatibility limits need physical testing. Apple's peripheral advertising API also does not expose BlueZ's arbitrary manufacturer-data field. Whether a Muse app version requires that field must be checked with the phone. The Mac adapter cannot actively cancel a central's connection; failed setup tears down the GATT service and requires restarting pairing.

## Requirements

- macOS with Bluetooth LE, Python 3.11+, Xcode Command Line Tools (`xcode-select --install`).
- [uv](https://docs.astral.sh/uv/) or pip, and Internet access for dependencies.
- Muse on your phone, Developer mode enabled, and your own [SDK token](https://gadgets.muse.ai/settings/sdk-tokens). Review the [Gadget SDK Terms](https://gadgets.muse.ai/sdk-terms).

## Install and pair

From this checkout:

```sh
uv venv
uv pip install -e '.[test]'
scripts/build-native.sh
cp .env.example .env
chmod 600 .env
```

Edit `.env` locally and put your token inside the empty quotes. Do not put the token in a shell command or chat. Then:

```sh
.venv/bin/muse-mac pair
```

Allow Bluetooth if macOS asks. The browser opens a live preview and shows the randomly generated gadget name. On your phone, use **Muse → Settings → Devices → Add Device**, select that exact `MuseGadgetXXXXXX` name, accept the community-device prompt, then choose **Use current connection**. The Mac uses its existing network; it does not retain the phone's Wi-Fi credentials.

The pairing window closes after ten minutes. Successful pairing continues into a live Muse session in the same process. The preview says **connected** only after the server acknowledges command registration. If already paired, start it with:

```sh
.venv/bin/muse-mac run
```

If Muse finds no devices, use an independent BLE scanner on the phone to check the exact name. After connecting, look for GATT service UUID `7fdd3d1c-38ea-46cf-8b46-314ecf5f240c`; in name-only mode it is not in the advertisement itself. A scanner seeing the advertisement while Muse does not would point to app filtering/compatibility; neither seeing it requires investigating the radio/advertising path. Check Bluetooth access for Muse in the phone's privacy settings. Disconnect from the scanner before retrying Muse. Restart `pair` to open a fresh window; it retains the gadget identity and refuses to overwrite an existing pairing.

To view the empty interface without credentials or Bluetooth:

```sh
.venv/bin/muse-mac preview
```

`--env-file PATH`, `--state-dir PATH`, `--native-app PATH`, and `--no-browser` support different environments. Paths are safe command arguments; tokens are not. Press Ctrl-C to stop. There is no persistent background service installed.

## Verify actual delivery

Ask your Muse:

> On my Mac Pocket Preview gadget, call pocket.set_status with text "SDK connection verified". Then call pocket.get_status and report the result. Send your character through display.draw_url as a public HTTPS image. Tell me if any command fails.

An accepted command increments the preview's received count. A visible exact caption verifies delivery. A character verifies the separate download and rendering path. Failed image updates preserve the previous character. Images are fitted inside a 480×480 canvas and dithered to black and white.

## Commands

| Command | Required parameters | Behavior |
| --- | --- | --- |
| `pocket.set_status` | `text` | Caption, up to 240 UTF-8 bytes |
| `display.draw_url` | `url` | Public HTTPS image, up to 5 MB |
| `pocket.set_watch_digest` | `payload` | JSON string with `updated` and `items` |
| `pocket.set_next_up` | `payload` | JSON string with `updated`, `title`, `when`, `ends`, `detail` |
| `pocket.get_status` | none | Connection state, command count and last command; excludes private content |

For watch/event commands, `payload` is a **serialized JSON string**, using scalar parameter types understood by the SDK command registry. Every timestamp needs an ISO 8601 timezone offset or `Z`.

Example decoded watch payload (serialize this object into `payload`):

```json
{
  "updated": "2026-10-03T10:00:00Z",
  "items": [
    {"label": "Example delivery", "state": "watching", "note": "Awaiting an update", "checked": "2026-10-03T09:50:00Z"}
  ]
}
```

A digest replaces the list; at most ten items, and an empty list clears it. Cards become stale after 24 hours without updates. An event needs an explicit end time, expires an hour after that end, and computes its countdown locally. An empty title clears it. The gadget receives curated payloads; it does not access calendar, task, or watch internals by itself.

## Privacy and security

- `.env` is ignored, must be owner-only, and is parsed as data rather than executed. Its token is never exported to the environment or supplied as a process argument.
- Pairing state lives outside the checkout, by default in `~/Library/Application Support/MuseMacGadget`, with a mode-0700 directory and mode-0600 JSON files. It contains device access/refresh tokens; never share it.
- The Swift process only transports BLE packets. SDK credentials stay in the Python pairing/session code; upstream logs are suppressed to avoid account identifiers or signed image URLs appearing in output.
- The preview is read-only and binds to loopback on a random port. It exposes displayed content to processes on the same Mac. It never returns SDK or device credentials. Display content and images are held in memory, not saved to the repository.
- Image URLs must use HTTPS and resolve to public addresses; redirects are checked too. This is not a sandbox against a malicious paired Muse or DNS operator.
- Community pairing has no manufacturer attestation. Pair your own trusted gadget locally. The private display content is separate from reusable source.

## Extend it

`MacTransport` implements the SDK's transport interface (`send_packets`, `mtu`, `disconnect`); the pairing cryptography stays upstream. `DisplayExecutor` supplies the command implementation, and `COMMANDS` supplies the advertised registry. Add a schema and handler there, validate the payload before mutation, and add tests. Restart the session to register changes. Keep privileged commands opt-in in any forks.

The SDK currently registers the desktop device with its upstream `linux` / `homehub` compatibility metadata. The local pairing firmware version identifies the macOS adapter. Remote OTA is not advertised. A future upstream platform-neutral device description can replace this compatibility choice.

The hours strip in the example preview is an optional local UI demonstration using a fixed Central-time schedule. It is not part of the SDK transport or an authoritative liturgical calendar.

## Development

```sh
.venv/bin/python -m pytest
scripts/build-native.sh
```

The SDK dependency is pinned to commit `b1a3822995a51c0203cd1f3d72c1c656b8c3e620`. Install it independently if you want to run its complete test suite. No pairing or credentials are needed for tests.

Before publishing:

```sh
python scripts/check-secrets.py
gitleaks git --redact --no-banner
```

The custom checker scans tracked files and all reachable history against locally supplied credential values without printing matches. Gitleaks adds general credential-pattern detection. Neither check makes private images or pairing files safe to publish.

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Upstream SDK copyright and license notices remain with the installed dependency. Proprietary SDK avatars are not included. Muse's SDK token terms are separate from this source license.
