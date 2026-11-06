import asyncio
import errno
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from starlette.staticfiles import StaticFiles


MAX_ARCHIVE_BYTES = 96 * 1024 * 1024
MAX_UNPACKED_BYTES = 192 * 1024 * 1024
WEB_FILES = ("index.html", "js/bootstrap.js", "js/app.min.js")
EDITOR_CSP = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data: blob:; media-src 'self' data: blob:; "
    "worker-src 'self' blob:; frame-src 'self' blob:; frame-ancestors 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'"
)


@dataclass(frozen=True)
class AssetSettings:
    cache_dir: Path
    object_key: str
    sha256: str

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("Draw.io sha256 must be 64 lowercase hexadecimal characters")
        if not self.object_key:
            raise ValueError("Draw.io object key is required")

    @property
    def web_dir(self) -> Path:
        return self.cache_dir / self.sha256 / "web"


def try_lock(file) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        return False
    return True


def check_release(path: Path) -> None:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or not manifest.get("drawio_version"):
        raise ValueError("Invalid Draw.io release manifest")
    for name in WEB_FILES:
        if not (path / "web" / name).is_file():
            raise ValueError(f"Draw.io release is missing web/{name}")
    if not (path / "licenses" / "DRAWIO-LICENSE.txt").is_file():
        raise ValueError("Draw.io release is missing its license")


def unpack_release(content: bytes, destination: Path) -> None:
    total_size = 0
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(content), mode="r|gz") as archive:
        for member in archive:
            name = member.name
            path = PurePosixPath(name)
            if (not path.parts or str(path) != name or ".." in path.parts
                    or any(c in name for c in ("\\", ":", "\x00"))
                    or (name != "manifest.json" and path.parts[0] not in {"web", "licenses"})
                    or name.lower() in seen or len(seen) >= 20000):
                raise ValueError(f"Invalid Draw.io archive path: {name!r}")
            seen.add(name.lower())
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile() or member.size < 0:
                raise ValueError(f"Invalid Draw.io archive entry: {name!r}")
            total_size += member.size
            if total_size > MAX_UNPACKED_BYTES:
                raise ValueError("Draw.io archive exceeds the unpacked size limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
    check_release(destination)


async def prepare_assets(
    settings: AssetSettings, load_object: Callable[[str], Awaitable[bytes]],
) -> None:
    """Run once per lifespan. One process downloads while the others wait."""
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    release = settings.web_dir.parent
    with (settings.cache_dir / f"{settings.sha256}.lock").open("a+b") as lock:
        async with asyncio.timeout(120):
            while not try_lock(lock):
                await asyncio.sleep(0.2)
            if release.exists():
                check_release(release)
                return
            content = await load_object(settings.object_key)
            if not isinstance(content, bytes) or len(content) > MAX_ARCHIVE_BYTES:
                raise ValueError("Invalid Draw.io object or compressed size exceeds 96 MiB")
            if hashlib.sha256(content).hexdigest() != settings.sha256:
                raise ValueError("Draw.io object SHA-256 mismatch")
            with TemporaryDirectory(prefix=".drawio-", dir=settings.cache_dir) as staging:
                unpacked = Path(staging) / "release"
                unpacked.mkdir()
                unpack_release(content, unpacked)
                unpacked.rename(release)


class EditorAssets(StaticFiles):
    def __init__(self, settings: AssetSettings):
        super().__init__(directory=settings.web_dir, check_dir=False)

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = EDITOR_CSP
        return response
