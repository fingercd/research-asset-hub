from __future__ import annotations

from pathlib import Path

import pytest

from tools.paper_code_audit import audit


def _approval() -> audit.RepositoryApproval:
    link = audit.canonical_link("https://github.com/example/repository")
    assert link is not None
    return audit.RepositoryApproval(
        link=link,
        commit="a" * 40,
        destination="github/example/repository",
        papers=(audit.ApprovedPaper("paper", "Paper"),),
        sparse_paths=(),
        evidence=("reviewed",),
    )


def test_sparse_plan_auto_excludes_nested_weights_suffixes_and_windows_paths() -> None:
    plan = audit._build_sparse_checkout_plan(
        [
            "README.md",
            "src/main.py",
            "src/weights/model.bin",
            "src/cache/model.pth",
            "STOP./nested/file.py",
            "docs/AUX.txt",
            "bad:name.py",
        ],
        explicit_sparse_paths=[],
    )

    assert plan.auto_excluded_paths == (
        "STOP./nested/file.py",
        "bad:name.py",
        "docs/AUX.txt",
        "src/cache/model.pth",
        "src/weights/model.bin",
    )
    assert "!/STOP./" in plan.patterns
    assert "!/src/weights/" in plan.patterns
    assert "!/src/cache/model.pth" in plan.patterns
    assert "!/docs/AUX.txt" in plan.patterns
    reasons = dict(plan.auto_exclusion_reasons)
    assert reasons["src/weights/model.bin"] == ("blocked_tree_component",)
    assert reasons["src/cache/model.pth"] == ("blocked_tree_suffix",)
    assert reasons["STOP./nested/file.py"] == ("windows_trailing_dot_or_space",)
    assert reasons["docs/AUX.txt"] == ("windows_reserved_name",)
    assert reasons["bad:name.py"] == ("windows_invalid_character",)


def test_explicit_sparse_paths_merge_with_automatic_exclusions() -> None:
    plan = audit._build_sparse_checkout_plan(
        [
            "README.md",
            "LeanK/main.py",
            "LeanK/weights/model.bin",
            "LeanK/cache.pth",
            "Other/weights/ignored.bin",
        ],
        explicit_sparse_paths=["LeanK"],
    )

    assert "/LeanK/" in plan.patterns
    assert "!/LeanK/weights/" in plan.patterns
    assert "!/LeanK/cache.pth" in plan.patterns
    assert plan.auto_excluded_paths == (
        "LeanK/cache.pth",
        "LeanK/weights/model.bin",
    )
    assert "Other/weights/ignored.bin" not in plan.auto_excluded_paths


def test_unrepresentable_invalid_path_fails_closed() -> None:
    with pytest.raises(ValueError, match="cannot be represented"):
        audit._build_sparse_checkout_plan(
            ["bad\nname.pth"],
            explicit_sparse_paths=[],
        )


def test_long_windows_relative_path_is_auto_excluded() -> None:
    long_path = "src/" + ("nested-" * 30) + "/main.py"
    assert len(long_path.encode("utf-16-le")) // 2 > 220

    plan = audit._build_sparse_checkout_plan([long_path], explicit_sparse_paths=[])

    assert plan.auto_excluded_paths == (long_path,)
    assert dict(plan.auto_exclusion_reasons)[long_path] == ("windows_relative_path_too_long",)


def test_casefold_collision_stably_keeps_sorted_first_path() -> None:
    plan = audit._build_sparse_checkout_plan(
        ["README.md", "readme.md", "src/Main.py", "src/main.py"],
        explicit_sparse_paths=[],
    )

    assert plan.checkout_paths == ("README.md", "src/Main.py")
    assert plan.casefold_collision_paths == ("readme.md", "src/main.py")
    assert dict(plan.casefold_collision_winners) == {
        "readme.md": "README.md",
        "src/main.py": "src/Main.py",
    }
    assert dict(plan.auto_exclusion_reasons)["readme.md"] == ("windows_casefold_collision",)
    assert "!/readme.md" in plan.patterns


def test_exclusion_verifier_fails_if_skip_worktree_bit_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_git_output(
        repository: Path,
        *arguments: str,
        timeout: int = 60,
        input_text: str | None = None,
    ) -> audit.CommandResult:
        assert repository == tmp_path
        assert arguments == ("ls-files", "-t", "-z")
        assert timeout == 60
        assert input_text is None
        return audit.CommandResult(0, "H src/weights/model.bin\0", "")

    monkeypatch.setattr(audit, "_git_output", fake_git_output)

    failures, error = audit._verify_auto_exclusions(
        tmp_path,
        ["src/weights/model.bin"],
    )

    assert error is None
    assert failures == ["src/weights/model.bin"]


def test_post_clone_failure_reports_specific_head_dirty_and_remote_differences() -> None:
    approval = _approval()

    failure = audit._post_clone_verification_failure(
        approval,
        head=audit.CommandResult(0, "b" * 40, ""),
        dirty=audit.CommandResult(0, "?? unexpected.txt", ""),
        remote=audit.CommandResult(0, "https://user:secret@github.com/other/repo?token=x", ""),
        auto_excluded_paths=["weights/model.bin"],
    )

    assert failure is not None
    assert failure["reason"] == "post_clone_verification_failed"
    assert failure["differences"] == [
        "head_mismatch",
        "working_tree_dirty",
        "remote_mismatch",
    ]
    assert failure["verification"]["actual_head"] == "b" * 40
    assert failure["verification"]["dirty_entries"] == ["?? unexpected.txt"]
    assert failure["verification"]["actual_remote"] == "https://github.com/other/repo"
    assert "secret" not in str(failure)


def test_post_clone_allows_only_exact_auto_excluded_worktree_deletions() -> None:
    approval = _approval()
    clean_enough = audit._post_clone_verification_failure(
        approval,
        head=audit.CommandResult(0, "a" * 40, ""),
        dirty=audit.CommandResult(0, " D weights/model.bin\0", ""),
        remote=audit.CommandResult(0, "https://github.com/example/repository", ""),
        auto_excluded_paths=["weights/model.bin"],
    )
    assert clean_enough is None

    failure = audit._post_clone_verification_failure(
        approval,
        head=audit.CommandResult(0, "a" * 40, ""),
        dirty=audit.CommandResult(
            0,
            " D weights/model.bin\0 M src/main.py\0 D weights/not-approved.bin\0",
            "",
        ),
        remote=audit.CommandResult(0, "https://github.com/example/repository", ""),
        auto_excluded_paths=["weights/model.bin"],
    )
    assert failure is not None
    assert failure["differences"] == ["working_tree_dirty"]
    assert failure["verification"]["allowed_excluded_deletions"] == [" D weights/model.bin"]
    assert failure["verification"]["dirty_entries"] == [
        " M src/main.py",
        " D weights/not-approved.bin",
    ]


def test_non_cone_sparse_checkout_materializes_safe_files_only(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    commands = [
        ("git", "init", str(source)),
        ("git", "-C", str(source), "config", "user.email", "test@example.org"),
        ("git", "-C", str(source), "config", "user.name", "Test User"),
    ]
    for command in commands:
        result = audit._run_command(command)
        assert result.returncode == 0, result.stderr
    (source / "src" / "weights").mkdir(parents=True)
    (source / "other").mkdir()
    (source / "src" / "main.py").write_text("print('safe')\n", encoding="utf-8")
    (source / "src" / "weights" / "model.bin").write_text("weight\n", encoding="utf-8")
    (source / "other" / "not-selected.py").write_text("print('other')\n", encoding="utf-8")
    (source / "cache.pth").write_text("checkpoint\n", encoding="utf-8")
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    assert audit._run_command(("git", "-C", str(source), "add", ".")).returncode == 0
    commit_result = audit._run_command(("git", "-C", str(source), "commit", "-m", "fixture"))
    assert commit_result.returncode == 0, commit_result.stderr
    commit = audit._git_output(source, "rev-parse", "HEAD").stdout

    checkout = tmp_path / "checkout"
    cloned = audit._run_command(("git", "clone", "--no-checkout", str(source), str(checkout)))
    assert cloned.returncode == 0, cloned.stderr
    tree = audit._git_output(checkout, "ls-tree", "-r", "-z", "--name-only", commit)
    plan = audit._build_sparse_checkout_plan(
        audit._parse_tree_paths(tree.stdout),
        explicit_sparse_paths=["src"],
    )
    configured = audit._configure_non_cone_sparse_checkout(checkout, plan)
    assert configured.returncode == 0, configured.stderr
    assert audit._git_output(checkout, "config", "--get", "core.autocrlf").stdout == "false"
    assert audit._git_output(checkout, "config", "--get", "core.eol").stdout == "lf"
    checked_out = audit._git_output(checkout, "checkout", "--detach", commit)
    assert checked_out.returncode == 0, checked_out.stderr
    reapplied = audit._git_output(checkout, "sparse-checkout", "reapply")
    assert reapplied.returncode == 0, reapplied.stderr

    failures, error = audit._verify_auto_exclusions(checkout, plan.auto_excluded_paths)

    assert error is None
    assert not failures, audit._git_output(checkout, "ls-files", "-t").stdout
    assert (checkout / "README.md").is_file()
    assert (checkout / "src" / "main.py").is_file()
    assert not (checkout / "src" / "weights" / "model.bin").exists()
    assert not (checkout / "cache.pth").exists()
    assert not (checkout / "other" / "not-selected.py").exists()


def test_invalid_windows_paths_use_fixed_index_head_fallback(tmp_path: Path) -> None:
    source = tmp_path / "invalid-source"
    source.mkdir()
    for command in (
        ("git", "init", str(source)),
        ("git", "-C", str(source), "config", "user.email", "test@example.org"),
        ("git", "-C", str(source), "config", "user.name", "Test User"),
    ):
        result = audit._run_command(command)
        assert result.returncode == 0, result.stderr
    safe = source / "safe.py"
    safe.write_text("print('safe')\n", encoding="utf-8")
    assert audit._run_command(("git", "-C", str(source), "add", "safe.py")).returncode == 0
    blob = audit._run_command(
        ("git", "-C", str(source), "hash-object", "-w", "--stdin"),
        input_text="invalid fixture\n",
    )
    assert blob.returncode == 0, blob.stderr
    for invalid_path in ("STOP.", "bad:name.txt"):
        cached = audit._run_command(
            (
                "git",
                "-C",
                str(source),
                "-c",
                "core.protectNTFS=false",
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob.stdout},{invalid_path}",
            )
        )
        assert cached.returncode == 0, cached.stderr
    tree = audit._git_output(source, "write-tree")
    assert tree.returncode == 0, tree.stderr
    commit = audit._git_output(source, "commit-tree", tree.stdout, input_text="fixture\n")
    assert commit.returncode == 0, commit.stderr
    assert (
        audit._git_output(source, "update-ref", "refs/heads/master", commit.stdout).returncode == 0
    )

    checkout = tmp_path / "invalid-checkout"
    cloned = audit._run_command(("git", "clone", "--no-checkout", str(source), str(checkout)))
    assert cloned.returncode == 0, cloned.stderr
    listed = audit._git_output(
        checkout,
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        commit.stdout,
    )
    plan = audit._build_sparse_checkout_plan(
        audit._parse_tree_paths(listed.stdout),
        explicit_sparse_paths=[],
    )
    assert set(plan.auto_excluded_paths) == {"STOP.", "bad:name.txt"}
    assert audit._configure_non_cone_sparse_checkout(checkout, plan).returncode == 0

    materialized = audit._materialize_fixed_commit(
        checkout,
        commit=commit.stdout,
        auto_excluded_paths=plan.auto_excluded_paths,
        safe_checkout_paths=plan.checkout_paths,
    )

    assert materialized.ok, materialized
    assert materialized.mode == "index-head-fallback"
    assert audit._git_output(checkout, "rev-parse", "HEAD").stdout == commit.stdout
    assert (checkout / "safe.py").is_file(), {
        "index": audit._git_output(checkout, "ls-files", "-t").stdout,
        "status": audit._git_output(checkout, "status", "--porcelain=v1").stdout,
        "patterns": plan.patterns,
    }
    assert not (checkout / "STOP.").exists()
    assert not (checkout / "bad:name.txt").exists()
    failures, error = audit._verify_auto_exclusions(
        checkout,
        plan.auto_excluded_paths,
        allow_index_only_deletions=True,
    )
    assert error is None
    assert not failures
    dirty = audit._git_output(
        checkout,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=normal",
    )
    verification = audit._post_clone_verification_failure(
        _approval(),
        head=audit.CommandResult(0, "a" * 40, ""),
        dirty=dirty,
        remote=audit.CommandResult(0, "https://github.com/example/repository", ""),
        auto_excluded_paths=plan.auto_excluded_paths,
    )
    assert verification is None


def test_casefold_collision_fixture_has_fixed_head_and_clean_safe_tree(tmp_path: Path) -> None:
    source = tmp_path / "case-source"
    source.mkdir()
    for command in (
        ("git", "init", str(source)),
        ("git", "-C", str(source), "config", "user.email", "test@example.org"),
        ("git", "-C", str(source), "config", "user.name", "Test User"),
        ("git", "-C", str(source), "config", "core.ignoreCase", "false"),
    ):
        result = audit._run_command(command)
        assert result.returncode == 0, result.stderr
    for path, content in (("README.md", "UPPER\n"), ("readme.md", "lower\n")):
        blob = audit._run_command(
            ("git", "-C", str(source), "hash-object", "-w", "--stdin"),
            input_text=content,
        )
        assert blob.returncode == 0, blob.stderr
        cached = audit._run_command(
            (
                "git",
                "-C",
                str(source),
                "-c",
                "core.ignoreCase=false",
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob.stdout},{path}",
            )
        )
        assert cached.returncode == 0, cached.stderr
    tree = audit._git_output(source, "write-tree")
    commit = audit._git_output(source, "commit-tree", tree.stdout, input_text="case fixture\n")
    assert commit.returncode == 0, commit.stderr
    assert (
        audit._git_output(source, "update-ref", "refs/heads/master", commit.stdout).returncode == 0
    )

    checkout = tmp_path / "case-checkout"
    cloned = audit._run_command(("git", "clone", "--no-checkout", str(source), str(checkout)))
    assert cloned.returncode == 0, cloned.stderr
    listed = audit._git_output(checkout, "ls-tree", "-r", "-z", "--name-only", commit.stdout)
    plan = audit._build_sparse_checkout_plan(
        audit._parse_tree_paths(listed.stdout), explicit_sparse_paths=[]
    )
    assert plan.casefold_collision_paths == ("readme.md",)
    assert audit._configure_non_cone_sparse_checkout(checkout, plan).returncode == 0
    materialized = audit._materialize_fixed_commit(
        checkout,
        commit=commit.stdout,
        auto_excluded_paths=plan.auto_excluded_paths,
        safe_checkout_paths=plan.checkout_paths,
    )
    assert materialized.ok, materialized
    assert audit._git_output(checkout, "rev-parse", "HEAD").stdout == commit.stdout
    assert (checkout / "README.md").read_text(encoding="utf-8") == "UPPER\n"
    failures, error = audit._verify_auto_exclusions(
        checkout,
        plan.auto_excluded_paths,
        allow_index_only_deletions=materialized.mode == "index-head-fallback",
        filesystem_alias_winners=dict(plan.casefold_collision_winners),
    )
    assert error is None
    assert not failures, audit._git_output(checkout, "ls-files", "-t").stdout
    dirty = audit._git_output(
        checkout, "status", "--porcelain=v1", "-z", "--untracked-files=normal"
    )
    link = audit.canonical_link("https://github.com/example/repository")
    assert link is not None
    approval = audit.RepositoryApproval(
        link=link,
        commit=commit.stdout,
        destination="github/example/repository",
        papers=(audit.ApprovedPaper("paper", "Paper"),),
        sparse_paths=(),
        evidence=("reviewed",),
    )
    verification = audit._post_clone_verification_failure(
        approval,
        head=audit.CommandResult(0, commit.stdout, ""),
        dirty=dirty,
        remote=audit.CommandResult(0, link.url, ""),
        auto_excluded_paths=plan.auto_excluded_paths,
    )
    assert verification is None
