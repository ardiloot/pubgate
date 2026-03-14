import logging
from collections.abc import Callable
from typing import NamedTuple

from .absorb import apply_absorb_changes
from .config import CONFIG_FILE, Config
from .errors import GitError, PubGateError
from .git import GitRepo
from .stage_snapshot import build_stage_snapshot
from .state import AbsorbStatus, validate_state_sha

logger = logging.getLogger(__name__)


class _AbsorbResult(NamedTuple):
    status: AbsorbStatus
    public_head: str
    last_absorbed: str | None


# ---------------------------------------------------------------------------
# PubGate class
# ---------------------------------------------------------------------------


class PubGate:
    def __init__(
        self,
        cfg: Config,
        git: GitRepo,
    ) -> None:
        self.cfg = cfg
        self.git = git

    # ------------------------------------------------------------------
    # Public commands
    # ------------------------------------------------------------------

    def absorb(self, *, dry_run: bool = False, force: bool = False) -> None:
        cfg, git = self.cfg, self.git
        public_main = cfg.public_main_ref

        result = self._absorb_startup()
        public_head = result.public_head

        if result.status == AbsorbStatus.UP_TO_DATE:
            logger.info("Already up to date (%s = %s)", public_main, public_head[:7])
            return

        if result.status == AbsorbStatus.NEEDS_BOOTSTRAP:
            published = git.read_file_at_ref(public_main, cfg.stage_state_file)
            if published is None:
                logger.warning(
                    "No prior publish detected on public-remote/main. "
                    "Ensure initial setup is complete before merging this PR."
                )
            logger.info("First-run bootstrap: recording %s HEAD (%s) as initial baseline", public_main, public_head[:7])

            if dry_run:
                logger.info("[dry-run] Would create branch, write tracking file, and commit")
                return

            def _bootstrap_work() -> bool:
                git.write_file_and_stage(cfg.absorb_state_file, public_head + "\n")
                msg = f"pubgate: initialize absorb tracking at {public_head[:7]}"
                sha = git.commit(msg)
                logger.info("Committed on %s (%s %s)", cfg.absorb_pr_branch, sha[:7], msg)
                return True

            self._run_on_pr_branch(
                branch=cfg.absorb_pr_branch,
                base=cfg.internal_main_branch,
                label="absorb",
                force=force,
                work_fn=_bootstrap_work,
            )
            self._push_to_remote(cfg.absorb_pr_branch, "origin", cfg.absorb_pr_branch, force=force)
            logger.info("Next steps:")
            logger.info("  1. Create PR '%s → %s' on your git host", cfg.absorb_pr_branch, cfg.internal_main_branch)
            logger.info("  2. Review and merge the PR")
            return

        # NEEDS_ABSORB - normal absorb
        last_absorbed = result.last_absorbed
        assert last_absorbed is not None, f"Expected {cfg.absorb_state_file} to exist"

        public_commits = git.log_oneline(last_absorbed, public_head)
        n = len(public_commits)
        logger.info(
            "Absorbing %d %s: %s..%s",
            n,
            "commit" if n == 1 else "commits",
            last_absorbed[:7],
            public_head[:7],
        )
        for i, c in enumerate(public_commits, 1):
            logger.info("  %d. %s (%s, %s, %s)", i, c.subject, c.sha[:7], c.author, c.date)
        state_files = frozenset({cfg.absorb_state_file, cfg.stage_state_file})
        changes = git.diff_tree(last_absorbed, public_head)
        changes = [c for c in changes if c.path not in state_files]
        if not changes:
            logger.info("No file changes detected (metadata-only commits?). Updating tracking")

        if dry_run:
            logger.info("[dry-run] Changes (base %s):", last_absorbed[:7])
            if changes:
                for c in changes:
                    logger.info("  %s: %s", c.status, c.path)
            else:
                logger.info("  (no file changes)")
            logger.info("[dry-run] Would commit on %s", cfg.absorb_pr_branch)
            logger.info("[dry-run] Would push %s to origin/%s", cfg.absorb_pr_branch, cfg.absorb_pr_branch)
            logger.info("Next steps:")
            logger.info(
                "  1. Create PR '%s \u2192 %s' on your git host", cfg.absorb_pr_branch, cfg.internal_main_branch
            )
            logger.info("  2. Review and merge the PR")
            return

        def _absorb_work() -> bool:
            actions = self._apply_absorb_changes(last_absorbed, public_head)
            if actions:
                logger.info("Changes (base %s):", last_absorbed[:7])
            for a in actions:
                if "review manually" in a or "CONFLICTS" in a:
                    logger.warning("%s", a)
                else:
                    logger.info("%s", a)
            git.write_file_and_stage(cfg.absorb_state_file, public_head + "\n")
            conflicted = [a.split(": ", 1)[1] for a in actions if "CONFLICTS" in a]
            msg = self._absorb_commit_message(last_absorbed, public_head, conflicted)
            sha = git.commit(msg)
            logger.info("Committed on %s (%s %s)", cfg.absorb_pr_branch, sha[:7], msg.split("\n", 1)[0])
            return True

        self._run_on_pr_branch(
            branch=cfg.absorb_pr_branch,
            base=cfg.internal_main_branch,
            label="absorb",
            force=force,
            work_fn=_absorb_work,
        )
        self._push_to_remote(cfg.absorb_pr_branch, "origin", cfg.absorb_pr_branch, force=force)
        logger.info("Next steps:")
        logger.info("  1. Create PR '%s → %s' on your git host", cfg.absorb_pr_branch, cfg.internal_main_branch)
        logger.info("  2. Review and merge the PR")

    def stage(self, *, dry_run: bool = False, force: bool = False) -> None:
        cfg, git = self.cfg, self.git

        self._stage_startup()

        main_head = git.rev_parse(cfg.internal_main_branch)
        origin_preview_ref = f"origin/{cfg.internal_preview_branch}"

        ignore_patterns = list(cfg.ignore)
        snapshot = self._build_stage_snapshot(ignore_patterns)

        unchanged_ref = self._snapshot_unchanged_ref(snapshot)
        if unchanged_ref is not None:
            if unchanged_ref == cfg.stage_pr_branch:
                if not force:
                    raise PubGateError(
                        f"Error: branch '{cfg.stage_pr_branch}' already exists "
                        f"(previous PR not merged?). Use --force to overwrite."
                    )
            else:
                logger.info("No changes to stage (%s is already up to date)", cfg.internal_preview_branch)
                return

        prev_state = git.read_file_at_ref(origin_preview_ref, cfg.stage_state_file)
        if prev_state is not None:
            try:
                prev_sha = validate_state_sha(prev_state, f"{origin_preview_ref}:{cfg.stage_state_file}")
                internal_commits = git.log_oneline(prev_sha, main_head)
            except PubGateError:
                internal_commits = []
        else:
            internal_commits = []
        n = len(internal_commits)
        if n:
            logger.info(
                "Staging %d %s: %s..%s",
                n,
                "commit" if n == 1 else "commits",
                prev_sha[:7],
                main_head[:7],
            )
            for i, c in enumerate(internal_commits, 1):
                logger.info("  %d. %s (%s, %s, %s)", i, c.subject, c.sha[:7], c.author, c.date)
        else:
            logger.info("Staging changes into public-preview")

        if dry_run:
            logger.info("[dry-run] Would commit on %s", cfg.stage_pr_branch)
            logger.info("[dry-run] Would push %s to origin/%s", cfg.stage_pr_branch, cfg.stage_pr_branch)
            logger.info("Next steps:")
            logger.info(
                "  1. Create PR '%s \u2192 %s' on your git host", cfg.stage_pr_branch, cfg.internal_preview_branch
            )
            logger.info("  2. Review and merge the PR")
            logger.info("  3. Run 'pubgate publish' (if ready)")
            return

        self._ensure_public_branch()

        def _stage_work() -> bool:
            existing = git.ls_tree("HEAD")
            for path in existing:
                if path not in snapshot and path != cfg.stage_state_file:
                    git.remove_file_and_stage(path)

            for path, content in sorted(snapshot.items()):
                git.write_file_and_stage_auto(path, content)

            git.write_file_and_stage(cfg.stage_state_file, main_head + "\n")

            if not git.has_staged_changes():
                logger.info("No changes to stage (public-preview is already up to date)")
                return False

            msg = self._stage_commit_message(main_head, origin_preview_ref)
            sha = git.commit(msg)
            logger.info("Committed on %s (%s %s)", cfg.stage_pr_branch, sha[:7], msg.split("\n", 1)[0])
            return True

        committed = self._run_on_pr_branch(
            branch=cfg.stage_pr_branch,
            base=origin_preview_ref,
            label="stage",
            force=force,
            work_fn=_stage_work,
        )
        if committed:
            self._push_to_remote(cfg.stage_pr_branch, "origin", cfg.stage_pr_branch, force=force)
            logger.info("Next steps:")
            logger.info("  1. Create PR '%s → %s' on your git host", cfg.stage_pr_branch, cfg.internal_preview_branch)
            logger.info("  2. Review and merge the PR")
            logger.info("  3. Run 'pubgate publish' (if ready)")

    def publish(self, *, dry_run: bool = False, force: bool = False) -> None:
        cfg, git = self.cfg, self.git
        public_main = cfg.public_main_ref

        self._publish_startup()

        origin_preview_ref = f"origin/{cfg.internal_preview_branch}"

        # Guard: internal PR into public must have been merged
        stage_state_val = git.read_file_at_ref(origin_preview_ref, cfg.stage_state_file)
        if stage_state_val is None:
            raise PubGateError(
                "Error: no stage state found on public-preview. Run 'stage' and merge the internal PR first."
            )

        main_sha = validate_state_sha(stage_state_val, f"{origin_preview_ref}:{cfg.stage_state_file}")

        # Already delivered?
        remote_stage_state = git.read_file_at_ref(public_main, cfg.stage_state_file)
        if remote_stage_state is not None:
            remote_sha = validate_state_sha(remote_stage_state, f"{public_main}:{cfg.stage_state_file}")
            if remote_sha == main_sha:
                logger.info("Already published (public repo is up to date)")
                return

        # Read absorbed baseline from origin/public-preview
        absorbed_state = git.read_file_at_ref(origin_preview_ref, cfg.absorb_state_file)
        if absorbed_state is None:
            raise PubGateError("Error: no absorb state found on public-preview. Run 'absorb' and 'stage' first.")
        absorbed_sha = validate_state_sha(absorbed_state, f"{origin_preview_ref}:{cfg.absorb_state_file}")

        if not git.is_ancestor(absorbed_sha, public_main):
            raise PubGateError(
                f"Error: absorbed commit {absorbed_sha[:7]} is not an ancestor of {cfg.public_remote}/main. "
                "The public repo may have been force-pushed. Run 'absorb' to re-sync."
            )

        # Build commit on top of absorbed commit with content from origin/public-preview
        public_files = git.ls_tree(origin_preview_ref)

        # Log commits being published
        remote_stage_state_val = git.read_file_at_ref(public_main, cfg.stage_state_file)
        if remote_stage_state_val is not None:
            try:
                prev_published = validate_state_sha(remote_stage_state_val, f"{public_main}:{cfg.stage_state_file}")
                staged_commits = git.log_oneline(prev_published, main_sha)
            except PubGateError:
                staged_commits = []
        else:
            staged_commits = []
        n = len(staged_commits)
        if n:
            logger.info(
                "Publishing %d %s: %s..%s",
                n,
                "commit" if n == 1 else "commits",
                prev_published[:7],
                main_sha[:7],
            )
            for i, c in enumerate(staged_commits, 1):
                logger.info("  %d. %s (%s, %s, %s)", i, c.subject, c.sha[:7], c.author, c.date)
        else:
            logger.info("Publishing staged content to %s", cfg.public_remote)

        if dry_run:
            logger.info("[dry-run] Would commit on %s", cfg.publish_pr_branch)
            logger.info(
                "[dry-run] Would push %s to %s/%s", cfg.publish_pr_branch, cfg.public_remote, cfg.publish_pr_branch
            )
            logger.info("Next steps:")
            logger.info(
                "  1. Create PR '%s \u2192 %s' on the public repo", cfg.publish_pr_branch, cfg.public_main_branch
            )
            logger.info("  2. Review and merge the PR")
            logger.info("  3. Run 'pubgate absorb' to sync tracking")
            return

        def _publish_work() -> bool:
            existing = git.ls_tree("HEAD")
            for path in existing:
                git.remove_file_and_stage(path)

            for path in sorted(public_files):
                git.copy_file_from_ref(origin_preview_ref, path)

            if not git.has_staged_changes():
                logger.info("No changes to publish (public repo already has this content)")
                return False

            msg = f"pubgate: publish stage from {main_sha[:7]}"
            sha = git.commit(msg)
            logger.info("Committed on %s (%s %s)", cfg.publish_pr_branch, sha[:7], msg)
            return True

        def _publish_push() -> None:
            self._push_to_remote(cfg.publish_pr_branch, cfg.public_remote, cfg.publish_pr_branch, force=force)

        committed = self._run_on_pr_branch(
            branch=cfg.publish_pr_branch,
            base=absorbed_sha,
            label="publish",
            force=force,
            work_fn=_publish_work,
            after_fn=_publish_push,
        )
        if committed:
            logger.info("Next steps:")
            logger.info("  1. Create PR '%s → %s' on the public repo", cfg.publish_pr_branch, cfg.public_main_branch)
            logger.info("  2. Review and merge the PR")
            logger.info("  3. Run 'pubgate absorb' to sync tracking")

    # ------------------------------------------------------------------
    # Shared workflow (private)
    # ------------------------------------------------------------------

    def _require_on_main(self) -> None:
        git, cfg = self.git, self.cfg
        git.ensure_clean_worktree()
        current = git.current_branch()
        if current == "HEAD":
            raise PubGateError(f"Error: HEAD is detached. Run 'git checkout {cfg.internal_main_branch}' first.")
        if current != cfg.internal_main_branch:
            raise PubGateError(
                f"Error: expected branch '{cfg.internal_main_branch}', currently on '{current}'. "
                f"Run 'git checkout {cfg.internal_main_branch}' first."
            )
        git.fetch("origin")
        git.ensure_branch_synced(cfg.internal_main_branch, "origin", cfg.internal_main_branch)

    def _absorb_startup(self) -> _AbsorbResult:
        logger.debug("Starting absorb startup")
        self._require_on_main()
        self.git.fetch(self.cfg.public_remote)
        self._prune_internal_pr_branches()
        self._prune_publish_pr_branch()
        return self._check_absorb()

    def _stage_startup(self) -> None:
        logger.debug("Starting stage startup")
        self._require_on_main()
        self._prune_internal_pr_branches()
        absorb_state = self.git.read_file_at_ref(self.cfg.internal_main_branch, self.cfg.absorb_state_file)
        if absorb_state is None:
            raise PubGateError("Error: no absorb state found. Run 'absorb' first.")

    def _publish_startup(self) -> None:
        logger.debug("Starting publish startup")
        git, cfg = self.git, self.cfg
        git.ensure_clean_worktree()
        git.fetch("origin")
        git.fetch(cfg.public_remote)
        self._prune_publish_pr_branch()

    def _check_absorb(self) -> _AbsorbResult:
        cfg, git = self.cfg, self.git
        if not git.remote_branch_exists(cfg.public_remote, cfg.public_main_branch):
            raise PubGateError(
                f"Error: public repo has no '{cfg.public_main_branch}' branch. "
                f"The public repo must have at least one commit before running absorb."
            )
        public_head = git.rev_parse(cfg.public_main_ref)

        absorb_state = git.read_file_at_ref(cfg.internal_main_branch, cfg.absorb_state_file)
        last_absorbed = validate_state_sha(absorb_state, cfg.absorb_state_file) if absorb_state else None

        if last_absorbed is None:
            logger.debug("Inbound status: NEEDS_BOOTSTRAP")
            return _AbsorbResult(AbsorbStatus.NEEDS_BOOTSTRAP, public_head, None)

        if last_absorbed == public_head:
            logger.debug("Inbound status: UP_TO_DATE")
            return _AbsorbResult(AbsorbStatus.UP_TO_DATE, public_head, last_absorbed)

        logger.debug("Inbound status: NEEDS_ABSORB")
        return _AbsorbResult(AbsorbStatus.NEEDS_ABSORB, public_head, last_absorbed)

    def _snapshot_unchanged_ref(self, snapshot: dict[str, str | bytes]) -> str | None:
        """Return the ref the snapshot matched against, or None if there are changes."""
        cfg, git = self.cfg, self.git
        if git.branch_exists(cfg.stage_pr_branch):
            compare_ref = cfg.stage_pr_branch
            logger.debug("Comparing snapshot against existing PR branch %s", compare_ref)
        elif git.remote_branch_exists("origin", cfg.internal_preview_branch):
            compare_ref = f"origin/{cfg.internal_preview_branch}"
            logger.debug("Comparing snapshot against origin/%s", cfg.internal_preview_branch)
        else:
            logger.debug("No previous snapshot to compare against")
            return "(empty)" if not snapshot else None

        prev_files = set(git.ls_tree(compare_ref)) - {cfg.stage_state_file, cfg.absorb_state_file}
        new_files = set(snapshot.keys()) - {cfg.absorb_state_file}
        if prev_files != new_files:
            return None

        for path in new_files:
            new_content = snapshot[path]
            if isinstance(new_content, bytes):
                old_content = git.read_file_at_ref_bytes(compare_ref, path)
            else:
                old_content = git.read_file_at_ref(compare_ref, path)
            if old_content != new_content:
                return None
        return compare_ref

    def _guard_branch_not_exists(self, name: str, *, force: bool) -> None:
        if not force and self.git.branch_exists(name):
            logger.debug("Branch %s exists and --force not set", name)
            raise PubGateError(
                f"Error: branch '{name}' already exists (previous PR not merged?). Use --force to overwrite."
            )

    def _run_on_pr_branch(
        self,
        *,
        branch: str,
        base: str,
        label: str,
        force: bool,
        work_fn: Callable[[], bool],
        after_fn: Callable[[], None] | None = None,
    ) -> bool:
        self._guard_branch_not_exists(branch, force=force)
        self.git.create_or_update_branch(branch, base)
        try:
            with self.git.on_branch(branch):
                committed = work_fn()
            if committed and after_fn:
                after_fn()
        except GitError as exc:
            raise PubGateError(
                f"Error during {label}: {exc}\n"
                f"The branch '{branch}' may be in a partial state. "
                f"Re-running '{label}' should recover."
            ) from exc
        return committed

    def _push_to_remote(
        self,
        local_branch: str,
        remote: str,
        remote_branch: str,
        *,
        force: bool = False,
    ) -> None:
        cfg = self.cfg
        if force:
            protected = {cfg.internal_main_branch, cfg.internal_preview_branch, cfg.public_main_branch}
            if remote_branch in protected:
                raise PubGateError(f"Error: refusing to force-push to protected branch '{remote_branch}'.")
        logger.info("Pushing %s to %s/%s", local_branch, remote, remote_branch)
        self.git.push(local_branch, remote, remote_branch, force=force)

    def _prune_internal_pr_branches(self) -> None:
        cfg, git = self.cfg, self.git
        for branch in (cfg.absorb_pr_branch, cfg.stage_pr_branch):
            if git.branch_exists(branch) and not git.remote_branch_exists("origin", branch):
                logger.debug("Pruning stale local branch %s", branch)
                git.delete_branch(branch)

    def _prune_publish_pr_branch(self) -> None:
        cfg, git = self.cfg, self.git
        if git.branch_exists(cfg.publish_pr_branch) and not git.remote_branch_exists(
            cfg.public_remote, cfg.publish_pr_branch
        ):
            logger.debug("Pruning stale local branch %s", cfg.publish_pr_branch)
            git.delete_branch(cfg.publish_pr_branch)

    # ------------------------------------------------------------------
    # Absorb internals (private)
    # ------------------------------------------------------------------

    def _apply_absorb_changes(self, base_sha: str, public_head: str) -> list[str]:
        cfg = self.cfg
        git = self.git
        public_ref = f"{cfg.public_remote}/{cfg.public_main_branch}"
        excluded = frozenset({cfg.absorb_state_file, cfg.stage_state_file})
        # Read the internal commit SHA that produced the current public content
        stage_state = git.read_file_at_ref(public_ref, cfg.stage_state_file)
        staged_sha: str | None = None
        if stage_state is not None:
            try:
                staged_sha = validate_state_sha(stage_state, f"{public_ref}:{cfg.stage_state_file}")
            except PubGateError:
                pass
        return apply_absorb_changes(git, base_sha, public_head, public_ref, excluded=excluded, staged_sha=staged_sha)

    def _absorb_commit_message(self, last_absorbed: str, public_head: str, conflicted: list[str] | None = None) -> str:
        subject = f"pubgate: absorb public changes {last_absorbed[:7]}..{public_head[:7]}"
        commits = self.git.log_oneline(last_absorbed, public_head)
        lines = [subject]
        if commits:
            lines.append("")
            for c in commits:
                lines.append(f"  {c.subject} ({c.sha[:7]}, {c.author}, {c.date})")
        if conflicted:
            lines.append("")
            lines.append("CONFLICTS (resolve before merging):")
            for path in conflicted:
                lines.append(f"  {path}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Stage internals (private)
    # ------------------------------------------------------------------

    def _stage_commit_message(self, main_head: str, origin_preview_ref: str) -> str:
        subject = f"pubgate: stage from main {main_head[:7]}"
        prev_state = self.git.read_file_at_ref(origin_preview_ref, self.cfg.stage_state_file)
        if prev_state is None:
            return subject
        try:
            prev_sha = validate_state_sha(prev_state, f"{origin_preview_ref}:{self.cfg.stage_state_file}")
        except PubGateError:
            return subject
        commits = self.git.log_oneline(prev_sha, main_head)
        if not commits:
            return subject
        lines = [subject, ""]
        for c in commits:
            lines.append(f"  {c.subject} ({c.sha[:7]}, {c.author}, {c.date})")
        return "\n".join(lines)

    def _build_stage_snapshot(self, ignore_patterns: list[str]) -> dict[str, str | bytes]:
        return build_stage_snapshot(
            self.git,
            self.cfg.internal_main_branch,
            ignore_patterns,
            frozenset({CONFIG_FILE}),
        )

    def _ensure_public_branch(self) -> None:
        cfg, git = self.cfg, self.git
        if git.remote_branch_exists("origin", cfg.internal_preview_branch):
            return

        logger.info("Creating orphan branch '%s'...", cfg.internal_preview_branch)
        git.checkout_orphan(cfg.internal_preview_branch)
        try:
            git.rm_all_tracked()
            git.commit_allow_empty("pubgate: initialize public branch")
        except BaseException:
            try:
                git.checkout_safe(cfg.internal_main_branch)
                git.delete_branch_safe(cfg.internal_preview_branch)
            except Exception as cleanup_exc:
                logger.warning("Failed to clean up incomplete orphan branch: %s", cleanup_exc)
            raise
        git.checkout(cfg.internal_main_branch)
        git.push(cfg.internal_preview_branch, "origin", cfg.internal_preview_branch)
        logger.debug("Orphan branch '%s' created and pushed", cfg.internal_preview_branch)
