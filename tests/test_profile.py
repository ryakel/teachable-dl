"""Working from a copy of a Chrome profile."""

import json
import os
import pathlib

import pytest

from teachable_dl.profile import (
    ProfileError,
    detect_profile_name,
    discard_copy,
    list_profiles,
    prepare_copy,
    remove_locks,
)


def build_profile(root, name="Default", with_cache=True):
    """Something shaped like a real Chrome user-data directory."""
    profile = pathlib.Path(root) / name
    (profile / "Network").mkdir(parents=True)
    (profile / "Preferences").write_text(json.dumps({"profile": {"name": name}}))
    (profile / "Cookies").write_bytes(b"SQLite format 3\x00")
    (profile / "Network" / "Cookies").write_bytes(b"SQLite format 3\x00")
    (pathlib.Path(root) / "Local State").write_text(json.dumps({"os_crypt": {}}))
    (profile / "SingletonLock").write_text("held")
    if with_cache:
        cache = profile / "Cache"
        cache.mkdir()
        (cache / "big.bin").write_bytes(b"0" * (1024 * 1024))
    return pathlib.Path(root)


def test_profiles_are_listed_by_their_preferences_file(tmp_path):
    build_profile(tmp_path, "Default")
    build_profile(tmp_path, "Profile 1")
    (tmp_path / "ShaderCache").mkdir()          # not a profile
    assert list_profiles(str(tmp_path)) == ["Default", "Profile 1"]


def test_default_profile_is_preferred(tmp_path):
    build_profile(tmp_path, "Profile 1")
    build_profile(tmp_path, "Default")
    assert detect_profile_name(str(tmp_path)) == "Default"


def test_a_named_profile_is_honoured(tmp_path):
    build_profile(tmp_path, "Default")
    build_profile(tmp_path, "Profile 3")
    assert detect_profile_name(str(tmp_path), "Profile 3") == "Profile 3"


def test_an_unknown_profile_name_lists_the_real_ones(tmp_path):
    build_profile(tmp_path, "Default")
    with pytest.raises(ProfileError) as caught:
        detect_profile_name(str(tmp_path), "Nope")
    assert "Default" in str(caught.value)


def test_the_copy_carries_the_session(tmp_path):
    source = build_profile(tmp_path / "src")
    copy, name = prepare_copy(str(source))
    try:
        assert (pathlib.Path(copy) / name / "Cookies").is_file()
        assert (pathlib.Path(copy) / name / "Network" / "Cookies").is_file()
        # Local State holds the key Chrome encrypts cookies with; without it
        # the copied cookies cannot be read.
        assert (pathlib.Path(copy) / "Local State").is_file()
    finally:
        discard_copy(copy)


def test_the_copy_leaves_the_locks_behind(tmp_path):
    """Chrome refuses to open a profile that still looks locked."""
    source = build_profile(tmp_path / "src")
    copy, name = prepare_copy(str(source))
    try:
        assert not (pathlib.Path(copy) / name / "SingletonLock").exists()
    finally:
        discard_copy(copy)


def test_the_copy_leaves_the_cache_behind(tmp_path):
    """A real profile runs to gigabytes; none of it is the session."""
    source = build_profile(tmp_path / "src")
    copy, name = prepare_copy(str(source))
    try:
        assert not (pathlib.Path(copy) / name / "Cache").exists()
        size = sum(f.stat().st_size for f in pathlib.Path(copy).rglob("*") if f.is_file())
        assert size < 100 * 1024
    finally:
        discard_copy(copy)


def test_the_original_profile_is_never_touched(tmp_path):
    source = build_profile(tmp_path / "src")
    before = {p.relative_to(source) for p in source.rglob("*")}
    copy, _ = prepare_copy(str(source))
    try:
        assert {p.relative_to(source) for p in source.rglob("*")} == before
        assert (source / "Default" / "SingletonLock").exists()   # untouched
    finally:
        discard_copy(copy)


def test_a_directory_that_is_not_a_profile_is_refused(tmp_path):
    with pytest.raises(ProfileError):
        prepare_copy(str(tmp_path / "does-not-exist"))

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ProfileError):
        prepare_copy(str(empty))


def test_locks_are_removed_recursively(tmp_path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "SingletonCookie").write_text("x")
    (tmp_path / "lockfile").write_text("x")
    remove_locks(str(tmp_path))
    assert not (nested / "SingletonCookie").exists()
    assert not (tmp_path / "lockfile").exists()


def test_discarding_a_copy_is_safe_to_repeat(tmp_path):
    source = build_profile(tmp_path / "src")
    copy, _ = prepare_copy(str(source))
    discard_copy(copy)
    discard_copy(copy)          # must not raise
    discard_copy(None)
    assert not os.path.exists(copy)
