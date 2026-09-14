"""Export the tested image and allowlisted host files locally. Never uploads anything."""

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST_FILES = (
    "deploy/compose.yaml",
    "deploy/compose.host.yaml",
    "deploy/start-host.sh",
    "deploy/relay-compose.service",
    "deploy/install-release.sh",
)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_release(root=ROOT):
    manifest = json.loads((root / "docs/deployment-release-manifest.json").read_text())
    for relative, expected in manifest["source_sha256"].items():
        path = root / relative
        if path.is_symlink() or sha256(path) != expected:
            raise ValueError(f"Release changed since rehearsal: {relative}")
    for relative in HOST_FILES:
        if (root / relative).is_symlink():
            raise ValueError(f"Host file cannot be a symlink: {relative}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = verify_release()
    image_id = manifest["image_id"]
    inspection = json.loads(subprocess.check_output(["docker", "image", "inspect", image_id]))[0]
    if (inspection["Id"], inspection["Architecture"], inspection["Os"]) != (
        image_id,
        "amd64",
        "linux",
    ):
        raise ValueError("Image does not match the tested release")
    with tempfile.TemporaryDirectory(prefix="relay-release-") as directory:
        staging = Path(directory)
        for relative in HOST_FILES:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
        (staging / "IMAGE_ID").write_text(image_id + "\n")
        subprocess.run(
            ["docker", "save", "--output", str(staging / "image.tar"), image_id], check=True
        )
        members = [*HOST_FILES, "IMAGE_ID", "image.tar"]
        sums = {relative: sha256(staging / relative) for relative in members}
        (staging / "SHA256SUMS").write_text(
            "".join(f"{checksum}  {name}\n" for name, checksum in sums.items())
        )
        archive = args.output / "release.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for relative in [*members, "SHA256SUMS"]:
                tar.add(staging / relative, arcname=relative, recursive=False)
        report = {
            "image_id": image_id,
            "archive_sha256": sha256(archive),
            "archive_bytes": archive.stat().st_size,
            "members": sums,
            "uploaded": False,
            "source_verified_against_rehearsal": True,
        }
        (args.output / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
