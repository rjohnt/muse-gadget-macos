// SPDX-License-Identifier: Apache-2.0
// CoreBluetooth transport only. Pairing secrets never enter this process.
import AppKit
import CoreBluetooth
import Foundation

func emit(_ message: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: message) else { return }
    FileHandle.standardOutput.write(data + Data([10]))
}

final class Peripheral: NSObject, CBPeripheralManagerDelegate {
    var manager: CBPeripheralManager!
    let localName: String
    let advertisementMode: String
    let serviceID = CBUUID(string: "7fdd3d1c-38ea-46cf-8b46-314ecf5f240c")
    let rxID = CBUUID(string: "4d593029-28a2-4a6e-a1f0-3c2d5e8f9b01")
    let txID = CBUUID(string: "d75dc4ca-7b2b-4e9c-8f0a-1d2e3f4a5b6c")
    var tx: CBMutableCharacteristic!
    var central: CBCentral?
    var pending: [Data] = []
    var advertisingWanted = true

    init(name: String, advertisementMode: String) {
        localName = name
        self.advertisementMode = advertisementMode
        super.init()
        manager = CBPeripheralManager(delegate: self, queue: .main)
    }
    func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        let states: [CBManagerState: String] = [.poweredOn: "powered_on", .poweredOff: "powered_off", .unauthorized: "unauthorized", .unsupported: "unsupported", .resetting: "resetting", .unknown: "unknown"]
        emit(["event": "bluetooth", "state": states[peripheral.state] ?? "unknown"])
        guard peripheral.state == .poweredOn else { return }
        let rx = CBMutableCharacteristic(type: rxID, properties: [.write, .writeWithoutResponse], value: nil, permissions: [.writeable])
        tx = CBMutableCharacteristic(type: txID, properties: [.notify, .read], value: nil, permissions: [.readable])
        let service = CBMutableService(type: serviceID, primary: true)
        service.characteristics = [rx, tx]
        peripheral.add(service)
    }
    func peripheralManager(_ peripheral: CBPeripheralManager, didAdd service: CBService, error: Error?) {
        guard error == nil else { emit(["event": "error", "code": "service_failed"]); return }
        if advertisingWanted {
            var payload: [String: Any] = [CBAdvertisementDataLocalNameKey: localName]
            // macOS can discard long local names when a 128-bit UUID consumes
            // the primary advertisement. The GATT service remains published
            // in either mode; only its presence in the advert changes.
            if advertisementMode == "name-and-service" { payload[CBAdvertisementDataServiceUUIDsKey] = [serviceID] }
            peripheral.startAdvertising(payload)
        }
    }
    func peripheralManagerDidStartAdvertising(_ peripheral: CBPeripheralManager, error: Error?) {
        emit(["event": error == nil ? "advertising" : "error", "code": error == nil ? "ready" : "advertising_failed"])
    }
    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral, didSubscribeTo characteristic: CBCharacteristic) {
        if self.central != nil && self.central?.identifier != central.identifier {
            emit(["event": "error", "code": "second_central_rejected"])
            return
        }
        self.central = central
        emit(["event": "subscribed", "mtu": min(central.maximumUpdateValueLength, 160) + 3])
        drain()
    }
    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral, didUnsubscribeFrom characteristic: CBCharacteristic) {
        guard self.central?.identifier == central.identifier else { return }
        self.central = nil
        pending.removeAll()
        emit(["event": "disconnected"])
    }
    func peripheralManager(_ peripheral: CBPeripheralManager, didReceiveWrite requests: [CBATTRequest]) {
        for request in requests {
            guard central?.identifier == request.central.identifier else {
                peripheral.respond(to: request, withResult: .insufficientAuthorization); continue
            }
            guard request.characteristic.uuid == rxID, request.offset == 0, let value = request.value, value.count <= 512 else {
                peripheral.respond(to: request, withResult: .invalidAttributeValueLength); continue
            }
            emit(["event": "write", "data": value.base64EncodedString()])
            peripheral.respond(to: request, withResult: .success)
        }
    }
    func peripheralManager(_ peripheral: CBPeripheralManager, didReceiveRead request: CBATTRequest) {
        request.value = Data()
        peripheral.respond(to: request, withResult: request.offset == 0 ? .success : .invalidOffset)
    }
    func peripheralManagerIsReady(toUpdateSubscribers peripheral: CBPeripheralManager) { drain() }
    func drain() {
        guard let central = central else { return }
        while let packet = pending.first {
            if !manager.updateValue(packet, for: tx, onSubscribedCentrals: [central]) { return }
            pending.removeFirst()
        }
    }
    func command(_ record: [String: Any]) {
        switch record["command"] as? String {
        case "notify":
            guard let text = record["data"] as? String, let data = Data(base64Encoded: text), data.count <= 160, pending.count < 512 else { return }
            pending.append(data); drain()
        case "stop_advertising":
            advertisingWanted = false; manager.stopAdvertising()
        case "stop":
            manager.stopAdvertising(); manager.removeAllServices(); NSApplication.shared.terminate(nil)
        default: break
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
guard CommandLine.arguments.count == 3 else { exit(2) }
let name = CommandLine.arguments[1]
guard name.range(of: "^MuseGadget[0-9A-F]{6}$", options: .regularExpression) != nil else { exit(2) }
let mode = CommandLine.arguments[2]
guard ["name-only", "name-and-service"].contains(mode) else { exit(2) }
let adapter = Peripheral(name: name, advertisementMode: mode)
DispatchQueue.global().async {
    while let line = readLine() {
        guard line.utf8.count <= 4096, let data = line.data(using: .utf8), let record = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { continue }
        DispatchQueue.main.async { adapter.command(record) }
    }
    DispatchQueue.main.async { NSApplication.shared.terminate(nil) }
}
app.run()
