import logging
from collections.abc import Callable
from pathlib import Path

from .absorb import AbsorbResult, absorb_commit_message, check_absorb, resolve_and_apply
from .config import CONFIG_FILE, Config
from .errors import GitError, PubGateError
from .git import GitRepo
from .models import CommitInfo, format_commit
from .pr import detect_provider
from .publish import normalize_publish_metadata, resolve_publish_base
from .stage_snapshot import (
    apply_stage_snapshot,
    build_stage_snapshot,
    ensure_public_branch,
    snapshot_unchanged_ref,
    stage_commit_message,
)
from .state import AbsorbStatus, StateRef

logger = logging.getLogger(__name__)

_PREVIEW_LOCK_REASON = "pubgate-preview-v1"


def _log_commits(commits: list[CommitInfo], *, limit: int = 10) -> None:
    for i, c in enumerate(commits[:limit], 1):
        logger.info("  %d. %s", i, format_commit(c))
    if len(commits) > limit:
        logger.info("  ... and %d more", len(commits) - limit)


def _split_message(msg: str) -> tuple[str, str]:
    title = msg.split("\n", 1)[0]
    body = msg.split("\n", 1)[1].strip() if "\n" in msg else ""
    return title, body


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

    def absorb(self, *, dry_run: bool = False, force: bool = False, no_pr: bool = False) -> None:
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

            self._guard_branch_not_exists(cfg.internal_absorb_branch, force=force)

            if dry_run:
                logger.info("[dry-run] Would create branch, write tracking file, and commit")
                self._handle_pr(
                    remote="origin",
                    head=cfg.internal_absorb_branch,
                    base=cfg.internal_main_branch,
                    title="",
                    body="",
                    host_label="your git host",
                    no_pr=no_pr,
                    dry_run=True,
                )
                return

            def _bootstrap_work() -> bool:
                git.write_file_and_stage(cfg.absorb_state_file, public_head + "\n")
                msg = f"pubgate: initialize absorb tracking at {public_head[:7]}"
                sha = git.commit(msg)
                logger.info("Committed on %s (%s %s)", cfg.internal_absorb_branch, sha[:7], msg)
                return True

            self._run_on_pr_branch(
                branch=cfg.internal_absorb_branch,
                base=cfg.internal_main_branch,
                label="absorb",
                force=force,
                work_fn=_bootstrap_work,
            )
            self._push_to_remote(cfg.internal_absorb_branch, "origin", cfg.internal_absorb_branch, force=force)
            self.git.lfs_push("origin", cfg.internal_absorb_branch)
            title = f"pubgate: initialize absorb tracking at {public_head[:7]}"
            self._handle_pr(
                remote="origin",
                head=cfg.internal_absorb_branch,
                base=cfg.internal_main_branch,
                title=title,
                body="",
                host_label="your git host",
                no_pr=no_pr,
            )
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
        _log_commits(public_commits)
        state_files = cfg.state_files
        changes = git.diff_tree(last_absorbed, public_head)
        changes = [c for c in changes if c.path not in state_files]
        if not changes:
            logger.info("No file changes detected (metadata-only commits?). Updating tracking")

        self._guard_branch_not_exists(cfg.internal_absorb_branch, force=force)

        if dry_run:
            logger.info("[dry-run] Changes (base %s):", last_absorbed[:7])
            if changes:
                for c in changes:
                    logger.info("  %s: %s", c.status, c.path)
            else:
                logger.info("  (no file changes)")
            logger.info("[dry-run] Would commit on %s", cfg.internal_absorb_branch)
            logger.info("[dry-run] Would push %s to origin/%s", cfg.internal_absorb_branch, cfg.internal_absorb_branch)
            self._handle_pr(
                remote="origin",
                head=cfg.internal_absorb_branch,
                base=cfg.internal_main_branch,
                title="",
                body="",
                host_label="your git host",
                no_pr=no_pr,
                dry_run=True,
            )
            return

        git.lfs_fetch(cfg.public_remote, public_head)

        commit_message: str | None = None

        def _absorb_work() -> bool:
            nonlocal commit_message
            actions = resolve_and_apply(cfg, git, last_absorbed, public_head)
            if actions:
                logger.info("Changes (base %s):", last_absorbed[:7])
            for a in actions:
                if "review manually" in a or "CONFLICTS" in a:
                    logger.warning("%s", a)
                else:
                    logger.info("%s", a)
            git.write_file_and_stage(cfg.absorb_state_file, public_head + "\n")
            conflicted = [a.split(": ", 1)[1] for a in actions if "CONFLICTS" in a]
            needs_review = [a.strip() for a in actions if "review manually" in a]
            commit_message = absorb_commit_message(git, last_absorbed, public_head, conflicted, needs_review)
            sha = git.commit(commit_message)
            logger.info(
                "Committed on %s (%s %s)",
                cfg.internal_absorb_branch,
                sha[:7],
                commit_message.split("\n", 1)[0],
            )
            return True

        self._run_on_pr_branch(
            branch=cfg.internal_absorb_branch,
            base=cfg.internal_main_branch,
            label="absorb",
            force=force,
            work_fn=_absorb_work,
        )
        self._push_to_remote(cfg.internal_absorb_branch, "origin", cfg.internal_absorb_branch, force=force)
        self.git.lfs_push("origin", cfg.internal_absorb_branch)
        assert commit_message is not None
        title, body = _split_message(commit_message)
        self._handle_pr(
            remote="origin",
            head=cfg.internal_absorb_branch,
            base=cfg.internal_main_branch,
            title=title,
            body=body,
            host_label="your git host",
            no_pr=no_pr,
        )

    def stage(self, *, dry_run: bool = False, force: bool = False, no_pr: bool = False) -> None:
        cfg, git = self.cfg, self.git

        self._stage_startup()

        main_head = git.rev_parse(cfg.internal_main_branch)
        approved_ref = f"origin/{cfg.internal_approved_branch}"

        ignore_patterns = list(cfg.ignore)
        snapshot, lfs_count = build_stage_snapshot(
            git,
            cfg.internal_main_branch,
            ignore_patterns,
            frozenset({CONFIG_FILE, cfg.stage_state_file}),
        )

        unchanged_ref = snapshot_unchanged_ref(cfg, git, snapshot)
        if unchanged_ref is not None:
            if unchanged_ref == cfg.internal_stage_branch:
                if not force:
                    raise PubGateError(
                        f"Error: branch '{cfg.internal_stage_branch}' already exists "
                        f"(previous PR not merged?). Use --force to overwrite."
                    )
            else:
                logger.info("No changes to stage (%s is already up to date)", cfg.internal_approved_branch)
                return

        previous_stage = StateRef.read(git, approved_ref, cfg.stage_state_file)
        previous_stage_sha = previous_stage.sha if previous_stage is not None else None
        if previous_stage_sha is None:
            previous_stage_sha = git.find_commit_adding(cfg.internal_main_branch, cfg.absorb_state_file)

        internal_commits = []
        if previous_stage_sha is not None:
            if not git.is_ancestor(previous_stage_sha, main_head):
                raise PubGateError(
                    f"Error: previous staged source {previous_stage_sha[:7]} is not an ancestor of "
                    f"{cfg.internal_main_branch} {main_head[:7]}. Reconcile the source branch history first."
                )
            try:
                internal_commits = git.log_oneline(previous_stage_sha, main_head)
            except (GitError, PubGateError) as exc:
                logger.warning("Could not list staged commits: %s", exc)
            n = len(internal_commits)
            if n:
                logger.info(
                    "Staging %d %s: %s..%s",
                    n,
                    "commit" if n == 1 else "commits",
                    previous_stage_sha[:7],
                    main_head[:7],
                )
                _log_commits(internal_commits)
            else:
                logger.info("Staging changes into %s", cfg.internal_approved_branch)
        else:
            logger.info("Staging changes into %s", cfg.internal_approved_branch)

        full_msg = stage_commit_message(main_head, previous_stage_sha, internal_commits)

        if lfs_count:
            logger.info("Snapshot includes %d LFS-tracked %s", lfs_count, "file" if lfs_count == 1 else "files")

        if dry_run:
            logger.info("[dry-run] Would commit on %s", cfg.internal_stage_branch)
            logger.info("[dry-run] Would push %s to origin/%s", cfg.internal_stage_branch, cfg.internal_stage_branch)
            self._handle_pr(
                remote="origin",
                head=cfg.internal_stage_branch,
                base=cfg.internal_approved_branch,
                title="",
                body="",
                host_label="your git host",
                extra_steps=["Run 'pubgate publish' (if ready)"],
                no_pr=no_pr,
                dry_run=True,
            )
            return

        ensure_public_branch(cfg, git)

        def _stage_work() -> bool:
            apply_stage_snapshot(git, snapshot, cfg.stage_state_file, main_head + "\n")

            if not git.has_staged_changes():
                logger.info("No changes to stage (%s is already up to date)", cfg.internal_approved_branch)
                return False

            sha = git.commit(full_msg)
            logger.info("Committed on %s (%s %s)", cfg.internal_stage_branch, sha[:7], full_msg.split("\n", 1)[0])
            return True

        committed = self._run_on_pr_branch(
            branch=cfg.internal_stage_branch,
            base=approved_ref,
            label="stage",
            force=force,
            work_fn=_stage_work,
        )
        if committed:
            self._push_to_remote(cfg.internal_stage_branch, "origin", cfg.internal_stage_branch, force=force)
            title, body = _split_message(full_msg)
            self._handle_pr(
                remote="origin",
                head=cfg.internal_stage_branch,
                base=cfg.internal_approved_branch,
                title=title,
                body=body,
                host_label="your git host",
                extra_steps=["Run 'pubgate publish' (if ready)"],
                no_pr=no_pr,
            )

    def preview(self, *, output: str | Path, force: bool = False) -> None:
        cfg, git = self.cfg, self.git
        source_root = git.repo_dir.resolve()
        output_path = Path(output).expanduser().resolve()

        if (
            output_path == source_root
            or output_path.is_relative_to(source_root)
            or source_root.is_relative_to(output_path)
        ):
            raise PubGateError("Error: preview output must be outside the source working tree.")

        git.ensure_clean_worktree()
        try:
            source_head = git.rev_parse("HEAD")
        except GitError as exc:
            raise PubGateError("Error: preview requires at least one commit.") from exc
        if git.is_sparse_checkout_enabled():
            raise PubGateError("Error: preview does not support sparse checkouts.")

        existing = git.find_worktree(output_path)
        if existing is not None and not output_path.exists():
            if existing.lock_reason != _PREVIEW_LOCK_REASON:
                raise PubGateError(
                    f"Error: refusing to remove missing worktree '{output_path}'; it is not a pubgate preview worktree."
                )
            git.remove_locked_worktree(output_path)
            logger.debug("Removed missing preview worktree registration: %s", output_path)
            existing = None

        if output_path.exists() or existing is not None:
            if not force:
                raise PubGateError(f"Error: preview output '{output_path}' already exists. Use --force to replace it.")
            if existing is None or existing.lock_reason != _PREVIEW_LOCK_REASON:
                raise PubGateError(f"Error: refusing to replace '{output_path}'; it is not a pubgate preview worktree.")

        git.fetch("origin")
        approved_ref = f"origin/{cfg.internal_approved_branch}"
        approved_base = git.try_rev_parse(approved_ref)
        approved_exists = approved_base is not None
        if approved_base is None:
            approved_base = git.create_empty_root_commit("pubgate: initialize local preview base")

        # Validate a present absorb state without requiring bootstrap.
        StateRef.read(git, "HEAD", cfg.absorb_state_file)
        snapshot, lfs_count = build_stage_snapshot(
            git,
            "HEAD",
            list(cfg.ignore),
            frozenset({CONFIG_FILE, cfg.stage_state_file}),
        )

        preview_git: GitRepo
        try:
            if existing is None:
                git.add_locked_worktree(output_path, approved_base, _PREVIEW_LOCK_REASON)
                preview_git = GitRepo(output_path)
            else:
                preview_git = GitRepo(output_path)
                preview_git.clean_untracked()
                preview_git.reset_hard(approved_base, skip_lfs_smudge=True)

            apply_stage_snapshot(preview_git, snapshot, cfg.stage_state_file, source_head + "\n")
            preview_git.clean_untracked()
            if lfs_count:
                preview_git.lfs_checkout()

            unexpected = [
                line
                for line in preview_git.status_porcelain()
                if line.startswith("??") or len(line) < 2 or line[1] != " "
            ]
            if unexpected:
                raise PubGateError("Error: preview generation left unexpected working-tree changes.")
        except BaseException:
            if git.find_worktree(output_path) is not None:
                try:
                    git.remove_locked_worktree(output_path)
                except Exception as cleanup_exc:
                    logger.warning("Failed to remove incomplete preview worktree '%s': %s", output_path, cleanup_exc)
            raise

        logger.info("Preview %s at %s", "updated" if existing is not None else "ready", output_path)
        logger.info("  Filtered commit: %s", source_head[:7])
        if approved_exists:
            logger.info("  Comparison base: %s (%s)", approved_ref, approved_base[:7])
        else:
            logger.info("  Comparison base: empty (%s does not exist)", approved_ref)
        if lfs_count:
            logger.info("  LFS-tracked files: %d", lfs_count)
        logger.info("  Changes are staged; run tests from the preview worktree.")

    def publish(
        self,
        *,
        message: str,
        author_name: str,
        author_email: str,
        dry_run: bool = False,
        force: bool = False,
        no_pr: bool = False,
    ) -> None:
        cfg, git = self.cfg, self.git
        public_main = cfg.public_main_ref
        message, author_name, author_email = normalize_publish_metadata(message, author_name, author_email)

        self._publish_startup()

        approved_ref = f"origin/{cfg.internal_approved_branch}"

        # Guard: internal PR into public must have been merged
        stage_ref = StateRef.read(git, approved_ref, cfg.stage_state_file)
        if stage_ref is None:
            raise PubGateError(
                f"Error: no stage state found on {cfg.internal_approved_branch}. "
                "Run 'stage' and merge the internal PR first."
            )
        # Already delivered?
        remote_stage_ref = StateRef.read(git, public_main, cfg.stage_state_file)
        if remote_stage_ref is not None:
            if remote_stage_ref.sha == stage_ref.sha:
                logger.info("Already published (public repo is up to date)")
                return

        # Read absorbed baseline from origin/{internal_approved_branch}
        absorb_ref = StateRef.read(git, approved_ref, cfg.absorb_state_file)
        if absorb_ref is None:
            raise PubGateError(
                f"Error: no absorb state found on {cfg.internal_approved_branch}. Run 'absorb' and 'stage' first."
            )
        absorbed_sha = absorb_ref.sha

        if not git.is_ancestor(absorbed_sha, public_main):
            raise PubGateError(
                f"Error: absorbed commit {absorbed_sha[:7]} is not an ancestor of {cfg.public_remote}/main. "
                "The public repo may have been force-pushed. Run 'absorb' to re-sync."
            )

        # Build commit on top of content from origin/{internal_approved_branch}
        public_files = git.ls_tree(approved_ref)

        # Determine publish base and log range
        public_head = git.rev_parse(public_main)
        publish_base, publish_log_base = resolve_publish_base(
            cfg,
            git,
            absorbed_sha,
            public_head,
            approved_ref,
            remote_sha=remote_stage_ref.sha if remote_stage_ref is not None else None,
        )

        # Log commits being published from origin/{internal_approved_branch}
        approved_commits = git.log_oneline(publish_log_base, approved_ref)
        n = len(approved_commits)
        if n:
            logger.info(
                "Publishing %d %s from %s (base %s):",
                n,
                "commit" if n == 1 else "commits",
                cfg.internal_approved_branch,
                publish_base[:7],
            )
            _log_commits(approved_commits)
        else:
            logger.info("Publishing to %s (no changes, base %s)", cfg.public_remote, publish_base[:7])

        title, body = _split_message(message)

        self._guard_branch_not_exists(cfg.public_publish_branch, force=force)

        if dry_run:
            logger.info("[dry-run] Would commit on %s", cfg.public_publish_branch)
            logger.info(
                "[dry-run] Would push %s to %s/%s",
                cfg.public_publish_branch,
                cfg.public_remote,
                cfg.public_publish_branch,
            )
            self._handle_pr(
                remote=cfg.public_remote,
                head=cfg.public_publish_branch,
                base=cfg.public_main_branch,
                title=title,
                body=body,
                host_label="the public repo",
                extra_steps=["Run 'pubgate absorb' to sync tracking"],
                no_pr=no_pr,
                dry_run=True,
            )
            return

        git.lfs_fetch("origin", approved_ref)

        def _publish_work() -> bool:
            existing = git.ls_tree("HEAD")
            for path in existing:
                git.remove_file_and_stage(path)

            for path in sorted(public_files):
                git.copy_file_from_ref(approved_ref, path)

            if not git.has_staged_changes():
                logger.info("No changes to publish (public repo already has this content)")
                return False

            sha = git.commit_with_identity(message, author_name, author_email)
            logger.info("Committed on %s (%s %s)", cfg.public_publish_branch, sha[:7], title)
            return True

        def _publish_push() -> None:
            self._push_to_remote(cfg.public_publish_branch, cfg.public_remote, cfg.public_publish_branch, force=force)
            self.git.lfs_push(cfg.public_remote, cfg.public_publish_branch)

        committed = self._run_on_pr_branch(
            branch=cfg.public_publish_branch,
            base=publish_base,
            label="publish",
            force=force,
            work_fn=_publish_work,
            after_fn=_publish_push,
        )
        if committed:
            self._handle_pr(
                remote=cfg.public_remote,
                head=cfg.public_publish_branch,
                base=cfg.public_main_branch,
                title=title,
                body=body,
                host_label="the public repo",
                extra_steps=["Run 'pubgate absorb' to sync tracking"],
                no_pr=no_pr,
            )

    def status(self) -> None:
        from .status import report_status

        report_status(self.cfg, self.git)

    # ------------------------------------------------------------------
    # Shared workflow (private)
    # ------------------------------------------------------------------

    def _log_manual_pr_steps(
        self,
        head: str,
        base: str,
        host_label: str,
        extra_steps: list[str] | None = None,
    ) -> None:
        logger.info("Next steps:")
        logger.info("  1. Create PR '%s → %s' on %s", head, base, host_label)
        logger.info("  2. Review and merge the PR")
        for i, step in enumerate(extra_steps or [], start=3):
            logger.info("  %d. %s", i, step)

    def _handle_pr(
        self,
        *,
        remote: str,
        head: str,
        base: str,
        title: str,
        body: str,
        host_label: str,
        extra_steps: list[str] | None = None,
        no_pr: bool,
        dry_run: bool = False,
    ) -> None:
        if no_pr:
            self._log_manual_pr_steps(head, base, host_label, extra_steps)
            return

        try:
            remote_url = self.git.get_remote_url(remote)
        except Exception:
            self._log_manual_pr_steps(head, base, host_label, extra_steps)
            return

        provider = detect_provider(remote_url)
        if provider is None:
            self._log_manual_pr_steps(head, base, host_label, extra_steps)
            return

        if dry_run:
            logger.info("[dry-run] Would create/update PR '%s → %s' automatically", head, base)
            logger.info("Next steps:")
            logger.info("  1. Review and merge the PR")
            for i, step in enumerate(extra_steps or [], start=2):
                logger.info("  %d. %s", i, step)
            return

        try:
            result = provider.create_or_update_pr(head=head, base=base, title=title, body=body)
        except Exception as exc:
            logger.warning("Automatic PR creation failed: %s", exc)
            self._log_manual_pr_steps(head, base, host_label, extra_steps)
            return

        if result.created:
            logger.info("Created PR #%d: %s", result.number, result.url)
        else:
            logger.info("Updated PR #%d: %s", result.number, result.url)
        logger.info("Next steps:")
        logger.info("  1. Review and merge the PR")
        for i, step in enumerate(extra_steps or [], start=2):
            logger.info("  %d. %s", i, step)

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

    def _absorb_startup(self) -> AbsorbResult:
        logger.debug("Starting absorb startup")
        self._require_on_main()
        self.git.fetch(self.cfg.public_remote)
        self._prune_internal_pr_branches()
        self._prune_public_publish_branch()
        return check_absorb(self.cfg, self.git)

    def _stage_startup(self) -> None:
        logger.debug("Starting stage startup")
        self._require_on_main()
        self._prune_internal_pr_branches()
        if StateRef.read(self.git, self.cfg.internal_main_branch, self.cfg.absorb_state_file) is None:
            raise PubGateError("Error: no absorb state found. Run 'absorb' first.")

    def _publish_startup(self) -> None:
        logger.debug("Starting publish startup")
        git, cfg = self.git, self.cfg
        git.ensure_clean_worktree()
        git.fetch("origin")
        git.fetch(cfg.public_remote)
        self._prune_public_publish_branch()

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
            protected = {cfg.internal_main_branch, cfg.internal_approved_branch, cfg.public_main_branch}
            if remote_branch in protected:
                raise PubGateError(f"Error: refusing to force-push to protected branch '{remote_branch}'.")
        logger.info("Pushing %s to %s/%s", local_branch, remote, remote_branch)
        self.git.push(local_branch, remote, remote_branch, force=force)

    def _prune_internal_pr_branches(self) -> None:
        cfg, git = self.cfg, self.git
        for branch in (cfg.internal_absorb_branch, cfg.internal_stage_branch):
            if git.branch_exists(branch) and not git.remote_branch_exists("origin", branch):
                logger.debug("Pruning stale local branch %s", branch)
                git.delete_branch(branch)

    def _prune_public_publish_branch(self) -> None:
        cfg, git = self.cfg, self.git
        if git.branch_exists(cfg.public_publish_branch) and not git.remote_branch_exists(
            cfg.public_remote, cfg.public_publish_branch
        ):
            logger.debug("Pruning stale local branch %s", cfg.public_publish_branch)
            git.delete_branch(cfg.public_publish_branch)
