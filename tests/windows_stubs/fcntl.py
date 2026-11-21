"""Test-only stand-in for the POSIX fcntl module (Home Assistant does not support Windows)."""

LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


def flock(fd, operation):
    return None
