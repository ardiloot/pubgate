# pubgate: Design Specification

Python CLI tool syncing an internal repo with a public repo. PR review in both directions, with an internal leak-review gate before anything is pushed to the public repo. Hosting-agnostic -- works with any git remote.

## Why not standard git tools?

Git's model preserves and shares complete history. Every native sync tool carries history along with content, but internal history is exactly what needs to stay hidden.

- **Fork**: shares full commit history. Past commits expose internal code, proprietary messages, and file names. `git filter-repo` can clean history once, but produces incompatible SHAs on every run, making it unusable for continuous sync.
- **Cherry-pick**: requires manually selecting "safe" commits. Cannot strip internal sections from individual files. Doesn't scale.
- **Merge**: brings all content including internal files. Standard merge preserves or references internal history. You'd need manual cleanup after every merge, the exact manual curation this tool eliminates.
- **Subtree / Submodule**: solves composition (embedding one repo in another), not filtered mirroring.
- **filter-repo / filter-branch**: one-time migration tools (`filter-branch` is deprecated). Repeated runs produce new SHAs, breaking external clones. Operate on entire history, too expensive for continuous use.

pubgate sidesteps this: staging is always a snapshot (current state, mechanically filtered, no history shared). The public repo gets its own independent commit history.

## Terminology

| Term | Meaning |
|------|---------|
| **internal repo** | The private/internal repository (any git host) |
| **public repo** | The public-facing repository (any git host) |
| **public-remote** | The git remote name in the working clone pointing at the public repo |

## Commands

- `absorb` = bring public repo changes into internal `main` via internal PR
- `stage` = generate public stage candidate and open an internal PR into `pubgate/public-approved`
- `publish` = push reviewed internal `pubgate/public-approved` content to a branch on the public repo and open or update a PR to public `main`

## Branches and tracking

| Branch | Purpose |
|--------|---------|
| `main` | Internal development |
| `pubgate/public-approved` | Internal branch containing reviewed staged content approved for the public repo |
| `pubgate/absorb` | Temp: absorb PR branch in internal repo |
| `pubgate/stage` | Temp: stage PR branch in internal repo |
| `pubgate/publish` | Temp: stage PR branch on the public repo |

Use two independent tracking files:
- `.pubgate-absorbed` on `main` = last absorbed `public-remote/main` hash; also included in the staged snapshot by `stage`, so it appears on `pubgate/public-approved` and the public repo as the absorbed baseline at staging time
- `.pubgate-staged` on `pubgate/public-approved` content = last staged `main` hash for external publication

These states are independent and should not share meaning implicitly by branch.
- Outbound tracking stays with the published content
- Both state files being pushed to the public repo is acceptable (`.pubgate-absorbed` contains a public-repo commit hash, so there is no information leak)
- Each file is included only where that tracking state is needed

## Per-command startup

Each command runs its own startup sequence before command-specific logic. `--dry-run` still runs the full startup. Stage and publish read `origin/pubgate/public-approved` (the remote tracking ref, not the local branch) to guarantee freshness after an stage PR is merged on the server.

### `absorb` startup

1. Ensure clean worktree - abort if uncommitted changes
2. Verify on `main` - error if on a different branch or detached HEAD
3. Fetch `origin` (with `--prune`), verify local `main` matches `origin/main` exactly -- error if ahead, behind, or diverged
4. Fetch `public-remote` (with `--prune`)
5. Prune stale PR branches: if a local PR branch exists but its remote counterpart was deleted (merged and auto-deleted on the server), delete the local branch
6. Check absorb status:

| Status | `absorb` |
|--------|----------|
| `UP_TO_DATE` | exit |
| `NEEDS_ABSORB` | proceed |
| `NEEDS_BOOTSTRAP` | bootstrap |

### `stage` startup

1. Ensure clean worktree - abort if uncommitted changes
2. Verify on `main` - error if on a different branch or detached HEAD
3. Fetch `origin` (with `--prune`), verify local `main` matches `origin/main` exactly -- error if ahead, behind, or diverged
4. Prune stale internal PR branches (`pubgate/absorb`, `pubgate/stage`): if the local branch exists but its remote counterpart on `origin` was deleted, delete the local branch
5. Error if `main:.pubgate-absorbed` does not exist (no baseline; run `absorb` first to bootstrap)

### `publish` startup

1. Ensure clean worktree - abort if uncommitted changes
2. Fetch `origin` (with `--prune`), needed to read `origin/pubgate/public-approved`
3. Fetch `public-remote` (with `--prune`)
4. Prune stale public PR branch (`pubgate/publish`): if the local branch exists but its remote counterpart on `public-remote` was deleted, delete the local branch

## `absorb` (public -> internal) -- semi-automated

1. Run absorb startup; exit on `UP_TO_DATE`
2. If `NEEDS_BOOTSTRAP`: record `public-remote/main HEAD` as initial baseline on a PR branch, open PR into `main`
3. If `NEEDS_ABSORB`: determine changed files by diffing the public tree at `main:.pubgate-absorbed` against `public-remote/main`; exclude both state files (`.pubgate-absorbed`, `.pubgate-staged`) from the diff (they are sync artifacts, not external contributions)
4. Create or update `pubgate/absorb` from `main`
5. Compute the inbound result, applying the per-file merge/copy/delete rules as needed, and update `.pubgate-absorbed`; deleted public files are left in place and reported for manual review in the PR; when only state files changed since last absorb, the resulting PR only updates `.pubgate-absorbed` (tracking-only)
6. Commit the result and open or update the internal PR into `main`; the commit message lists the public commits being absorbed (safe, they are already public); internal CI must pass before merge

## `stage` (internal -> internal `pubgate/public-approved` review) -- semi-automated

1. Run stage startup; error if `main:.pubgate-absorbed` is missing
2. Build the stage candidate from `main` by excluding internal files that must not be published and scrubbing `BEGIN-INTERNAL`/`END-INTERNAL`. Built-in default ignore patterns cover common naming conventions (`.internal/*`, `*-internal.*`, `*.internal.*`, `*.secret`, etc.); users can override them via `ignore` in `pubgate.toml`. `.pubgate-absorbed` is included in the snapshot naturally (not excluded), and `.pubgate-staged` is set to `main` HEAD; if the result does not differ from `origin/pubgate/public-approved`, exit
3. Create or update `pubgate/stage` from `origin/pubgate/public-approved`
4. Commit the staged result to `pubgate/stage` and open or update the internal PR (`pubgate/stage` → `pubgate/public-approved`); the commit message lists the internal commits since the last stage (safe, stays on the internal repo, useful context for the leak reviewer); internal review here is the leak-check gate before anything is pushed to the public repo

## `publish` (internal `pubgate/public-approved` -> public repo branch -> public PR) -- semi-automated

1. Run publish startup
2. If `origin/pubgate/public-approved:.pubgate-staged` is missing → error ("run `stage` and merge the internal PR first")
3. If `public-remote/main:.pubgate-staged` exists and equals `origin/pubgate/public-approved:.pubgate-staged` → exit (already delivered or nothing pending for public delivery)
4. Read the absorbed baseline from `origin/pubgate/public-approved:.pubgate-absorbed`; create or update branch `pubgate/publish` based on this absorbed commit, replacing all content with the current content of `origin/pubgate/public-approved`
5. Open or update public PR (`pubgate/publish` → `main`); public CI must pass before merge
6. Done; after the public PR is merged, the user may run `absorb` so `.pubgate-absorbed` catches up to the new `public-remote/main` commit (recommended but not required before the next `stage`/`publish` cycle)

## Core design principle: controlled divergence

Without active management, internal and public repos can drift apart significantly: files diverge, patches conflict, and reconciliation becomes increasingly painful. pubgate prevents this by enforcing a single rule: **the public repo is always an exact filtered copy of internal, never an independent fork.**

Outbound snapshots are produced mechanically: ignore patterns exclude entire files, and `BEGIN-INTERNAL` / `END-INTERNAL` markers strip sections from individual files. No manual curation is involved; the same deterministic rules are applied every time. This means the public repo's content is always a predictable, reproducible function of the internal repo's content.

When the user follows the strict `absorb → stage → publish` workflow, the public repo is always an exact filtered copy of internal. If external contributions arrive mid-cycle (between stage and publish, or before the next absorb), `stage` and `publish` no longer block. Instead, `publish` bases the public PR on the last absorbed commit. Git's three-way merge preserves external contributions or surfaces them as conflicts in the public PR. This is an acceptable trade-off: the public PR may require conflict resolution when unabsorbed changes overlap with the snapshot, but external contributions are never silently overwritten.

The result is that divergence between the two repos is always controlled and bounded: the public repo differs from internal only by the content that was mechanically stripped, never by accumulated drift.

## Key constraints

- Python stdlib + git CLI, hosting-agnostic
- Inbound review happens in the internal repo
- Outbound has two gates:
  - internal PR into `pubgate/public-approved` to catch leaks before any public push
  - public PR into `main` to run public CI before merge
- `absorb` only modifies internal `main`
- `stage` is the only command that modifies internal `pubgate/public-approved`
- `publish` only modifies public-repo-side branches/PRs
- `main` and public `main` stay protected by their destination CI gates
- Outbound: snapshot (no drift). Inbound: three-way merge (base from public history)
- Outbound publication must always be a filtered snapshot, never a normal history-preserving merge, to avoid exposing internal history
- The tool is intended to be operator-driven; CI validates the resulting PRs but does not own the sync workflow
- Protected branches are never written directly
- Temp branches force-updated, one PR per direction
- Initial setup manual
- Each command has a planning phase and an execution phase; `--dry-run` shows the planned actions without changing branches, files, or PRs (still runs the full per-command startup)
- PR creation is automatic when a supported hosting provider is detected (currently GitHub via the `gh` CLI). If the remote URL is not a supported provider, or `gh` is not installed/authenticated, commands log manual PR creation steps instead. Use `--no-pr` to disable automatic PR creation. Run `gh auth login` to set up authentication
- Each command has its own startup sequence tailored to the remotes it interacts with: `absorb` fetches both remotes and verifies `main` is synced; `stage` fetches only `origin` and verifies `main` is synced; `publish` fetches both `origin` (for `origin/pubgate/public-approved`) and `public-remote` but does not require being on `main`
- Branch guard: before creating a PR branch, each command checks whether the branch already exists. If it does (previous PR not merged), the command errors out. Use `--force` to overwrite the existing branch and proceed. After a PR is merged and the server auto-deletes the source branch, the next startup prune removes the stale local branch automatically

## Known limitations

### State file conflicts on repeated publish without absorb

If the user publishes multiple times without running `absorb` between cycles, each publish PR is based on the same absorbed commit. The `.pubgate-staged` file will have different values on the PR branch versus `public-remote/main` (from the previous publish merge), and both appear as "added" relative to the absorbed base, producing a guaranteed merge conflict on this file. The conflict is trivially resolvable by taking the newer value. Running `absorb` between publish cycles advances the baseline and eliminates this.

### Git LFS support

pubgate supports repositories that use Git LFS. LFS support is auto-detected via `git lfs version` and requires no configuration.

**How it works:** LFS-tracked files are stored as pointer files in git. pubgate reads and writes these pointers as-is; they pass through the snapshot, stage, and publish pipelines without modification. When files are staged with `git add`, git's clean/smudge filters handle the LFS encoding automatically via `.gitattributes`.

**LFS object transfer:** pubgate runs `git lfs fetch` during command startups (absorb, publish) to ensure LFS objects are locally cached, and `git lfs push` after pushing branches to transfer LFS objects to the destination remote's LFS server.

**Limitations:**
- **LFS files are treated as binary**: they are never merged during `absorb` (copied/overwritten instead) and never scrubbed for `BEGIN-INTERNAL`/`END-INTERNAL` markers during `stage`. Do not place internal markers inside LFS-tracked files; use ignore patterns in `pubgate.toml` to exclude sensitive LFS files from publication.
- **`.gitattributes` is included as-is** in the public snapshot (with internal-block scrubbing if markers are present). If internal `.gitattributes` contains LFS patterns for files excluded by pubgate's ignore rules, those orphan patterns will appear in the public repo. This is harmless but may be confusing. Use `BEGIN-INTERNAL`/`END-INTERNAL` markers in `.gitattributes` to exclude internal-only LFS patterns.
- **When LFS is not installed**, pubgate's behavior is unchanged; LFS-specific operations (fetch, push) are silently skipped.
