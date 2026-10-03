#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check public Git content without printing credential matches."""
import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--include-untracked', action='store_true')
    parser.add_argument('--private-file', action='append', type=Path, default=[])
    args = parser.parse_args()
    sensitive = []
    for path in args.private_file:
        raw = path.read_text()
        if path.suffix == '.json':
            record = json.loads(raw)
            sensitive.extend(str(record[key]).encode() for key in ('access_token', 'refresh_token', 'username', 'mac') if record.get(key))
        else:
            sensitive.extend(match.encode() for match in re.findall(r'mgst_[A-Za-z0-9_-]{43}', raw))
    needles = [needle for value in sensitive for needle in
               (value, base64.b64encode(value), base64.urlsafe_b64encode(value), value.hex().encode())
               if len(needle) >= 8]
    paths = git('ls-files', '-z').decode().split('\0')
    if args.include_untracked:
        paths += git('ls-files', '--others', '--exclude-standard', '-z').decode().split('\0')
    paths = sorted(set(paths))
    blobs = [(ROOT / path).read_bytes() for path in paths if path and (ROOT / path).is_file()]
    try:
        objects = git('rev-list', '--objects', '--all', '--reflog').decode().splitlines()
    except subprocess.CalledProcessError:
        objects = []
    for line in objects:
        oid = line.split(' ', 1)[0]
        if git('cat-file', '-t', oid).strip() in (b'blob', b'commit', b'tag'):
            blobs.append(git('cat-file', '-p', oid))
    forbidden = re.compile(rb'mgst_[A-Za-z0-9_-]{43}|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
    failed = any(forbidden.search(blob) or any(value in blob for value in needles) for blob in blobs)
    email_pattern = re.compile(rb'[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
    emails = {m.lower() for blob in blobs for m in email_pattern.findall(blob)}
    def public_address(value):
        domain = value.rsplit(b'@', 1)[1]
        return domain in (b'users.noreply.github.com', b'noreply.github.com',
                          b'example.com', b'example.org', b'example.net')
    personal_emails = {value for value in emails if not public_address(value)}
    private_paths = any(re.search(rb'/(?:Users|home)/[A-Za-z0-9_.-]+/', blob) for blob in blobs)
    failed = failed or bool(personal_emails) or private_paths
    unsafe_paths = any(path and (path == '.env' or path.startswith(('private/', 'build/', '.venv/')) or path.endswith(('.bin', '.log'))) for path in paths)
    if failed or unsafe_paths:
        print('FAIL: private material or generated files detected. Matches withheld.')
        return 1
    print(f'PASS: {sum(bool(path) for path in paths)} public candidate paths, reachable/reflog history, and commit metadata checked; no private matches or personal emails.')
    print(f'Public noreply/example email addresses: {len(emails)} distinct; values withheld.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
