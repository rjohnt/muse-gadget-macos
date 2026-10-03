# SPDX-License-Identifier: Apache-2.0
"""CoreBluetooth bridge. Only BLE packets cross the child-process pipes."""
import base64
import json
import subprocess
import threading
import time
from musegadget.ble_framing import CHUNK_STAGGER_S


class MacTransport:
    def __init__(self, executable, name, on_write, on_disconnect, on_state):
        self.executable, self.name = executable, name
        self.on_write, self.on_disconnect, self.on_state = on_write, on_disconnect, on_state
        self._mtu = 23
        self._lock = threading.Lock()
        self._process = None
        self._closed = threading.Event()

    def start(self):
        self._process = subprocess.Popen([str(self.executable), self.name], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         text=True, bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self._process.stdout:
                if len(line) > 4096:
                    continue
                try:
                    event = json.loads(line)
                    kind = event.get('event')
                    if kind == 'write':
                        self.on_write(base64.b64decode(event['data'], validate=True))
                    elif kind == 'subscribed':
                        self._mtu = max(23, min(163, int(event['mtu'])))
                        self.on_state(event)
                    elif kind == 'disconnected':
                        self._mtu = 23
                        self.on_disconnect()
                        self.on_state(event)
                    else:
                        self.on_state(event)
                except (ValueError, KeyError, TypeError):
                    self.on_state({'event': 'error', 'code': 'transport_record_invalid'})
        finally:
            self._closed.set()
            self.on_state({'event': 'stopped'})

    def command(self, record):
        with self._lock:
            if self._process and self._process.poll() is None:
                try:
                    self._process.stdin.write(json.dumps(record, separators=(',', ':')) + '\n')
                    self._process.stdin.flush()
                except (BrokenPipeError, OSError):
                    self._closed.set()

    def send_packets(self, packets):
        for packet in packets:
            if self._closed.is_set():
                raise ConnectionError('Bluetooth transport closed')
            self.command({'command': 'notify', 'data': base64.b64encode(packet).decode()})
            time.sleep(CHUNK_STAGGER_S)

    def mtu(self):
        return self._mtu

    def disconnect(self, delay=0):
        # CoreBluetooth peripherals cannot cancel a central connection. Tear
        # down the service; a failed setup can be retried by restarting pairing.
        threading.Timer(delay, self.stop).start()

    def stop_advertising(self):
        self.command({'command': 'stop_advertising'})

    def stop(self):
        self.command({'command': 'stop'})
        self._closed.set()
        if self._process:
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.terminate()
