#!/usr/bin/env python3
"""Generate the GitHub Release body from CHANGELOG.md — generate only, never publish.

Incident background: the three tags v1.10.3 / v1.11.0 / v1.11.1 had long been pushed to the public repo, but their
Release entries were never
created, and for two weeks the visitor homepage sidebar showed "Latest v1.10.2" (manually backfilled on 2026-07-30). The
previous round was exactly
the same pattern: the v1.10.0/1/2 tags were cut on 07-14 and their entries were only batch-backfilled on 07-21. The
cause is not negligence — every red line that
entered scripts/check_invariants.sh has not rotted, and everything that did not enter a machine check has rotted, while
"on release day you also have to create
the Release entry" existed only in human memory. This script solidifies the part of that which **can be done offline and
deterministically** (where the body
comes from, whether the version is right, how the command is written) into a single command.

Three deliberate choices:

1. **The body is a verbatim slice of the CHANGELOG section, with no stripping at all.** The slice range = after the
version heading line
   and before the next ``## [``; the heading line itself is not included, because the Release page already shows the tag
   and the date.
   Not stripping is a measured conclusion: the live v1.11.1 body is exactly such a verbatim slice (including the leading
   newline and the trailing
   blank lines, 15,597 characters); remove the whitespace and it can never match again — and "a newly generated body is
   byte-for-byte
   comparable with the existing live entry" is this script's only acceptance criterion; once comparability breaks,
   --check becomes a decoration.
2. **Never publish.** This repo's discipline: the release pipeline must be interruptible; commit / push / publish are
all
   left for a human to perform. The script only prints a directly pasteable ``gh release create``. That is also why
   --check warns rather than fails when it
   cannot reach the network: its job is to report, not to gatekeep.
3. **Checks only do what can be done offline.** The section exists, the version is consistent with the five places of
the I2 lockstep, the body is non-empty — none of these
   needs the network, so they run unconditionally. Checking whether an entry exists and whether its body is right
   requires the network, so that goes into the optional --check,
   and it may degrade gracefully. That is also exactly where "add a machine check for Release entries" cannot currently
   be done:
   check_invariants.sh must be runnable offline and deterministically.

Usage::

    python3 scripts/release_notes.py 1.11.1                    # extract the section → write the file → print the gh
    command
    python3 scripts/release_notes.py 1.11.1 --title "v1.11.1 — Truthful Ledgers"
    python3 scripts/release_notes.py --check 1.11.1             # compare byte-for-byte with the live Release body
    python3 scripts/release_notes.py 1.10.3 --backfill          # backfill an entry for a historical tag

Exit codes: 0=pass (including degrade-and-skip on network failure), 1=offline check failed / live entry missing / body
mismatch.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
# build/ is already in .gitignore; landing here does not pollute the working tree, and the path is stable enough to copy
# straight into a command.
DEFAULT_OUT_DIR = ROOT / "build" / "release-notes"
# The remote name of the public repo. origin is the private repo; Release entries are only created on the public repo.
PUBLIC_REMOTE = "public"

# The dash in a version heading line comes in two spellings (from 1.10.0 on the ASCII "-", up to 1.9.0 the Chinese "—"),
# so the tail of a heading line is always swallowed with [^\n]* and the date separator itself is never matched.
_HEADING_TMPL = r"^## \[{ver}\][^\n]*\n"
_NEXT_HEADING = re.compile(r"^## \[", re.M)
_ANY_HEADING = re.compile(r"^## \[([^\]]+)\]", re.M)
_SEMVER = re.compile(r"\d+\.\d+\.\d+")

# gh failures fall into two classes. **Anything unrecognized is treated as degradation**: a script whose only job is
# reporting should not crash on an unknown error,
# and even less should it render "I could not check" as "there is a problem live".
_DEGRADE = re.compile(
    r"dial tcp|no such host|timed? ?out|connection refused|network is unreachable"
    r"|could not resolve|certificate|tls|proxy|unexpected eof|not installed"
    r"|not logged in|authentication|bad credentials|gh auth",
    re.I,
)
_MISSING = re.compile(r"release not found|not found|404", re.I)


class OnlineUnavailableError(Exception):
    """The live state cannot be checked due to network/authentication/missing tooling — cannot check does not mean
    there is a problem; degrade and skip."""


# All three levels go to stdout, consistent with the other check scripts under scripts/. Splitting them to stderr would
# interleave them with stdout
# in a pipeline and scramble the order — and the first reader of this output is the Leader capturing output in a
# session, where order is causality.
def ok(msg: str) -> None:
    print(f"✅ {msg}")


def warn(msg: str) -> None:
    print(f"⚠️  {msg}")


def fail(msg: str) -> None:
    print(f"❌ {msg}")


# ── CHANGELOG section extraction ──────────────────────────────────────


def extract_section(text: str, version: str) -> str | None:
    """Take a verbatim slice of the version section; return None if the version is not found."""
    head = re.search(_HEADING_TMPL.format(ver=re.escape(version)), text, re.M)
    if head is None:
        return None
    rest = text[head.end() :]
    nxt = _NEXT_HEADING.search(rest)
    return rest if nxt is None else rest[: nxt.start()]


def known_versions(text: str) -> list[str]:
    """The version numbers that have appeared in the CHANGELOG, in file order (i.e. newest to oldest)."""
    return _ANY_HEADING.findall(text)


# ── I2 version lockstep in five places ───────────────────────────────


def read_version_sites() -> dict[str, str]:
    """Read the five version sources of truth of the I2 machine check. This set must be kept consistent with the one
    in check_invariants.sh."""
    sites: dict[str, str] = {}

    m = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    sites["pyproject.toml"] = m.group(1) if m else "?"

    m = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/aiteam/__init__.py").read_text(encoding="utf-8"))
    sites["src/aiteam/__init__.py"] = m.group(1) if m else "?"

    plugin_json = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
    sites["plugin/.claude-plugin/plugin.json"] = plugin_json.get("version") or "?"

    for rel in ("plugin/.claude-plugin/marketplace.json", ".claude-plugin/marketplace.json"):
        data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        plugins = data.get("plugins") or []
        sites[rel] = (plugins[0].get("version") if plugins else data.get("version")) or "?"

    return sites


# ── External commands ──────────────────────────────────────────────────


def _run(argv: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Run an external command, never raising. cwd is fixed at ROOT — this repo requires parallel sessions to isolate
    with git worktrees,
    and without anchoring the working directory it would go and read another checkout's remotes and tags."""
    try:
        proc = subprocess.run(
            argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: timed out after {timeout}s"
    return proc.returncode, proc.stdout, proc.stderr


def public_repo_slug() -> str | None:
    code, out, _ = _run(["git", "remote", "get-url", PUBLIC_REMOTE], timeout=15)
    if code != 0:
        return None
    url = out.strip().removesuffix(".git")
    m = re.search(r"[:/]([^/:]+/[^/]+)$", url)
    return m.group(1) if m else None


def tag_exists_locally(tag: str) -> bool:
    code, _, _ = _run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], timeout=15)
    return code == 0


def gh_json(args: list[str]) -> object | None:
    """Run gh and parse its JSON. Returning None = the object definitely does not exist live; raising
    OnlineUnavailableError = it cannot be checked."""
    code, out, err = _run(["gh", *args])
    if code == 0:
        return json.loads(out)
    msg = (err or out).strip() or f"gh exit code {code}"
    if code in (124, 127) or _DEGRADE.search(msg):
        raise OnlineUnavailableError(msg)
    if _MISSING.search(msg):
        return None
    raise OnlineUnavailableError(msg)


# ── --check: compare byte-for-byte with the live body ─────────────────


def _first_diff(a: str, b: str) -> int:
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            return i
    return min(len(a), len(b))


def _report_body_diff(version: str, local: str, online: str, slug: str, out_path: Path) -> None:
    i = _first_diff(local, online)
    lo, hi = max(0, i - 40), i + 40
    fail(
        f"The body differs from live: local {len(local)} characters / live {len(online)} characters,"
        f"the first difference is at character {i}"
    )
    print(f"   Local: {local[lo:hi]!r}")
    print(f"   Live: {online[lo:hi]!r}")
    same_normalized = local.replace("\r\n", "\n").strip() == online.replace("\r\n", "\n").strip()
    print(
        "   After normalization (removing CRLF and leading/trailing whitespace)"
        + (
            "they are equal — the difference is only in line endings/whitespace, and the GitHub rendering is unchanged"
            "(the v1.10.0/1/2 batch of entries was created from stripped slices, a historical difference)"
            if same_normalized
            else "they are still not equal — the bodies substantively differ, most likely because the CHANGELOG was "
                "edited after the entry was created"
        )
    )
    print(
        f"   Fix the live body (performed by a human): gh release edit v{version} --repo {slug} "
        f"--notes-file {shlex.quote(str(out_path))}"
    )


def _report_latest_flag(version: str, slug: str, is_newest: bool) -> int:
    """Verify the latest badge. Only when this version is the newest one in the CHANGELOG does not being latest count as
    a failure
    — the visitor homepage sidebar shows exactly this badge, and before 2026-07-30 it was stuck on v1.10.2 for two
    weeks."""
    try:
        rels = gh_json(["release", "list", "--repo", slug, "--limit", "30", "--json", "tagName,isLatest"])
    except OnlineUnavailableError as exc:
        warn(f"the latest badge could not be verified ({exc})")
        return 0
    if not rels:
        warn("the latest badge could not be verified: this repo has no Release entries at all")
        return 0

    latest = next((r["tagName"] for r in rels if r.get("isLatest")), None)
    tag = f"v{version}"
    if latest == tag:
        ok(f"latest badge = {tag}")
        return 0
    if is_newest:
        fail(
            f"the latest badge points at {latest or '(none)'}, while the newest version in the CHANGELOG is {tag} "
            "— the visitor homepage sidebar is showing the old version"
        )
        return 1
    print(f"ℹ️  latest badge = {latest or '(none)'} ({tag} is not the newest version, as expected)")
    return 0


def check_online(version: str, generated: str, slug: str, out_path: Path, is_newest: bool) -> int:
    tag = f"v{version}"
    try:
        data = gh_json(["release", "view", tag, "--repo", slug, "--json", "body,name,publishedAt"])
    except OnlineUnavailableError as exc:
        warn(f"--check skipped: cannot get the live Release ({exc}). The script only reports and does not gatekeep, so "
            f"this does not count as a failure.")
        return 0

    if data is None:
        fail(
            f"there is no Release entry for {tag} on {slug} — the tag was pushed but the entry was never created,"
            "exactly the class of omission that was manually backfilled on 2026-07-30"
        )
        return 1

    assert isinstance(data, dict)
    status = 0
    online_body: str = data.get("body") or ""
    if online_body == generated:
        ok(f"the body is byte-for-byte identical to live ({len(generated)} characters)")
    else:
        _report_body_diff(version, generated, online_body, slug, out_path)
        status = 1

    print(f"ℹ️  Live title: {data.get('name')!r} · Published at {data.get('publishedAt')}")
    return status or _report_latest_flag(version, slug, is_newest)


# ── Release command (print, do not execute) ──────────────────────────


def print_create_command(version: str, slug: str, out_path: Path, title: str, backfill: bool) -> None:
    tag = f"v{version}"
    print()
    print("Release command — performed by a human; this script does not publish:")
    print(f"  gh release create {tag} \\")
    print(f"    --repo {slug} \\")
    print("    --verify-tag \\")
    print(f"    --title {shlex.quote(title)} \\")
    # A repo path may contain spaces (this machine's checkout is under "AI team OS"), and a command that is not quoted
    # will be wrong the moment it is pasted.
    print(f"    --notes-file {shlex.quote(str(out_path))}")
    print()
    print(f"  · --verify-tag verifies whether the **remote** has {tag}; having the tag locally ≠ having it remotely,"
          f"if necessary first git push {PUBLIC_REMOTE} {tag} (also performed by a human)")
    print('  · The historical entry title convention is "v1.11.1 — <English subtitle>"; the subtitle is written by a '
        'human, the script does not fabricate it;'
          "pass it with --title")
    if backfill:
        print("  · When backfilling multiple entries, **create them one by one in ascending version order**: GitHub's "
            "latest badge looks at publish time,"
              "and creating in reverse order would push an old version up to latest")
    print(f"  · Verify immediately after creating: python3 scripts/release_notes.py --check {version}")


# ── Main flow ────────────────────────────────────────────────────────


def _resolve_version(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    raw = args.check or args.version
    if raw is None:
        parser.error("a version number is required: release_notes.py 1.11.1 or release_notes.py --check 1.11.1")
    if args.check and args.version and args.check.lstrip("v") != args.version.lstrip("v"):
        parser.error("--check and the positional argument gave two different versions")
    version = raw.lstrip("v")
    if not _SEMVER.fullmatch(version):
        parser.error(f"invalid version number format: {raw!r} (expected 1.11.1 or v1.11.1)")
    return version


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the GitHub Release body from CHANGELOG.md (generate only, never publish)"
    )
    parser.add_argument("version", nargs="?", help="version number, 1.11.1 or v1.11.1")
    parser.add_argument("--check", metavar="VERSION", help="compare byte-for-byte with the live Release body (degrade "
        "and skip on network failure)")
    parser.add_argument("--out", type=Path, help=f"body output path (default {DEFAULT_OUT_DIR}/v<version>.md)")
    parser.add_argument("--title", help='Release title (default "v<version>")')
    parser.add_argument("--repo", help=f"target repo owner/name (default: take it from git remote {PUBLIC_REMOTE})")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="backfill an entry for a historical version: explicitly declare that the version differing from the "
            "current tree's version is intentional",
    )
    args = parser.parse_args()
    version = _resolve_version(parser, args)

    if not CHANGELOG.exists():
        fail(f"cannot find {CHANGELOG}")
        return 1
    text = CHANGELOG.read_text(encoding="utf-8")

    # ① This version exists in the CHANGELOG
    section = extract_section(text, version)
    if section is None:
        versions = known_versions(text)
        fail(f"there is no {version} section in CHANGELOG.md — first complete the CHANGELOG, then create the Release "
            f"entry")
        print(f"   Existing versions (most recent 8): {', '.join(versions[:8])}")
        return 1

    # ② The body is non-empty
    if not section.strip():
        fail(f"the {version} section is empty — a Release entry with an empty body is the same as not creating it")
        return 1

    # ③ The version agrees with the five places of the I2 lockstep
    sites = read_version_sites()
    distinct = set(sites.values())
    if len(distinct) != 1:
        fail("I2 version drift in the five places; it must converge before releasing:")
        for k, v in sites.items():
            print(f"   {k} = {v}")
        return 1
    tree_version = distinct.pop()
    if tree_version != version:
        detail = f"the current tree's version is {tree_version}, and the one to generate is {version}"
        if args.check:
            warn(f"{detail} — --check is verifying a historical entry, continuing")
        elif args.backfill:
            warn(f"{detail} — --backfill has been explicitly declared, continuing")
        else:
            fail(f"{detail} — on release day this is usually a mistyped version number; if you really are backfilling "
                f"a historical entry, add --backfill")
            return 1

    # ④ Tag existence: checkable locally, not remotely (offline), so only a warning
    tag = f"v{version}"
    if not tag_exists_locally(tag):
        warn(f"there is no local tag {tag} — gh release create --verify-tag will fail; first create the tag and push "
            f"it to the remote")

    slug = args.repo or public_repo_slug()
    if slug is None:
        fail(f"cannot get the public repo address (git remote {PUBLIC_REMOTE} does not exist) — specify it explicitly "
            f"with --repo owner/name")
        return 1

    out_path = args.out or DEFAULT_OUT_DIR / f"{tag}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" is a hard requirement: on Windows the default newline translation would write \n as \r\n, and the
    # moment the body enters GitHub
    # it is no longer byte-for-byte comparable with the CHANGELOG, and --check would forever report a mismatch.
    out_path.write_text(section, encoding="utf-8", newline="\n")

    ok(
        f"{tag} body generated: {out_path}"
        f"({len(section)} characters / {len(section.splitlines())} lines, a verbatim slice from CHANGELOG.md)"
    )

    if args.check:
        is_newest = known_versions(text)[:1] == [version]
        return check_online(version, section, slug, out_path, is_newest)

    print_create_command(version, slug, out_path, args.title or tag, args.backfill)
    return 0


if __name__ == "__main__":
    sys.exit(main())
