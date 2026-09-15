"""Deterministic helpers for Helm OCI release artifacts."""

from __future__ import annotations

import argparse
import copy
import gzip
import io
import tarfile
from pathlib import Path, PurePosixPath


def _validate_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe Helm package member: {member.name}")
    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
        raise ValueError(f"unsupported Helm package member: {member.name}")


def normalize_package(path: Path) -> None:
    """Rewrite a Helm-generated archive with stable order and metadata."""

    members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(path, mode="r:gz") as source:
        for member in source.getmembers():
            _validate_member(member)
            extracted = source.extractfile(member) if member.isfile() else None
            members.append((member, extracted.read() if extracted else None))

    temporary = path.with_name(f".{path.name}.normalized")
    with temporary.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as zipped:
            with tarfile.open(
                fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
                for original, content in sorted(members, key=lambda item: item[0].name):
                    member = copy.copy(original)
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    member.mtime = 0
                    member.pax_headers = {}
                    if member.isdir():
                        member.mode = 0o755
                    elif member.isfile():
                        member.mode = 0o755 if original.mode & 0o111 else 0o644
                    archive.addfile(
                        member,
                        io.BytesIO(content) if content is not None else None,
                    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    normalize_package(args.package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
