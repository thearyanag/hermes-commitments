"""Export only reviewed source files; no Git, network, or credentials required."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile


def release_files(root):
    root = root.resolve()
    paths = []
    for line in (root / "release-files.txt").read_text().splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or str(rel) != name:
            raise ValueError(f"Invalid release path: {name}")
        path = root / name
        if any(
            (root.joinpath(*rel.parts[:i])).is_symlink()
            for i in range(1, len(rel.parts) + 1)
        ):
            raise ValueError(f"Symlink in release path: {name}")
        if not path.is_file():
            raise ValueError(f"Missing release file: {name}")
        if name in paths:
            raise ValueError(f"Duplicate release path: {name}")
        # Public exports are reviewable UTF-8 source, not binary payloads.
        data = path.read_bytes()
        data.decode("utf-8")
        if b"\x00" in data:
            raise ValueError(f"Binary release file: {name}")
        paths.append(name)
    return sorted(paths)


def archive(path, files):
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as zipped:
            with tarfile.open(
                fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT
            ) as tar:
                for name, data in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(data))


def plugin_files(names):
    """Runtime source and documentation, without developer tests or tooling."""
    return [
        name
        for name in names
        if name
        in {
            "plugin.yaml",
            "__init__.py",
            "ledger.py",
            "policy.py",
            "host.py",
            "runtime.py",
            "desktop_bridge.py",
            "desktop/plugin.js",
            "LICENSE",
        }
        or name.endswith(".md")
    ]


def build(root, output):
    root = root.resolve()
    files = {name: (root / name).read_bytes() for name in release_files(root)}
    match = re.search(
        r"^version: ['\"]?(\d+\.\d+\.\d+)['\"]?\s*$",
        files["plugin.yaml"].decode(),
        re.MULTILINE,
    )
    if not match:
        raise ValueError("Expected a numeric manifest release version")
    version = match.group(1)
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    for name, data in files.items():
        dest = source / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    source_name = f"hermes-commitments-{version}-source.tar.gz"
    plugin_name = f"commitments-{version}.tar.gz"
    archive(
        output / source_name,
        {f"hermes-commitments-{version}/{n}": d for n, d in files.items()},
    )
    archive(
        output / plugin_name,
        {f"commitments/{n}": files[n] for n in plugin_files(files)},
    )
    inventory = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    (output / "source-manifest.json").write_text(
        json.dumps({"version": version, "files": inventory}, indent=2) + "\n"
    )
    artifacts = [source_name, plugin_name, "source-manifest.json"]
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256((output / name).read_bytes()).hexdigest()}  {name}\n"
            for name in artifacts
        )
    )
    return source


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist/release"),
        help="New output directory; existing paths are never overwritten",
    )
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parents[1], args.output))
