"""Small RESP transport used by explicit Valkey maintenance commands."""

from __future__ import annotations

import socket
from collections.abc import Iterable, Sequence
from typing import Any


class RedisError(RuntimeError):
    pass


class RedisConnection:
    def __init__(self, endpoint: str, timeout: float = 60.0):
        host, separator, port = endpoint.rpartition(":")
        if not separator or not host or not port.isdigit():
            raise ValueError(f"endpoint must be HOST:PORT: {endpoint}")
        self.endpoint = endpoint
        self._socket = socket.create_connection((host, int(port)), timeout=timeout)
        self._stream = self._socket.makefile("rb")

    def close(self) -> None:
        self._stream.close()
        self._socket.close()

    def command(self, *parts: str) -> Any:
        return self.pipeline([parts])[0]

    def pipeline(self, commands: Sequence[Sequence[str]]) -> list[Any]:
        request = bytearray()
        for command in commands:
            encoded = [part.encode("utf-8") for part in command]
            request.extend(f"*{len(encoded)}\r\n".encode())
            for part in encoded:
                request.extend(f"${len(part)}\r\n".encode())
                request.extend(part)
                request.extend(b"\r\n")
        self._socket.sendall(request)
        return [self._read_response() for _ in commands]

    def _read_line(self) -> bytes:
        line = self._stream.readline()
        if not line.endswith(b"\r\n"):
            raise RedisError(f"truncated RESP reply from {self.endpoint}")
        return line[:-2]

    def _read_response(self) -> Any:
        prefix = self._stream.read(1)
        if prefix == b"+":
            return self._read_line().decode("utf-8")
        if prefix == b"-":
            raise RedisError(self._read_line().decode("utf-8"))
        if prefix == b":":
            return int(self._read_line())
        if prefix == b"$":
            length = int(self._read_line())
            if length == -1:
                return None
            payload = self._stream.read(length)
            if self._stream.read(2) != b"\r\n":
                raise RedisError(f"invalid bulk RESP reply from {self.endpoint}")
            return payload.decode("utf-8")
        if prefix == b"*":
            length = int(self._read_line())
            if length == -1:
                return None
            return [self._read_response() for _ in range(length)]
        raise RedisError(f"unsupported RESP prefix from {self.endpoint}: {prefix!r}")


def scan_records(
    client: RedisConnection,
    pattern: str,
    *,
    batch_size: int = 500,
) -> Iterable[tuple[str, str]]:
    cursor = "0"
    while True:
        reply = client.command("SCAN", cursor, "MATCH", pattern, "COUNT", "1000")
        cursor, keys = str(reply[0]), sorted(reply[1])
        for offset in range(0, len(keys), batch_size):
            batch = keys[offset : offset + batch_size]
            values = client.pipeline([("GET", key) for key in batch])
            for key, raw in zip(batch, values, strict=True):
                if raw is not None:
                    yield key, raw
        if cursor == "0":
            return
