# SPDX-License-Identifier: Apache-2.0
import io
import json
import os
from pathlib import Path
import stat
import urllib.error
import urllib.request
from unittest.mock import patch
import pytest
from muse_mac.commands import COMMANDS, DisplayExecutor, DisplayState, download_image
from muse_mac.host import serve_preview
from muse_mac.secrets import read_token
from PIL import Image


def test_private_token_file_and_shell_syntax(tmp_path):
    path = tmp_path / '.env'
    synthetic = 'mgst_' + 'A' * 43
    path.write_text('MUSE_SDK_TOKEN="' + synthetic + '"\n')
    path.chmod(0o600)
    assert read_token(path) == synthetic
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_token(path)
    path.chmod(0o600)
    path.write_text('MUSE_SDK_TOKEN="$(do-not-execute)"\n')
    with pytest.raises(ValueError):
        read_token(path)
    link = tmp_path / 'linked'
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_token(link)


def test_commands_restricted_and_failed_updates_preserve_previous():
    state = DisplayState()
    executor = DisplayExecutor(state)
    assert all(key not in COMMANDS for key in ('system.run', 'file.read', 'file.write', 'device.ota'))
    assert not executor.run('system.run', {'command': 'echo unsafe'})['ok']
    assert executor.run('pocket.set_status', {'text': 'SDK connection verified'})['ok']
    assert not executor.run('pocket.set_status', {'text': 'x' * 241})['ok']
    assert state.snapshot()['caption'] == 'SDK connection verified'
    state.image = b'previous'
    with patch('muse_mac.commands.download_image', side_effect=OSError('signed secret URL')):
        result = executor.run('display.draw_url', {'url': 'https://example.org/a.jpg'})
    assert not result['ok']
    assert 'secret' not in result['error']
    assert state.image == b'previous'
    assert state.snapshot()['commands_received'] == 1


def test_watch_contract_staleness_and_atomic_validation():
    state = DisplayState()
    executor = DisplayExecutor(state)
    payload = {'updated': '2000-01-01T10:00:00Z', 'items': [{'label': 'Example', 'state': 'watching', 'note': 'Awaiting update', 'checked': '2000-01-01T09:00:00Z'}]}
    assert executor.run('pocket.set_watch_digest', {'payload': json.dumps(payload)})['ok']
    assert state.snapshot()['watches']['stale']
    payload['items'][0]['checked'] = '2000-01-01T09:00'
    assert not executor.run('pocket.set_watch_digest', {'payload': json.dumps(payload)})['ok']
    assert state.snapshot()['watches']['items'][0]['checked'].endswith('Z')
    payload['items'] = []
    assert executor.run('pocket.set_watch_digest', {'payload': json.dumps(payload)})['ok']
    assert state.snapshot()['watches']['items'] == []


def test_event_expiry_and_timezone_requirement():
    state = DisplayState()
    executor = DisplayExecutor(state)
    payload = {'updated': '2000-01-01T10:00:00Z', 'title': 'Example', 'when': '2000-01-01T11:00:00Z', 'ends': '2000-01-01T11:30:00Z'}
    assert executor.run('pocket.set_next_up', {'payload': json.dumps(payload)})['ok']
    assert state.snapshot()['next_up'] is None
    payload['ends'] = '2000-01-01T10:00:00Z'
    assert not executor.run('pocket.set_next_up', {'payload': json.dumps(payload)})['ok']
    assert state.data['next_up']['ends'] == '2000-01-01T11:30:00Z'


def test_health_does_not_return_private_content():
    state = DisplayState()
    state.set(caption='private sample content')
    result = DisplayExecutor(state).run('pocket.get_status', {})
    assert result['ok']
    assert 'private sample' not in json.dumps(result)


def test_image_pipeline_is_bilevel_and_keeps_aspect_ratio():
    buf = io.BytesIO()
    Image.new('RGB', (200, 100), (128, 128, 128)).save(buf, format='JPEG')
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_args): self.close()
    class Opener:
        def open(self, *_args, **_kwargs): return Response(buf.getvalue())
    with patch('muse_mac.commands.validate_url'), patch('muse_mac.commands.urllib.request.build_opener', return_value=Opener()):
        output = download_image('https://example.org/image.jpg')
    image = Image.open(io.BytesIO(output))
    assert image.size == (480, 480)
    assert image.mode == '1'


def test_preview_read_only_no_token_and_invalid_host():
    server = serve_preview(DisplayState())
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urllib.request.urlopen(base + '/state') as response:
            snapshot = json.load(response)
            assert 'access_token' not in snapshot and 'sdk_token' not in snapshot
            assert response.headers['Cache-Control'] == 'no-store'
        bad = urllib.request.Request(base + '/state', headers={'Host': 'evil.example'})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(bad)
        assert error.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(base + '/state', data=b'{}'))
        assert error.value.code == 501
    finally:
        server.shutdown()
        server.server_close()


def test_transport_advertisement_modes_and_process_arguments(monkeypatch):
    from muse_mac.transport import MacTransport
    from unittest.mock import Mock
    popen = Mock()
    monkeypatch.setattr('muse_mac.transport.subprocess.Popen', popen)
    monkeypatch.setattr('muse_mac.transport.threading.Thread.start', lambda self: None)
    transport = MacTransport(Path('/test/native'), 'MuseGadget123456', lambda _: None, lambda: None, lambda _: None)
    transport.start()
    assert popen.call_args.args[0] == ['/test/native', 'MuseGadget123456', 'name-only']
    transport = MacTransport(Path('/test/native'), 'MuseGadget123456', lambda _: None, lambda: None, lambda _: None, advertisement='name-and-service')
    transport.start()
    assert popen.call_args.args[0][-1] == 'name-and-service'
    with pytest.raises(ValueError):
        MacTransport(Path('/test/native'), 'MuseGadget123456', lambda _: None, lambda: None, lambda _: None, advertisement='invalid')
