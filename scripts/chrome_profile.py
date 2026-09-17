"""Where a Chrome profile and its two bookmark stores live, per operating system."""

import os
import sys


def user_data_dir(platform=None, environ=None, home=None):
    """Chrome's User Data directory for this platform."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home = home or os.path.expanduser("~")
    if platform == "win32":
        local = environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(local, "Google", "Chrome", "User Data")
    if platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Google", "Chrome")
    config = environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(config, "google-chrome")


def profile_dir(profile, **kwargs):
    """A profile folder name such as 'Default' or 'Profile 1', or a path to one."""
    looks_like_path = os.path.isabs(profile) or "/" in profile or "\\" in profile
    return profile if looks_like_path else os.path.join(user_data_dir(**kwargs), profile)


def stores(profile_path):
    """(account store path, device store path). The account store may not exist."""
    return (os.path.join(profile_path, "AccountBookmarks"),
            os.path.join(profile_path, "Bookmarks"))
