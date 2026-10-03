# SPDX-License-Identifier: Apache-2.0
"""Pair a restricted Mac display gadget, then serve real Muse commands."""
import argparse
from dataclasses import dataclass
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


@dataclass
class Application:
    """Optional application layer; the adapter owns transport, not product features."""
    caption_command: str = 'display.set_caption'
    state_factory: object = DisplayState
    executor_factory: object = DisplayExecutor
    command_specs: object = None
    preview_path: object = None
    routes: object = None
    configure_parser: object = None
    on_registered: object = None


class PreviewServer(ThreadingHTTPServer):
    daemon_threads = True


def serve_preview(state, preview_path=None, routes=None):
    html_path = Path(preview_path) if preview_path else Path(__file__).with_name('preview.html')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                self.send_error(403)
                return
            path = urllib.parse.urlsplit(self.path).path
            if path == '/':
                body, kind = html_path.read_bytes(), 'text/html; charset=utf-8'
            elif path == '/icons.js':
                body, kind = Path(__file__).with_name('icons.js').read_bytes(), 'text/javascript'
            elif path == '/state':
                body, kind = json.dumps(state.snapshot()).encode(), 'application/json'
            elif routes and path in routes:
                try:
                    body, kind = routes[path](urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))
                except (ValueError, KeyError):
                    self.send_error(400, 'Invalid preview request')
                    return
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


async def request_character(session, state, caption_command='display.set_caption'):
    state.set(avatar_request='pending')
    try:
        result = await session.send_chat(
            'Please send your actual existing Muse character/avatar to the Mac display gadget that sent this message. '
            'Call display.draw_url now with a public HTTPS URL to your existing avatar. '
            'Prefer a transparent-background PNG, ideally 480x480, showing only the avatar with no backdrop or text. '
            'If a transparent version is unavailable, send the existing avatar image first rather than withholding it. '
            f'Use your own character, not an unrelated placeholder. Then call {caption_command} with a short caption '
            'that does not repeat your name or introduce yourself. '
            'If you cannot access your character or either command fails, explain that in chat; do not claim success.'
        )
        state.set(avatar_request='sent' if result.get('ok') else 'failed')
    except Exception:
        state.set(avatar_request='failed')


async def run_connection(ident, token, state, stop, display_name='MacMuse', request_avatar=False, application=None, args=None):
    # Retain the upstream reconnect/rotation logic; replace its default shell
    # registry with display commands before constructing the service.
    application = application or Application()
    service.COMMAND_SPECS = COMMANDS if application.command_specs is None else application.command_specs
    client = service.Service(ident, application.executor_factory(state), sdk_token=token,
                             display_name=display_name)
    task = asyncio.create_task(client.run())
    requested = False
    application_requested = False
    try:
        while not stop.is_set() and not task.done():
            session = client._current
            registered = bool(session and session.registered_at)
            paired = config.load_json(config.PAIRING_FILE) is not None
            with state.lock:
                advertising = state.data['bluetooth'] == 'advertising'
            state.set(connection='connected' if registered else 'connecting' if paired else 'ready to pair' if advertising else 'not paired')
            if registered and request_avatar and not requested:
                requested = True
                await request_character(session, state, application.caption_command)
            if registered and application.on_registered and not application_requested:
                application_requested = True
                await application.on_registered(session, state, args)
            await asyncio.sleep(0.5)
        client.stop()
        await task
    finally:
        client.stop()
        if not task.done():
            task.cancel()
        state.set(connection='stopped')


def main(application=None):
    application = application or Application()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['pair', 'run', 'preview'])
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--state-dir', type=Path, default=Path.home() / 'Library/Application Support/MuseMacGadget')
    parser.add_argument('--native-app', type=Path, default=Path('build/Muse Mac Gadget.app'))
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--name', default='MacMuse', help='Friendly name in Muse and the preview; BLE discovery identity stays unchanged')
    parser.add_argument('--character-name', default='Your Muse', help='Heading below the character in the preview')
    parser.add_argument('--request-avatar', action='store_true', help='Ask Muse once, after registration, to send its actual character to this display')
    parser.add_argument('--advertisement', choices=['name-only', 'name-and-service'], default='name-only',
                        help='Prioritize the full name, or advertise the name and 128-bit service UUID')
    parser.add_argument('--timeout', type=int, default=600, help='Pairing window in seconds, 30–600')
    if application.configure_parser:
        application.configure_parser(parser)
    args = parser.parse_args()
    if not args.name.strip() or len(args.name.encode('utf-8')) > 80 or any(ord(c)<32 for c in args.name):
        parser.error('Friendly name must be 1–80 UTF-8 bytes without control characters')
    if not args.character_name.strip() or len(args.character_name.encode('utf-8')) > 80 or any(ord(c)<32 for c in args.character_name):
        parser.error('Character name must be 1–80 UTF-8 bytes without control characters')
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
    state = application.state_factory()
    state.set(connection='offline preview' if args.mode == 'preview' else 'not paired', display_name=args.name, character_name=args.character_name)
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
            if args.mode == 'run':
                state.set(pairing_confirmed=True, bluetooth='setup complete')
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
        server = serve_preview(state, application.preview_path, application.routes)
        url = f'http://127.0.0.1:{server.server_port}/'
        print(f'Preview: {url}', flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        if args.mode == 'preview':
            stop.wait()
        else:
            asyncio.run(run_connection(ident, token, state, stop, args.name, args.request_avatar, application, args))
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
