---
name: os-release
description: The complete checklist for releasing a new AI Team OS version — preflight, the seven version locksteps, bilingual (Chinese/English) CHANGELOG, dual dist builds, private-terminology scan, commit/tag, dual-repo push, creating the GitHub Release entry and verifying the latest badge, and post-release checks. Use when preparing a release, backfilling a missed Release entry, or verifying the online state of an already-released version.
---

# OS Release — Release Checklist

## Why this checklist exists

Every red line that made it into `scripts/check_invariants.sh` never rotted; the ones that did not make it into machine checks leaked twice, in exactly the same shape:

- The v1.10.0/1/2 tags were cut on 2026-07-14, but the GitHub Release entries were only backfilled in bulk on 07-21;
- The v1.10.3/v1.11.0/v1.11.1 tags were pushed on 07-27 / 07-27 / 07-29 respectively, and the Release entries were only filled in on 07-30 (measured: the three `publishedAt` values squeezed within 30 seconds), during which the visitor homepage sidebar showed `Latest v1.10.2` for two weeks.

What machine checks can govern does not need your memory — just run one command. This checklist governs only **what machine checks cannot**, and **who presses which button**.

## Division of execution (hard constraint: the release pipeline must be interruptible)

| Step | Executor |
| --- | --- |
| 0–6 Preparation and verification | Leader, full authority |
| 7 commit · 8 tag | Leader executes, but **first submit the `git diff --stat` and the drafted message to the user for approval** |
| 9 Dual-repo push · 10 Create the Release entry | **The user executes**. The Leader only prepares the commands and prints them |
| 11 Post-release check | Leader (read-only) |

Do not push / publish on the user's behalf, and do not commit before approval.

---

## 0. Confirm where you are working

```bash
git branch --show-current
```

This repository's parallel-session discipline: the second and later sessions must be isolated with `git worktree`. A release, which touches the whole tree, especially must not share a checkout with anyone else.

## 1. Preflight all green

```bash
bash scripts/preflight.sh          # For a release, run the full suite; do not use --fast
```

Four gates: `ruff check src/ tests/` → `dashboard npm run lint` → `check_invariants.sh` (I1–I14) → `pytest tests/unit/`.

A failure looks like this: each item followed by `✗ <gate name> failed`, ending with `✗ Preflight did not pass — fix before pushing`. The exit code is non-zero.

One **established exemption**: in an environment where `dashboard/dist` is not built, I3 outputs `⚠️ dist directory missing (ignorable in an unbuilt environment)` — a warning, not a block. But a release must build (see step 4), so at release time this line should no longer be a warning.

## 2. The seven version locksteps

**I2's five machine-checked places** (drift means red, `❌ [I2] Version number drift: ...`):

- `pyproject.toml`
- `src/aiteam/__init__.py`
- `plugin/.claude-plugin/plugin.json`
- `plugin/.claude-plugin/marketplace.json`
- `.claude-plugin/marketplace.json`

**One more place watched by I6's class-① hard equality**: the announcement line (starting with `> ⚡`) of `README.md` must contain `v<new version>`, otherwise `❌ [I6] README number drift`. (The former Chinese edition, `README.zh-CN.md`, was retired when the project went English-only.)

**Not covered by machine checks — human eyes required**: the numbers inside the `description` text of `plugin/.claude-plugin/plugin.json` (the MCP tool count, lifecycle event count). The only solid catch from human review in the v1.11.1 release was here — two numbers had rotted for an entire version.

The other numbers in the README (tool count / page count / endpoint count / test count) are verified by I6 against measurements; **change them per measurement, never from memory**.

## 3. CHANGELOG (English only)

- `CHANGELOG.md` gets a new version section, titled `## [x.y.z] - YYYY-MM-DD`.
- Section content is written with evidence taken item by item from `git log` and the design documents, never copied from the plan (things the plan did not deliver become fake accounts if written into the CHANGELOG).

Historical note: a Chinese edition (`CHANGELOG.zh-CN.md`) once mirrored this file under a full-translation contract (creator ruling of 2026-07-30); it was retired when the project went English-only, and no translated edition is maintained anymore.

## 4. Dashboard dual dist builds consistent (I3)

```bash
cd dashboard && npm run build && cd ..
rm -rf plugin/dashboard-dist
cp -R dashboard/dist plugin/dashboard-dist
```

I3's failure hint says `cp -R dashboard/dist plugin/dashboard-dist`; when the target directory already exists, that command nests a `dist/` layer inside it, so **first `rm -rf`, then `cp`**. Not deleting has a second pitfall: bundle filenames carry hashes, so old files are never overwritten and remain in the distribution package as stale artifacts.

A failure looks like this: `❌ [I3] The JS bundles of dashboard/dist and plugin/dashboard-dist are inconsistent`.

`plugin/dashboard-dist/` is the artifact marketplace users receive — it must be produced by **the same commit** as this batch's frontend source.

## 5. Private-terminology keyword scan (four surfaces)

**The word list is not written here**: writing the banned-word list into the repository about to be released would publish the very thing being guarded against. Before executing, use `memory_search` to pull the leak-prevention direction-layer memory (search term "private terminology leak prevention") for the current word list.

The four surfaces (these are exactly the four actually scanned in past releases; the messages of `f86a63e` and `b92295b` record them):

```bash
PREV=v<previous version>
git log --format='%h %B' $PREV..HEAD      # ① All commit messages in this batch
git diff $PREV..HEAD                      # ② The content of all tracked files this batch touches
git diff --name-status $PREV..HEAD        # ③ The paths of files added/renamed in this batch themselves
```

④ **The release commit message you are about to write itself** — scan it after writing, before committing. This surface is the easiest to miss, because it does not exist in git yet.

On any hit, stop: sanitize the tree surface and start over. Record (batch 9): an agent wrote a private design document's name and internal terminology into `types.py`, `hook_translator.py`, test comments, and a commit message; it was caught by manual scanning and ultimately needed user-authorized history rewriting to reach zero. The release scan is **the last gate**; the first gate is in the dispatch prompt.

## 6. Development/distribution sync human review

`src/` is the development edition and `plugin/` is the distribution edition. Machine checks already cover: I1 (hook dual copies, byte-for-byte), I3 (dual dist), I6 (README numbers), I8 (the hook registration surface install.py ↔ hooks.json ↔ bilingual README).

**Beyond machine-check coverage, go through these by eye, one by one**:

- Does `plugin/.mcp.json` have a version reference?
- Does `install.py` embed a version number?
- The numbers in the `description` text of `plugin/.claude-plugin/plugin.json` (see step 2)
- Was `plugin/dashboard-dist/` produced by the same commit as the frontend source: does the bundle contain this batch's new pages' code, and do the bundle names referenced by `index.html` match the actual filenames in `assets/`?
- New skills / agent templates / commands added? `install.py` walks directories (`copy_skills` etc.), so adding a directory needs no code change — but confirm the directory name matches the frontmatter `name`

## 7. Commit (submit to the user for approval first)

Write the message in English, with no agent signature attached (signatures such as `Co-Authored-By:` are forbidden). Following the structure of past release commits, state clearly:

1. The version's nature and the rationale for the number segment (why patch / minor)
2. The seven version locksteps: old version → new version
3. The evidence points of the CHANGELOG sections (what Added / Fixed / Changed / Upgrade notes each contain)
4. The development/distribution human review conclusions (step 6, item by item)
5. The conclusions of the four-surface keyword scan
6. Acceptance numbers: pytest pass/skip counts, the `check_invariants.sh` I1–I14 results (including established exemptions)

First show the user the `git diff --stat` and your drafted message; commit only after approval.

## 8. Tag

```bash
git tag -a v<x.y.z> -m "<one line synonymous with the commit title>"
```

Historically annotated and lightweight tags were mixed (v1.10.1/2/3 are lightweight; v1.10.0/v1.11.0/v1.11.1 are annotated). Use `-a`, consistent with the most recent two versions.

## 9. Dual-repo push (**user executes**)

```bash
git push origin master        # origin = the private repo (daily pushes)
git push origin v<x.y.z>
git push public master        # public = the public repo (release sync + tag)
git push public v<x.y.z>
```

Dual-repo strategy: since v1.8.0 the public repo syncs the full edition. After pushing, both remotes' master should sit on the same commit (the two lines of `git log --oneline -1 origin/master public/master` are identical).

`--verify-tag` (next step) verifies whether **the remote** has the tag, so the tag must be pushed first.

## 10. Create the GitHub Release entry (**user executes the publish**)

The Leader generates the body and commands first — offline, repeatable, no network:

```bash
python3 scripts/release_notes.py <x.y.z>
```

It does four things: writes the **verbatim slice** of that version's section from `CHANGELOG.md` into a notes file, verifies the section exists and is non-empty, verifies the version matches I2's five places, and verifies the tag exists locally; then it prints a directly pasteable `gh release create ... --verify-tag --title ... --notes-file ...`. **The script itself never publishes.**

The user executes the printed command. The title convention is `v1.11.1 — <English subtitle>`; the subtitle is written by a human — the script does not fabricate it — and is passed with `--title`:

```bash
python3 scripts/release_notes.py 1.11.1 --title "v1.11.1 — Truthful Ledgers"
```

**When backfilling several missed entries**: add `--backfill` (explicitly declaring that the version number intentionally differs from the current tree), and **create them one by one in ascending version order**. GitHub's latest badge follows publish time; creating in reverse order would hoist an old version to latest — on 2026-07-30 they were backfilled ascending, 1.10.3 → 1.11.0 → 1.11.1, which is why latest landed correctly.

## 11. Post-release check (Leader, read-only)

```bash
python3 scripts/release_notes.py --check <x.y.z>
```

All green looks like this:

```
✅ Body identical to online, byte for byte (15597 characters)
ℹ️  Online title: 'v1.11.1 — Truthful Ledgers: ...' · Published at 2026-07-30T04:21:30Z
✅ latest badge = v1.11.1
```

Failures look like this, and here is what each means:

| Output | Meaning | What to do |
| --- | --- | --- |
| `❌ <repo> has no Release entry for v<x>` | The tag was pushed but the entry was never created — exactly the two leaks | Return to step 10 |
| `❌ Body differs from online` + `the difference is only newlines/whitespace` | No rendering difference (the v1.10.0/1/2 batch were stripped slices — a historical difference) | May be left alone; to align, use the `gh release edit` it prints |
| `❌ Body differs from online` + `still not equal` | The CHANGELOG was edited after the entry was created | Overwrite the body with the printed `gh release edit` (user executes) |
| `❌ latest badge points to <old tag>` | The visitor homepage sidebar is showing an old version | Check the publish order; re-publish the newest entry if needed |
| `⚠️ --check skipped: cannot obtain the online Release` | Network/auth/`gh` missing. The script only reports and does not gate; exit code 0 | Re-run on another network; do not treat it as a pass |

The byte-for-byte comparability of `--check` only holds from **v1.10.0** onward: the bodies of v1.9.0 and earlier entries were hand-written and substantively differ from the CHANGELOG sections (measured: v1.9.0 online is 1375 characters vs the CHANGELOG section's 4012 characters).

While you are at it, check the public repo sidebar once: `gh release list --repo <owner/name> --limit 5`.

---

## Why this one was not made into a machine check

`scripts/check_invariants.sh` must run **offline and deterministically** (usable by CI and locally alike, with no external dependencies), while checking whether a Release entry exists and what the latest badge points to **requires the network**. Putting it into the machine checks first requires answering "may machine checks use the network, and does being offline count as red or as skipped" — itself a design question that must be settled first. Add this repository's discipline (adding/modifying machine-check red lines must pass a meeting first), and this stays in the on-demand script `scripts/release_notes.py --check`, invoked by step 11 of this checklist, rather than masquerading as a machine check that could turn green because the network is down.
