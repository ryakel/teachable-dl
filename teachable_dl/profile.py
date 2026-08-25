"""Working from a copy of a Chrome profile.

Driving someone's live Chrome profile directly does not work reliably. Chrome
holds singleton locks on it, background processes keep it open after the last
window closes, and SeleniumBase does its own profile juggling on top -- the
symptoms are "Unable to set user_data_dir while starting Chrome!" followed by
``DevToolsActivePort file doesn't exist``, or a browser that simply sits on its
start page.

Copying the profile removes every one of those problems at once: nothing is
locked, the real profile is never touched, and the browser can stay open while
a download runs. Only the parts that carry the session are copied, so the copy
is small even when the original is not.
"""

import logging
import os
import shutil
import tempfile

logger = logging.getLogger(__name__)

#: Files at the top of the user-data directory that a session needs.
#: ``Local State`` holds the key Chrome encrypts its cookies with.
ROOT_FILES = ("Local State", "First Run")

#: Per-profile files worth copying. Everything else is cache, history and
#: several gigabytes of things a download does not care about.
PROFILE_FILES = (
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Login Data For Account",
    "Preferences",
    "Secure Preferences",
    "Web Data",
    "Network Persistent State",
    "TransportSecurity",
    "Trust Tokens",
)

#: Subdirectories inside a profile that matter for staying signed in.
PROFILE_DIRS = ("Network", "Sessions", "Local Storage", "IndexedDB")

#: Lock files that make Chrome refuse to open a copied profile.
LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile")


class ProfileError(RuntimeError):
    """The Chrome profile could not be prepared."""


def _copy_if_present(source, destination):
    if os.path.isdir(source):
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True,
                        ignore_dangling_symlinks=True)
        return True
    if os.path.isfile(source):
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination)
        return True
    return False


def detect_profile_name(user_data_dir, requested=None):
    """Pick which profile inside the user-data directory to use."""
    if requested:
        if not os.path.isdir(os.path.join(user_data_dir, requested)):
            available = list_profiles(user_data_dir)
            raise ProfileError(
                f"No profile named {requested!r} in {user_data_dir!r}. "
                f"Available: {', '.join(available) or 'none'}"
            )
        return requested

    for candidate in ("Default", "Profile 1"):
        if os.path.isdir(os.path.join(user_data_dir, candidate)):
            return candidate

    available = list_profiles(user_data_dir)
    if available:
        return available[0]
    raise ProfileError(f"No Chrome profile found inside {user_data_dir!r}")


def list_profiles(user_data_dir):
    """Profile directories that actually look like profiles."""
    try:
        entries = sorted(os.listdir(user_data_dir))
    except OSError:
        return []
    return [
        name
        for name in entries
        if os.path.isfile(os.path.join(user_data_dir, name, "Preferences"))
    ]


def prepare_copy(user_data_dir, profile_name=None, destination=None):
    """Copy the session-bearing parts of a profile, returning ``(dir, name)``.

    The copy is what the browser is pointed at, so the real profile is never
    locked, modified, or at risk.
    """
    user_data_dir = os.path.expanduser(user_data_dir)
    if not os.path.isdir(user_data_dir):
        raise ProfileError(f"No Chrome user-data directory at {user_data_dir!r}")

    name = detect_profile_name(user_data_dir, profile_name)
    target = destination or tempfile.mkdtemp(prefix="teachable-dl-profile-")
    target_profile = os.path.join(target, name)
    os.makedirs(target_profile, exist_ok=True)

    for filename in ROOT_FILES:
        _copy_if_present(os.path.join(user_data_dir, filename),
                         os.path.join(target, filename))

    copied = 0
    source_profile = os.path.join(user_data_dir, name)
    for filename in PROFILE_FILES:
        if _copy_if_present(os.path.join(source_profile, filename),
                            os.path.join(target_profile, filename)):
            copied += 1
    for dirname in PROFILE_DIRS:
        _copy_if_present(os.path.join(source_profile, dirname),
                         os.path.join(target_profile, dirname))

    remove_locks(target)

    if not copied:
        raise ProfileError(
            f"Copied nothing usable out of {source_profile!r}. Is that really a "
            "Chrome profile directory?"
        )

    logger.info("Prepared a copy of Chrome profile %r (%s item(s))", name, copied)
    return target, name


def remove_locks(directory):
    """Drop the lock files that stop Chrome opening a copied profile."""
    for current, _dirs, files in os.walk(directory):
        for filename in files:
            if filename in LOCK_NAMES:
                try:
                    os.remove(os.path.join(current, filename))
                except OSError:
                    pass


def discard_copy(path):
    """Remove a prepared copy, best effort."""
    if not path:
        return
    shutil.rmtree(path, ignore_errors=True)
