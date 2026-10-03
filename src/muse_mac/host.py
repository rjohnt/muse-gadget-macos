# SPDX-License-Identifier: Apache-2.0
"""Pair a restricted Mac display gadget, then serve real Muse commands."""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import platform
import signal
import threading
import time
import urllib.parse
import webbrowser
from musegadget import config, identity, muse_api, network, service
from musegadget.ble_setup import ProvisionFailed, SetupController
from musegadget.pairing import PairingSession
from .commands import COMMANDS, DisplayExecutor, DisplayState
from .secrets import read_token
from .transport import MacTransport


class PreviewServer(ThreadingHTTPServer):
    daemon_threads = True


def serve_preview(state):
    html = Path(__file__).with_name('preview.html').read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                self.send_error(403)
                return
            path = urllib.parse.urlsplit(self.path).path
            if path == '/':
                body, kind = html, 'text/html; charset=utf-8'
            elif path == '/icons.js':
                body, kind = Path(__file__).with_name('icons.js').read_bytes(), 'text/javascript'
            elif path == '/state':
                body, kind = json.dumps(state.snapshot()).encode(), 'application/json'
            elif path == '/image':
                with state.lock:
                    body = state.image
                if body is None:
                    self.send_error(404)
                    return
                kind = 'image/png'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = PreviewServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class CurrentNetwork:
    is_online = staticmethod(network.is_online)

    @staticmethod
    def current_connection_entry():
        # Offer the connection already used by the Mac; never read/store SSIDs.
        return {'ssid': 'Use current connection', 'rssi': -40, 'secure': False}


def verify_and_save(credentials, commit):
    # The phone supplies a device token, never an SDK token used as a bearer.
    api = credentials.api_url_v2 or muse_api.API_BASE
    if api.rstrip('/') != muse_api.API_BASE:
        raise ProvisionFailed('auth_failed')
    host = credentials.noise_host or service.DEFAULT_NOISE_HOST
    if not host.endswith('.metaaivm.com') or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in host):
        raise ProvisionFailed('auth_failed')
    vms, _status = muse_api.fetch_vms_with_status(credentials.access_token, api)
    if not vms:
        raise ProvisionFailed('auth_failed')
    record = {'access_token': credentials.access_token, 'refresh_token': credentials.refresh_token,
              'api_url_v2': muse_api.API_BASE, 'noise_host': host,
              'access_token_saved_at': int(time.time()), 'token_type': 'device'}
    def save():
        config.save_json(config.PAIRING_FILE, record)
        return True
    if not commit(save):
        raise ProvisionFailed('error_storage')


async def run_connection(ident, token, state, stop):
    # Retain the upstream reconnect/rotation logic; replace its default shell
    # registry with display commands before constructing the service.
    service.COMMAND_SPECS = COMMANDS
    client = service.Service(ident, DisplayExecutor(state), sdk_token=token,
                             display_name='Mac Pocket Preview')
    task = asyncio.create_task(client.run())
    try:
        while not stop.is_set() and not task.done():
            session = client._current
            registered = bool(session and session.registered_at)
            paired = config.load_json(config.PAIRING_FILE) is not None
            with state.lock:
                advertising = state.data['bluetooth'] == 'advertising'
            state.set(connection='connected' if registered else 'connecting' if paired else 'ready to pair' if advertising else 'not paired')
            await asyncio.sleep(0.5)
        client.stop()
        await task
    finally:
        client.stop()
        if not task.done():
            task.cancel()
        state.set(connection='stopped')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['pair', 'run', 'preview'])
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--state-dir', type=Path, default=Path.home() / 'Library/Application Support/MuseMacGadget')
    parser.add_argument('--native-app', type=Path, default=Path('build/Muse Mac Gadget.app'))
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--advertisement', choices=['name-only', 'name-and-service'], default='name-only',
                        help='Prioritize the full name, or advertise the name and 128-bit service UUID')
    parser.add_argument('--timeout', type=int, default=600, help='Pairing window in seconds, 30–600')
    args = parser.parse_args()
    if platform.system() != 'Darwin' and args.mode != 'preview':
        parser.error('Pairing and live sessions require macOS')
    if not 30 <= args.timeout <= 600:
        parser.error('Pairing timeout must be 30–600 seconds')
    # Upstream log exceptions can include account identifiers/URLs. Suppress
    # them entirely; the preview displays only whitelisted transport states.
    logging.disable(logging.CRITICAL)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    state = DisplayState()
    state.set(connection='offline preview' if args.mode == 'preview' else 'not paired')
    transport = controller = None
    timer = None
    server = None
    try:
        token = None if args.mode == 'preview' else read_token(args.env_file)
        if args.mode != 'preview':
            args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if args.state_dir.is_symlink() or args.state_dir.stat().st_uid != os.getuid():
                raise ValueError('State directory must be owned by you and not a symlink')
            os.chmod(args.state_dir, 0o700)
            os.environ[config.STATE_DIR_ENV] = str(args.state_dir.resolve())
            ident = identity.load_or_create()
            state.set(device_name=ident.ble_name)
            if args.mode == 'pair' and config.load_json(config.PAIRING_FILE):
                raise ValueError('Already paired; use run. Use a different state directory to create another gadget.')
            if args.mode == 'run' and not config.load_json(config.PAIRING_FILE):
                raise ValueError('Not paired yet; use pair first')
        if args.mode == 'pair':
            executable = args.native_app.resolve() / 'Contents/MacOS/MuseMacPeripheral'
            if not executable.is_file():
                raise ValueError('Build the native adapter first: scripts/build-native.sh')
            pairing = PairingSession(node_id=ident.node_id, device_id=ident.device_id, mac=ident.mac,
                                     firmware_version='0.1.0-macos', sdk_token=token)
            def on_state(event):
                kind = event.get('event')
                if kind == 'bluetooth':
                    status = event.get('state')
                    if status in ('powered_on', 'powered_off', 'unauthorized', 'unsupported', 'resetting', 'unknown'):
                        state.set(bluetooth=status)
                elif kind == 'advertising':
                    state.set(bluetooth='advertising', connection='ready to pair')
                elif kind == 'subscribed':
                    state.set(phone_connected=True)
                elif kind == 'disconnected':
                    state.set(phone_connected=False)
                elif kind == 'error':
                    state.set(bluetooth='transport error')
                    stop.set()
                elif kind == 'stopped':
                    state.set(bluetooth='setup complete' if config.load_json(config.PAIRING_FILE) else 'stopped')
                    if not config.load_json(config.PAIRING_FILE):
                        stop.set()
            transport = MacTransport(executable, ident.ble_name,
                on_write=lambda packet: controller.on_write(packet),
                on_disconnect=lambda: controller.on_disconnect(), on_state=on_state,
                advertisement=args.advertisement)
            def completed():
                state.set(connection='paired; connecting')
                if timer:
                    timer.cancel()
                # Allow auth_ok notifications to leave before closing BLE.
                threading.Timer(2, transport.stop).start()
            class ObservedSetup(SetupController):
                def _handle_client_finished(self, command):
                    super()._handle_client_finished(command)
                    state.set(pairing_confirmed=self._pairing.confirmed)
            controller = ObservedSetup(pairing=pairing, identity=ident, version='0.1.0-macos',
                transport=transport, network=CurrentNetwork(), provision=verify_and_save, on_complete=completed)
            controller.start()
            transport.start()
            def expire():
                state.set(bluetooth='pairing window closed')
                transport.stop()
                if not config.load_json(config.PAIRING_FILE):
                    stop.set()
            timer = threading.Timer(args.timeout, expire)
            timer.daemon = True
            timer.start()
            print(f'In Muse: Settings > Devices > Add Device > {ident.ble_name}', flush=True)
        server = serve_preview(state)
        url = f'http://127.0.0.1:{server.server_port}/'
        print(f'Preview: {url}', flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        if args.mode == 'preview':
            stop.wait()
        else:
            asyncio.run(run_connection(ident, token, state, stop))
    except (ValueError, OSError) as exc:
        # Only our deliberate validation messages are printed, never arbitrary
        # OS/network paths or upstream exception text.
        print(str(exc) if isinstance(exc, ValueError) else 'Local adapter failed to start; check build and permissions.', flush=True)
        return 1
    except Exception:
        print('Adapter stopped unexpectedly. No credentials were logged.', flush=True)
        return 1
    finally:
        stop.set()
        if timer:
            timer.cancel()
        if controller:
            controller.stop()
        if transport:
            transport.stop()
        if server:
            server.shutdown()
            server.server_close()
    return 0
