"""Owner-only token files; never evaluate shell syntax or export the token."""
import os
from pathlib import Path
import re
import stat

TOKEN_PATTERN = re.compile(r"mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")


def read_token(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Token file must be a regular file owned by you")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Token file must have owner-only permissions (chmod 600)")
        if info.st_size > 4096:
            raise ValueError("Token file is too large")
        entries = [line.strip() for line in stream if line.strip() and not line.lstrip().startswith("#")]
    if len(entries) != 1:
        raise ValueError("Expected exactly one MUSE_SDK_TOKEN entry")
    match = re.fullmatch(r'MUSE_SDK_TOKEN=(?:"([^"\r\n]*)"|\x27([^\x27\r\n]*)\x27|([^\s"\x27]*))', entries[0])
    if not match:
        raise ValueError("Invalid token file syntax")
    token = next(value for value in match.groups() if value is not None)
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Missing or invalid Muse SDK token")
    return token
