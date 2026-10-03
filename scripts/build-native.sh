#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
app_dir="build/Muse Mac Gadget.app/Contents"
mkdir -p "$app_dir/MacOS"
cp native/Info.plist "$app_dir/Info.plist"
xcrun swiftc native/Peripheral.swift -o "$app_dir/MacOS/MuseMacPeripheral" -framework AppKit -framework CoreBluetooth
codesign --force --sign - "build/Muse Mac Gadget.app"
