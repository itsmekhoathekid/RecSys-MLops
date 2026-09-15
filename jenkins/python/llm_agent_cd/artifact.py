"""Download public HTTPS model bytes and verify before llama.cpp can start."""

import hashlib
import sys
import urllib.request
from pathlib import Path


def download(uri, expected, destination):
    if not uri.startswith("https://"):
        raise ValueError("only public HTTPS artifacts are supported")
    target = Path(destination)
    temporary = target.with_suffix(".partial")
    checksum = hashlib.sha256()
    with (
        urllib.request.urlopen(uri, timeout=60) as response,
        temporary.open("wb") as output,
    ):
        while chunk := response.read(1024 * 1024):
            checksum.update(chunk)
            output.write(chunk)
    if checksum.hexdigest() != expected:
        temporary.unlink()
        raise ValueError("artifact SHA256 mismatch")
    temporary.replace(target)


if __name__ == "__main__":
    download(*sys.argv[1:])
