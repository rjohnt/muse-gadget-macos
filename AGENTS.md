# Working on Muse Gadget for macOS

This is a standalone experimental macOS transport for the upstream Muse
Gadget SDK. Preserve the pinned dependency and its license. Keep pairing
cryptography and Noise protocol upstream; isolate platform code in native/.

Never read or print .env, pairing JSON, tokens, signed image URLs, account
identifiers or private display payloads in tool output. Read credentials
only in the local runtime or secret checker. Never put them in arguments,
source, commits, CI, examples, screenshots, release artifacts or logs.
The SDK token goes through the encrypted phone pairing exchange; it is
not a general API bearer token.

Register only documented display commands by default. No shell, arbitrary
file access or OTA. Validate payloads fully before updating the display.
Preserve the previous character if an image transfer fails. Keep the
preview read-only, loopback-bound and separate from credentials.

Run pytest and scripts/build-native.sh for relevant changes. Check the
tracked file list, scripts/check-secrets.py and gitleaks on all reachable
Git history before publishing. A successful build or BLE advertisement
is not evidence of phone pairing, authenticated command registration,
command delivery or physical-reader behavior; report these separately.
Do not reset a working pairing to troubleshoot display issues.
