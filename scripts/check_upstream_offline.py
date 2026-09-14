"""Run selected upstream tests with macOS file/network isolation and dummy credentials."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PIN = "be3f65c0dac8e99b68641083aa74d13103779bb3"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists")
    root = Path(__file__).resolve().parents[1]
    upstream = root / ".data/surplus-router-review"
    pin = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    if pin != PIN:
        parser.error("unexpected upstream version")
    with tempfile.TemporaryDirectory(prefix="relay-upstream-") as temporary:
        scratch = Path(temporary).resolve()
        allowed = [
            "/System",
            "/usr",
            "/opt/homebrew",
            "/Library",
            "/dev",
            "/private/etc",
            "/private/var/db",
            str(Path(sys.base_prefix).resolve()),
            str(root / ".venv"),
            str(upstream),
            str(scratch),
        ]
        profile = "\n".join(
            [
                "(version 1)",
                "(allow default)",
                "(deny network*)",
                "(deny file-read*)",
                "(deny file-write*)",
                "(allow file-read-metadata)",
                '(allow file-read-data (literal "/"))',
                '(allow file-write* (literal "/dev/null"))',
                *[f"(allow file-read* (subpath {json.dumps(p)}))" for p in allowed],
                f"(allow file-write* (subpath {json.dumps(str(scratch))}))",
            ]
        )
        policy = scratch / "isolation.sb"
        policy.write_text(profile)
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(scratch),
            "TMPDIR": str(scratch),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "AWS_ACCESS_KEY_ID": "offline",
            "AWS_SECRET_ACCESS_KEY": "offline",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_SHARED_CREDENTIALS_FILE": str(scratch / "missing"),
            "AWS_CONFIG_FILE": str(scratch / "missing"),
        }
        prefix = ["/usr/bin/sandbox-exec", "-f", str(policy), sys.executable]
        # Verify isolation before loading upstream code. No secret bytes are read.
        probe = """import pathlib,socket,sys
for path in sys.argv[1:]:
    try:
        pathlib.Path(path).open('rb')
    except PermissionError:
        continue
    raise RuntimeError('Sensitive read was not blocked')
try:
    socket.socket().connect(('127.0.0.1',5173))
except PermissionError:
    pass
else:
    raise RuntimeError('Network was not blocked')
print('isolation verified')
"""
        with tempfile.NamedTemporaryFile(
            dir=root / ".data", prefix="isolation-", delete=False
        ) as canary:
            canary.write(b"nonsecret isolation probe")
            sentinel = Path(canary.name)
        try:
            verified = subprocess.run(
                prefix + ["-c", probe, str(sentinel), str(root / ".env")],
                cwd=scratch,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        finally:
            sentinel.unlink()
        if verified.returncode:
            raise RuntimeError(
                f"Isolation probe failed ({verified.returncode}): "
                + verified.stdout
                + verified.stderr
            )
        selected = ["tests/test_claim_compensation.py", "tests/test_e2e_runtime_crash_recovery.py"]
        result = subprocess.run(
            prefix + ["-m", "pytest", "-q", "-p", "no:cacheprovider", *selected],
            cwd=upstream,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        report = {
            "upstream_commit": pin,
            "selected_tests": selected,
            "scope": "Selected upstream mock tests only; not a shared benchmark or real AWS test. Uses Relay installed dependencies, not upstream pins.",
            "isolation": verified.stdout.strip(),
            "python": sys.version,
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        with args.output.open("x") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        print(result.stdout)
        print(result.stderr)
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
