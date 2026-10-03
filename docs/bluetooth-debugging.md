# Debugging Muse Bluetooth pairing on macOS

This adapter is a native CoreBluetooth peripheral. It substitutes for the Linux
BlueZ transport while retaining the upstream Muse pairing and Noise protocols.
It is also a useful diagnostic display: the browser separates Bluetooth setup
from authenticated command delivery.

## The first Mac connection: what went wrong

There were two separate obstacles:

1. **The SDK assumed Linux.** Its desktop peripheral used BlueZ. On macOS we
   needed a native CoreBluetooth peripheral, bridged to the existing Python SDK.
   Reusing the SDK preserved its pairing protocol and encrypted session handling.
2. **An active advertisement was not enough.** Our first adapter advertised the
   gadget name together with the 128-bit service UUID. macOS reported success,
   but Muse’s device picker stayed empty. LightBlue found a strong nearby
   peripheral, sometimes displayed under the Mac’s system name, and could inspect
   the SDK GATT service. This established that the Bluetooth peripheral existed;
   it did not establish that Muse could recognize its advertisement.

We checked **Advertisement Data → Local Name**, then switched to name-only
advertising. The complete `MuseGadgetXXXXXX` name became visible and Muse paired.
After phone authorization, the server acknowledged command registration; caption
and avatar delivery were verified separately.

This points to advertisement contents and discovery compatibility as the problem
in that attempt. We did not instrument Muse’s scanner, so we do not claim to know
its exact filtering rules. The name-only workaround is now the default.

Bluetooth is used for phone-to-Mac setup. Ongoing Muse commands travel over the
SDK’s encrypted Internet session, not over a continuous phone Bluetooth link.

## Check each stage separately

| Stage | Evidence | What remains unverified |
| --- | --- | --- |
| Advertising | CoreBluetooth reports that advertising started | The phone can discover it |
| Phone connected | The phone subscribes to the SDK notification characteristic | User confirmation and credentials |
| Authorized | The encrypted pairing exchange is confirmed | Authenticated server registration |
| Muse connected | The server acknowledges the command registry | Any display command has run |
| Caption received | Received count increases and the caption changes | Image download and rendering |
| Avatar received | Image revision increases and the character appears | Physical e-paper rendering |

The status bar shows the first four stages. The received-command counter and
character are separate evidence. A chat request marked sent only means the
request was acknowledged; it does not mean Muse called a display command.

## Muse finds no nearby devices

1. Run `muse-mac pair` and keep the Mac awake and the phone nearby. Check Bluetooth
   permission in macOS Privacy & Security and Muse's Bluetooth permission on iOS.
2. In LightBlue on the phone, inspect nearby peripherals. The device may initially
   appear unnamed or under the Mac's system name. That heading is not sufficient
   to identify the advertisement.
3. Inspect **Advertisement Data → Local Name**. Compare it with the complete
   randomly generated `MuseGadgetXXXXXX` name shown in the preview.
4. Connect with LightBlue and inspect the GATT service
   `7fdd3d1c-38ea-46cf-8b46-314ecf5f240c`. Name-only advertisements do not include
   this service UUID in advertisement data; the service remains accessible in GATT.
5. Disconnect LightBlue before trying Muse again. The adapter supports one central
   at a time. Discover and select the gadget in Muse's Add Device flow.

Do not expect iOS Settings → Bluetooth to be a reliable BLE diagnostic browser.
A BLE scanner and Muse's own device picker are the useful checks here.

## Why name-only advertising is the default

We observed a combined local-name/128-bit-service advertisement that CoreBluetooth
reported as active and LightBlue could connect to, but Muse could not discover.
Changing to name-only advertising exposed the complete SDK discovery name and
allowed the Muse app to pair. This is an observed compatibility workaround, not a
claim that every macOS/iOS/Muse version behaves identically.

Compare both modes when reporting a reproducible problem:

```sh
muse-mac pair --advertisement name-only
# Stop the unpaired attempt before trying the other mode.
muse-mac pair --advertisement name-and-service
```

Apple documents peripheral advertising as best effort, with limited payload space
and only the local-name and service-UUID advertisement keys:
[CBPeripheralManager.startAdvertising](https://developer.apple.com/documentation/corebluetooth/cbperipheralmanager/startadvertising%28_%3A%29?language=objc).
A scanner requiring the UUID in the advertisement may need combined mode; a Muse
version filtering by complete name may need name-only mode. Manufacturer data
available through BlueZ is not exposed by this CoreBluetooth API.

`--name` changes the friendly gadget name in Muse and the preview. It does not
change the required `MuseGadgetXXXXXX` discovery identity.

## Paired, but the preview is empty

Run `muse-mac run`; do not reset a working pairing. Ask Muse to call
`display.set_caption` and then `display.get_status`. The latter returns only
connection state, accepted-command count, and last accepted command.

`--request-avatar` requests the existing avatar through `display.draw_url`.
Transparent PNG is preferred, but an existing opaque avatar is accepted. The
image must be publicly downloadable over HTTPS; a URL requiring cookies or login
will not work. Failed image transfers preserve the previous character. PNG alpha
is retained while the image is dithered to black and white.

The Mac preview verifies the download and browser rendering path. It does not
verify the X4 Pro firmware image decoder, panel, buttons, or battery behavior.

## Useful issue reports

Include macOS and iOS versions, the Muse app version, advertisement mode, the
last completed stage, and whether LightBlue showed the complete Local Name and
SDK service. State whether a caption and character arrived independently.
Use generic names and crop/redact display content before attaching pictures.

Never attach `.env`, pairing JSON, SDK/device tokens, signed image URLs, private
watch/event payloads, or generated user characters. Upstream logs are suppressed
because exceptions can include identifiers and credentials. The local preview
is read-only and never returns SDK or device credentials.
