"""Offline release-boundary tests: local Git objects and inert archives only."""

import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import release_candidate as candidate


@pytest.fixture(autouse=True)
def temp_data_dir():
    """This stdlib tool must not import or initialize the application/database."""
    yield


def git(repo, *args):
    env = candidate._git_env() | {
        "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00",
    }
    return (
        subprocess.check_output(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Candidate Test",
                "-c",
                "user.email=test@example.invalid",
                *args,
            ],
            env=env,
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )


def commit_all(repo):
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "Offline candidate fixture")
    return git(repo, "rev-parse", "HEAD")


def write(repo, path, content):
    destination = repo / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    for path in candidate.REQUIRED_FILES:
        write(repo, path, b"# inert fixture\n")
    write(repo, "server/release.py", b'APP_VERSION = "v1.2.3"\n')
    return repo, commit_all(repo)


@pytest.fixture
def built(source, tmp_path):
    repo, commit = source
    result = candidate.build_candidate(repo, commit, tmp_path / "candidate")
    return repo, commit, Path(result["archive"]), Path(result["manifest"])


def entries(path):
    with tarfile.open(path, "r:gz") as archive:
        return [(member, archive.extractfile(member).read()) for member in archive.getmembers()]


def compressed(raw):
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0, compresslevel=9) as handle:
        handle.write(raw)
    return stream.getvalue()


def repack(path, items):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for info, content in items:
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    path.write_bytes(compressed(stream.getvalue()))
    return path


def replace_manifest(items, manifest):
    payload = candidate._json_bytes(manifest)
    items[0] = (candidate._tar_info(candidate.MANIFEST_NAME, len(payload), 0o644), payload)


def test_reproducible_git_snapshot_ignores_dirty_files_and_records_no_local_identity(source, tmp_path):
    repo, commit = source
    first = candidate.build_candidate(repo, commit, tmp_path / "first")
    write(repo, "server/main.py", b'raise RuntimeError("dirty source must never run")\n')
    write(repo, ".env", b"private runtime fixture")
    second = candidate.build_candidate(repo, commit, tmp_path / "second")
    assert Path(first["archive"]).read_bytes() == Path(second["archive"]).read_bytes()
    assert Path(first["manifest"]).read_bytes() == Path(second["manifest"]).read_bytes()
    manifest_text = Path(first["manifest"]).read_text()
    manifest = json.loads(manifest_text)
    assert manifest["commit"] == commit
    assert manifest["source_tree"] == git(repo, "rev-parse", commit + "^{tree}")
    assert manifest["version"] == "v1.2.3"
    assert manifest["deployed"] is False
    assert str(repo) not in manifest_text and "@example.invalid" not in manifest_text
    assert "dirty source" not in gzip.decompress(Path(first["archive"]).read_bytes()).decode(errors="ignore")
    verified = candidate.verify_candidate(
        Path(first["archive"]), Path(first["manifest"]), expected_commit=commit, repo=repo
    )
    assert verified["provenance_verified"] is True
    assert candidate.verify_candidate(Path(first["archive"]))["provenance_verified"] is False


def test_runtime_and_installation_files_and_nonbuiltin_skills_are_excluded(source, tmp_path):
    repo, _ = source
    excluded = [
        ".env",
        "install.sh",
        "stock-app.service",
        "data/chat.db",
        "logs/runtime.log",
        "server/data/session.py",
        "static/reports/generated.html",
        "server/.env.py",
        "static/cache/response.json",
        "static/private/credentials.json",
        "server/archive.tar.gz",
        "server/archive/old.py",
        "static/privategit/old.json",
        "skills/external/CLAUDE.md",
        "skills/CLAUDE.md",
        "old-history.bundle",
        "tests/test_sentinel.py",
    ]
    for path in excluded:
        write(repo, path, b"excluded runtime fixture")
    write(repo, "skills/force-chinese/CLAUDE.md", b"# Controlled language instruction\n")
    write(repo, "server/executable.py", b"# executable source\n")
    (repo / "server/executable.py").chmod(0o755)
    commit = commit_all(repo)
    result = candidate.build_candidate(repo, commit, tmp_path / "output")
    manifest = json.loads(Path(result["manifest"]).read_bytes())
    paths = {item["path"] for item in manifest["files"]}
    assert not paths.intersection(excluded)
    assert "skills/force-chinese/CLAUDE.md" in paths
    assert (
        next(item for item in manifest["files"] if item["path"] == "server/executable.py")["mode"] == "0755"
    )


@pytest.mark.parametrize("location", ["source", "other_git", "symlink"])
def test_output_must_be_outside_all_git_workspaces(source, tmp_path, location):
    repo, commit = source
    if location == "source":
        output = repo / "output"
    else:
        other = tmp_path / "other"
        other.mkdir()
        git(other, "init", "--initial-branch=main")
        output = other / "output"
        if location == "symlink":
            output = tmp_path / "alias"
            output.symlink_to(other, target_is_directory=True)
            output /= "output"
    with pytest.raises(candidate.CandidateError, match="outside every Git"):
        candidate.build_candidate(repo, commit, output)
    assert not output.exists()


@pytest.mark.parametrize("bad", ["HEAD", "main", "a" * 39, "A" * 40, "a" * 41])
def test_requires_explicit_full_lowercase_commit(source, bad):
    repo, _ = source
    with pytest.raises(candidate.CandidateError, match="40-character"):
        candidate.collect_candidate(repo, bad)


def test_tree_object_and_commit_outside_allowed_ancestry_are_rejected(source):
    repo, original = source
    tree = git(repo, "rev-parse", original + "^{tree}")
    with pytest.raises(candidate.CandidateError, match="not a commit"):
        candidate.collect_candidate(repo, tree)
    write(repo, "server/new.py", b"# new source\n")
    newer = commit_all(repo)
    with pytest.raises(candidate.CandidateError, match="Git read failed"):
        candidate.collect_candidate(repo, newer, original)
    assert candidate.collect_candidate(repo, original, newer)[0]["commit"] == original


def test_selected_symlink_rejected_without_reading_its_target(source, tmp_path):
    repo, _ = source
    secret = tmp_path / "private.py"
    secret.write_text("must not be read")
    (repo / "server/link.py").symlink_to(secret)
    with pytest.raises(candidate.CandidateError, match="Links and non-regular"):
        candidate.collect_candidate(repo, commit_all(repo))


@pytest.mark.parametrize("kind", ["file", "total", "count"])
def test_size_and_count_limits_fail_before_artifact_publication(source, tmp_path, monkeypatch, kind):
    repo, commit = source
    if kind == "file":
        monkeypatch.setattr(candidate, "MAX_FILE_BYTES", 2)
    elif kind == "total":
        monkeypatch.setattr(candidate, "MAX_TOTAL_BYTES", 10)
    else:
        monkeypatch.setattr(candidate, "MAX_FILES", 1)
    output = tmp_path / "never-published"
    with pytest.raises(candidate.CandidateError, match="limit"):
        candidate.build_candidate(repo, commit, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "secret",
    [
        b"sk-" + b"A" * 32,
        b'API_KEY = "' + b"synthetic-test-marker" + b'"',
        b"-----BEGIN " + b"PRIVATE KEY-----",
        b"https://synthetic-user:synthetic-password@example.invalid",
        b'{"authorization":"Bearer ' + b"synthetic-opaque-fixture" + b'"}',
    ],
)
def test_sensitive_content_rejected_without_echoing_matches(source, tmp_path, secret):
    repo, _ = source
    write(repo, "server/example.py", secret)
    with pytest.raises(candidate.CandidateError) as failure:
        candidate.build_candidate(repo, commit_all(repo), tmp_path / "no-secrets")
    assert "server/example.py" in str(failure.value)
    assert secret.decode() not in str(failure.value)
    assert not (tmp_path / "no-secrets").exists()


def test_lfs_pointer_and_missing_runtime_file_rejected(source):
    repo, _ = source
    write(repo, "static/asset.png", b"version https://git-lfs.github.com/spec/v1\noid sha256:fixture\n")
    with pytest.raises(candidate.CandidateError, match="LFS"):
        candidate.collect_candidate(repo, commit_all(repo))
    (repo / "static/asset.png").unlink()
    (repo / "server/config.py").unlink()
    with pytest.raises(candidate.CandidateError, match="missing required"):
        candidate.collect_candidate(repo, commit_all(repo))


def test_version_is_parsed_without_executing_candidate(source, tmp_path):
    repo, _ = source
    sentinel = tmp_path / "executed"
    write(
        repo,
        "server/release.py",
        f'from pathlib import Path\nPath({str(sentinel)!r}).touch()\nAPP_VERSION = "v2.3.4"\n'.encode(),
    )
    manifest, _ = candidate.collect_candidate(repo, commit_all(repo))
    assert manifest["version"] == "v2.3.4" and not sentinel.exists()
    write(repo, "server/release.py", b'APP_VERSION = str("v2.3.4")\n')
    with pytest.raises(candidate.CandidateError, match="literal APP_VERSION"):
        candidate.collect_candidate(repo, commit_all(repo))
    assert not sentinel.exists()


def test_existing_outputs_never_overwritten(built):
    repo, commit, archive, manifest = built
    previous = archive.read_bytes(), manifest.read_bytes()
    with pytest.raises(candidate.CandidateError, match="already exists"):
        candidate.build_candidate(repo, commit, archive.parent)
    assert (archive.read_bytes(), manifest.read_bytes()) == previous


def test_file_hash_and_external_manifest_mismatch_rejected(built, tmp_path):
    _, _, archive, manifest = built
    items = entries(archive)
    items[1] = (items[1][0], b"# altered bytes\n")
    corrupt = repack(tmp_path / "corrupt.tar.gz", items)
    with pytest.raises(candidate.CandidateError, match="integrity mismatch"):
        candidate.verify_candidate(corrupt)
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(candidate.CandidateError, match="External manifest"):
        candidate.verify_candidate(archive, manifest)


def test_rehashed_forgery_is_self_consistent_but_fails_git_provenance(built, tmp_path):
    repo, commit, archive, _ = built
    items = entries(archive)
    manifest = json.loads(items[0][1])
    index = next(i for i, (info, _) in enumerate(items) if info.name == "server/main.py")
    altered = b"# forged candidate source\n"
    items[index] = (items[index][0], altered)
    entry = next(item for item in manifest["files"] if item["path"] == "server/main.py")
    manifest["total_size"] += len(altered) - entry["size"]
    entry.update(size=len(altered), sha256=hashlib.sha256(altered).hexdigest())
    replace_manifest(items, manifest)
    forged = repack(tmp_path / "forged.tar.gz", items)
    external = tmp_path / "forged.json"
    external.write_bytes(items[0][1])
    assert (
        candidate.verify_candidate(forged, external, expected_commit=commit)["provenance_verified"] is False
    )
    with pytest.raises(candidate.CandidateError, match="Git source tree"):
        candidate.verify_candidate(forged, external, expected_commit=commit, repo=repo)


@pytest.mark.parametrize(
    "attack",
    ["extra", "missing", "duplicate", "traversal", "absolute", "symlink", "hardlink", "device", "metadata"],
)
def test_archive_inventory_and_extraction_hazards_rejected(built, tmp_path, attack):
    _, _, archive, _ = built
    items = entries(archive)
    if attack == "extra":
        items.append((candidate._tar_info("server/extra.py", 0, 0o644), b""))
    elif attack == "missing":
        items.pop()
    elif attack == "duplicate":
        items.append(items[-1])
    elif attack in {"traversal", "absolute"}:
        items[-1][0].name = "../escape.py" if attack == "traversal" else "/absolute.py"
    elif attack in {"symlink", "hardlink", "device"}:
        items[-1][0].type = {
            "symlink": tarfile.SYMTYPE,
            "hardlink": tarfile.LNKTYPE,
            "device": tarfile.CHRTYPE,
        }[attack]
        items[-1][0].linkname = "../private"
        items[-1] = items[-1][0], b""
    else:
        items[-1][0].uname = "private-author"
    unsafe = repack(tmp_path / "unsafe.tar.gz", items)
    with pytest.raises(candidate.CandidateError):
        candidate.verify_candidate(unsafe)
    assert not (tmp_path / "escape.py").exists()


@pytest.mark.parametrize("attack", ["padding", "trailing_tar", "trailing_gzip", "gzip_filename", "gzip_bomb"])
def test_hidden_payload_and_resource_attacks_rejected(built, tmp_path, monkeypatch, attack):
    _, _, archive, _ = built
    raw = bytearray(gzip.decompress(archive.read_bytes()))
    if attack == "padding":
        manifest_size = len(entries(archive)[0][1])
        assert manifest_size % 512
        raw[512 + manifest_size] = 42
        payload = compressed(raw)
    elif attack == "trailing_tar":
        raw[-1] = 42
        payload = compressed(raw)
    elif attack == "trailing_gzip":
        payload = archive.read_bytes() + compressed(b"hidden")
    elif attack == "gzip_filename":
        stream = io.BytesIO()
        with gzip.GzipFile(
            fileobj=stream, mode="wb", filename="private-path", mtime=0, compresslevel=9
        ) as handle:
            handle.write(raw)
        payload = stream.getvalue()
    else:
        monkeypatch.setattr(candidate, "MAX_TAR_BYTES", 1024)
        payload = compressed(b"\0" * 8192)
    unsafe = tmp_path / "hidden.tar.gz"
    unsafe.write_bytes(payload)
    with pytest.raises(candidate.CandidateError):
        candidate.verify_candidate(unsafe)


@pytest.mark.parametrize(
    "attack",
    ["mode_type", "size_bool", "unexpected_key", "duplicate_key", "version_mismatch", "wrong_commit"],
)
def test_manifest_contract_and_expected_commit_enforced(built, tmp_path, attack):
    _, commit, archive, _ = built
    items = entries(archive)
    manifest = json.loads(items[0][1])
    if attack == "mode_type":
        manifest["files"][0]["mode"] = []
    elif attack == "size_bool":
        manifest["files"][0]["size"] = True
    elif attack == "unexpected_key":
        manifest["author_email"] = "test@example.invalid"
    elif attack == "version_mismatch":
        manifest["version"] = "v9.9.9"
    elif attack == "wrong_commit":
        manifest["commit"] = "f" * 40
    replace_manifest(items, manifest)
    if attack == "duplicate_key":
        items[0] = (
            items[0][0],
            items[0][1].replace(b'{"commit":', b'{"commit":"' + commit.encode() + b'","commit":', 1),
        )
    unsafe = repack(tmp_path / "manifest-attack.tar.gz", items)
    with pytest.raises(candidate.CandidateError):
        candidate.verify_candidate(unsafe, expected_commit=commit)


def test_cli_failure_is_clean_json_without_sensitive_values(source, tmp_path, capsys):
    repo, _ = source
    marker = "sk-" + "B" * 32
    write(repo, "server/leak.py", marker.encode())
    commit = commit_all(repo)
    assert (
        candidate.main(
            ["build", "--repo", str(repo), "--commit", commit, "--output-dir", str(tmp_path / "failed")]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert json.loads(output)["ok"] is False
    assert marker not in output


def test_source_binding_requires_explicit_commit(built):
    repo, _, archive, _ = built
    with pytest.raises(candidate.CandidateError, match="explicit expected commit"):
        candidate.verify_candidate(archive, repo=repo)


def test_missing_promisor_object_cannot_invoke_repository_transport(source, tmp_path):
    repo, _ = source
    write(repo, "server/missing.py", b"# unique unavailable promisor object\n")
    commit = commit_all(repo)
    object_id = git(repo, "rev-parse", commit + ":server/missing.py")
    marker = tmp_path / "transport-invoked"
    transport = tmp_path / "transport"
    transport.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    transport.chmod(0o755)
    git(repo, "config", "extensions.partialClone", "origin")
    git(repo, "config", "remote.origin.promisor", "true")
    git(repo, "config", "remote.origin.url", "ext::" + str(transport))
    git(repo, "config", "protocol.ext.allow", "always")
    (repo / ".git" / "objects" / object_id[:2] / object_id[2:]).unlink()
    with pytest.raises(candidate.CandidateError, match="Git object unavailable"):
        candidate.collect_candidate(repo, commit)
    assert not marker.exists()
