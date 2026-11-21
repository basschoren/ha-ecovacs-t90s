"""Test-only stand-in for the POSIX resource module."""

RLIMIT_NOFILE = 7
RLIM_INFINITY = -1


def getrlimit(resource):
    return (4096, 4096)


def setrlimit(resource, limits):
    return None
