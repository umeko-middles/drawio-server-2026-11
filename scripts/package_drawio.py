"""Build the OBS artifact from an official draw.io checkout or Desktop app.asar."""

import argparse
import gzip
import hashlib
import io
import json
import re
import struct
import tarfile
from pathlib import Path


ASAR_WEB = "drawio/src/main/webapp/"
PRIVATE_DIRS = {"META-INF", "WEB-INF"}
POST_CONFIG = b"""
// doc-master offline deployment overrides (drawio-server-2026-11).
window.EXPORT_URL = null;
window.VSS_CONVERT_URL = null;
window.ICONSEARCH_PATH = null;
window.ICON_SERVICE_PATH = null;
"""
PRE_CONFIG = b"""
// doc-master offline deployment overrides (drawio-server-2026-11).
urlParams['offline'] = '1';
urlParams['stealth'] = '1';
urlParams['pwa'] = '0';
urlParams['plugins'] = '0';
"""


def from_asar(path: Path):
    with path.open("rb") as source:
        prefix, header_size, _, json_size = struct.unpack("<4I", source.read(16))
        if prefix != 4 or json_size > header_size - 8 or header_size > 16 * 1024 * 1024:
            raise ValueError("Invalid ASAR header")
        files = json.loads(source.read(json_size))["files"]

        def entries(directory, parent=""):
            for name, item in directory.items():
                full_name = parent + name
                if "files" in item:
                    yield from entries(item["files"], full_name + "/")
                else:
                    yield full_name, item

        def read(item):
            if "offset" not in item or item.get("unpacked"):
                raise ValueError("Expected a packed ASAR file")
            source.seek(header_size + 8 + int(item["offset"]))
            data = source.read(item["size"])
            if len(data) != item["size"]:
                raise ValueError("Truncated ASAR file")
            return data

        desktop_version = json.loads(read(files["package.json"]))["version"]
        license_files = {"drawio/LICENSE": "DRAWIO-LICENSE.txt", "LICENSE": "DESKTOP-LICENSE.txt"}
        web = {}
        licenses = {}
        version = None
        for name, item in entries(files):
            if name == "drawio/VERSION":
                version = read(item).decode().strip()
            elif name in license_files:
                licenses[license_files[name]] = read(item)
            elif name.startswith(ASAR_WEB):
                relative = name[len(ASAR_WEB):]
                if relative.split("/", 1)[0] not in PRIVATE_DIRS:
                    web[relative] = read(item)
        source.seek(0)
        source_info = {
            "url": f"https://github.com/jgraph/drawio-desktop/releases/tag/v{desktop_version}",
            "input": "resources/app.asar",
            "input_sha256": hashlib.file_digest(source, "sha256").hexdigest(),
        }
    return version, source_info, web, licenses


def from_checkout(path: Path, tag: str):
    version = (path / "VERSION").read_text().strip()
    if tag != f"v{version}":
        raise ValueError("Pass the official checkout's release tag as --tag")
    root = path / "src" / "main" / "webapp"
    web = {}
    for file in root.rglob("*"):
        relative = file.relative_to(root)
        if relative.parts[0] not in PRIVATE_DIRS and file.is_file() and not file.is_symlink():
            web[relative.as_posix()] = file.read_bytes()
    licenses = {"DRAWIO-LICENSE.txt": (path / "LICENSE").read_bytes()}
    if (path / "NOTICE").is_file():
        licenses["NOTICE.txt"] = (path / "NOTICE").read_bytes()
    source_info = {"url": f"https://github.com/jgraph/drawio/releases/tag/{tag}", "input": "src/main/webapp"}
    return version, source_info, web, licenses


def package(output: Path, source, revision: int) -> Path:
    version, source_info, web, licenses = source
    if not version or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or revision < 1:
        raise ValueError("Invalid Draw.io version or package revision")
    for required in ("index.html", "js/bootstrap.js", "js/app.min.js"):
        if required not in web:
            raise ValueError(f"Missing {required}")
    web["js/PreConfig.js"] += PRE_CONFIG
    web["js/PostConfig.js"] += POST_CONFIG
    manifest = {"schema": 1, "drawio_version": version, "revision": revision, "source": source_info}
    files = {
        "manifest.json": (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        **{f"licenses/{name}": data for name, data in licenses.items()},
        **{f"web/{name}": data for name, data in web.items()},
    }
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"drawio-web-{version}-r{revision}.tar.gz"
    with archive.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|") as tar:
            for name, data in sorted(files.items()):
                item = tarfile.TarInfo(name)
                item.size = len(data)
                item.mode = 0o644
                tar.addfile(item, io.BytesIO(data))
    with archive.open("rb") as raw:
        digest = hashlib.file_digest(raw, "sha256").hexdigest()
    archive.with_name(archive.name + ".sha256").write_bytes(f"{digest}  {archive.name}\n".encode())
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--asar", type=Path)
    source.add_argument("--checkout", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--revision", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("release"))
    args = parser.parse_args()
    if args.checkout and not args.tag:
        parser.error("--checkout requires --tag")
    release = from_asar(args.asar) if args.asar else from_checkout(args.checkout, args.tag)
    print(package(args.output, release, args.revision))
