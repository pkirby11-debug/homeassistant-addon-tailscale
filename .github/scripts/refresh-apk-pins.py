#!/usr/bin/env python3
"""Refresh the exact Alpine package pins in the add-on Dockerfile.

The Dockerfile pins every apk package to an exact version. Alpine drops older
revisions from its index as packages are rebuilt, so a pin that was valid when
it was written eventually stops resolving:

    ERROR: unable to select packages:
      bind-tools-9.20.27-r0:
        breaks: world[bind-tools=9.20.26-r0]

Because this fork has no `image:` key, the Supervisor builds the add-on on the
user's machine, so a stale pin breaks installs there as well as in CI.

Versions are resolved by running `apk search` inside the add-on's own base
image, which uses exactly the repositories the real build will use, rather than
guessing at an Alpine branch.

Only the amd64 base image is queried. Alpine keeps architectures in step, and
if they ever diverge the aarch64 build in the same workflow fails and the
commit is blocked, which is the safe outcome.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Matches e.g. "        bind-tools=9.20.26-r0 \" in the apk add block.
PIN_RE = re.compile(
    r"^(?P<indent> +)(?P<name>[a-z0-9][a-z0-9._+-]*)=(?P<version>[a-zA-Z0-9._+-]+)(?P<trail> *\\?)$"
)
VERSION_RE = re.compile(r"^[0-9][a-zA-Z0-9._+-]*-r[0-9]+$")


def base_image(build_yaml: Path, arch: str) -> str:
    """Read build_from.<arch> without requiring a YAML library on the runner."""
    in_block = False
    for line in build_yaml.read_text().splitlines():
        if re.match(r"^build_from:", line):
            in_block = True
            continue
        if line and not line[0].isspace():
            in_block = False
        if in_block:
            m = re.match(rf"^\s+{re.escape(arch)}:\s*(\S+)\s*$", line)
            if m:
                return m.group(1)
    raise SystemExit(f"error: no build_from entry for {arch} in {build_yaml}")


def read_pins(dockerfile: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in dockerfile.read_text().splitlines():
        m = PIN_RE.match(line)
        if m:
            pins[m.group("name")] = m.group("version")
    return pins


def _run(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    """Run a command, failing with the actual output rather than just a code."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"error: failed to {what} (exit {proc.returncode})\n"
            f"  command: {' '.join(cmd)}\n"
            f"  stdout : {proc.stdout.strip() or '<empty>'}\n"
            f"  stderr : {proc.stderr.strip() or '<empty>'}"
        )
    return proc


def query_available(image: str, packages: list[str]) -> str:
    _run(["docker", "pull", "--quiet", image], f"pull {image}")
    # The base image sets ENTRYPOINT ["/init"] (s6-overlay), so a bare
    # `docker run <image> apk ...` hands the arguments to s6 instead of running
    # apk, and the command fails. Override the entrypoint to invoke apk directly.
    #
    # `apk search -x` is an exact-name match, so "nginx" cannot match
    # "nginx-module-*" and each requested package yields at most one line.
    proc = _run(
        ["docker", "run", "--rm", "--entrypoint", "apk", image,
         "search", "--no-cache", "-x", *packages],
        f"query package versions from {image}",
    )
    return proc.stdout


def parse_available(output: str, packages: list[str]) -> dict[str, str]:
    """Turn "bind-tools-9.20.27-r0" lines into {"bind-tools": "9.20.27-r0"}."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        for name in packages:
            prefix = name + "-"
            if line.startswith(prefix):
                version = line[len(prefix):]
                # Guard against a longer package name matching a shorter one's
                # prefix; a real version always starts with a digit.
                if VERSION_RE.match(version) and name not in found:
                    found[name] = version
    return found


def rewrite(dockerfile: Path, updates: dict[str, str]) -> None:
    lines = dockerfile.read_text().splitlines(keepends=True)
    out = []
    for line in lines:
        stripped = line.rstrip("\n")
        m = PIN_RE.match(stripped)
        if m and m.group("name") in updates:
            newline = (
                f"{m.group('indent')}{m.group('name')}="
                f"{updates[m.group('name')]}{m.group('trail')}"
            )
            out.append(newline + ("\n" if line.endswith("\n") else ""))
        else:
            out.append(line)
    dockerfile.write_text("".join(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dockerfile", default="tailscale/Dockerfile", type=Path)
    ap.add_argument("--build-yaml", default="tailscale/build.yaml", type=Path)
    ap.add_argument("--arch", default="amd64")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument(
        "--apk-output",
        type=Path,
        help="read `apk search` output from a file instead of running docker (for tests)",
    )
    args = ap.parse_args()

    pins = read_pins(args.dockerfile)
    if not pins:
        raise SystemExit(f"error: no pinned packages found in {args.dockerfile}")
    packages = sorted(pins)

    if args.apk_output:
        raw = args.apk_output.read_text()
    else:
        raw = query_available(base_image(args.build_yaml, args.arch), packages)

    available = parse_available(raw, packages)

    missing = [p for p in packages if p not in available]
    if missing:
        raise SystemExit(
            "error: no version reported for: " + ", ".join(missing) +
            "\nRefusing to edit the Dockerfile from an incomplete package list."
        )

    updates = {name: available[name] for name in packages if available[name] != pins[name]}

    for name in packages:
        mark = "STALE" if name in updates else "ok"
        detail = f" -> {available[name]}" if name in updates else ""
        print(f"  {name:22s} {pins[name]:18s} {mark}{detail}")

    summary = ", ".join(f"{n} {pins[n]} -> {v}" for n, v in updates.items())
    print(f"\n{len(packages)} pins checked, {len(updates)} stale")

    if updates and not args.dry_run:
        rewrite(args.dockerfile, updates)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as fh:
            fh.write(f"changed={'true' if updates else 'false'}\n")
            fh.write(f"summary={summary}\n")
            fh.write(f"count={len(updates)}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
