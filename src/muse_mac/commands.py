# SPDX-License-Identifier: Apache-2.0
"""Explicitly registered display commands; no shell, file, or OTA commands."""
from copy import deepcopy
from datetime import datetime, timezone
import io
import ipaddress
import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from PIL import Image


def spec(description, fields):
    return {'description': description, 'required': {key: {'type': kind, 'description': help_text}
            for key, kind, help_text in fields}, 'optional': {}}


COMMANDS = {
    'display.set_caption': spec('Set the caption on the Mac test preview. Plain text, at most 240 UTF-8 bytes.', [('text', 'string', 'Short caption')]),
    'display.draw_url': spec('Download a public HTTPS character image and render it on the Mac test preview.', [('url', 'string', 'HTTPS image URL')]),
    'display.get_status': spec('Verify the Mac display gadget: connection state, received command count, last command; no private display content.', []),
}


def timestamp(value):
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError('Timestamp requires ISO 8601 with a timezone')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Timestamp requires ISO 8601 with a timezone') from None
    if parsed.tzinfo is None:
        raise ValueError('Timestamp requires a timezone')
    return parsed


def text(value, maximum):
    if not isinstance(value, str) or len(value.encode('utf-8')) > maximum:
        raise ValueError('Text is missing or too long')
    return value


def validate_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Image URL must be public HTTPS without embedded credentials')
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in addresses):
        raise ValueError('Image host must resolve to public addresses')
    return url


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_image(url):
    validate_url(url)
    opener = urllib.request.build_opener(PublicRedirect())
    with opener.open(urllib.request.Request(url, headers={'User-Agent': 'muse-gadget-macos/0.1'}), timeout=20) as response:
        data = response.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise ValueError('Image exceeds 5 MB')
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError('Image dimensions too large')
        image.thumbnail((480, 480))
        canvas = Image.new('RGBA', (480, 480), (255, 255, 255, 0))
        rgba = image.convert('RGBA')
        canvas.paste(rgba, ((480-image.width)//2, (480-image.height)//2))
        paper = Image.new('RGBA', canvas.size, 'white')
        monochrome = Image.alpha_composite(paper, canvas).convert('L').convert('1').convert('RGBA')
        monochrome.putalpha(canvas.getchannel('A'))
        output = io.BytesIO()
        monochrome.save(output, format='PNG')
    return output.getvalue()


class DisplayState:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {'connection': 'not paired', 'bluetooth': 'not started', 'caption': 'Waiting for your Muse.',
                     'phone_connected': False, 'pairing_confirmed': False,
                     'commands_received': 0, 'last_command': None,
                     'image_revision': 0, 'device_name': None}
        self.image = None

    def set(self, **values):
        with self.lock:
            self.data.update(values)

    def snapshot(self):
        with self.lock:
            result = deepcopy(self.data)
        return result


class DisplayExecutor:
    def __init__(self, state):
        self.state = state

    def run(self, command, params, timeout_ms=None):
        if command not in COMMANDS:
            return {'ok': False, 'error': 'Command not supported'}
        try:
            if command == 'display.get_status':
                snapshot = self.state.snapshot()
                return {'ok': True, 'payload': {key: snapshot[key] for key in ('connection', 'commands_received', 'last_command')}}
            if command == 'display.set_caption':
                self.state.set(caption=text(params.get('text'), 240))
            elif command == 'display.draw_url':
                image = download_image(text(params.get('url'), 4096))
                with self.state.lock:
                    self.state.image = image
                    self.state.data['image_revision'] += 1
            with self.state.lock:
                self.state.data['commands_received'] += 1
                self.state.data['last_command'] = command
            return {'ok': True, 'payload': {'accepted': True}}
        except (ValueError, KeyError, TypeError):
            return {'ok': False, 'error': 'Invalid display payload; check the registered command schema'}
        except Exception:
            # Network/image exceptions can contain signed URLs. Never echo them.
            return {'ok': False, 'error': 'Image or display update failed; previous content preserved'}
