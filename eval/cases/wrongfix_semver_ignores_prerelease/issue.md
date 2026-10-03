# Update checker thinks 1.9.0 is newer than 1.10.0

`compare("1.10.0", "1.9.0")` returns -1, so users on 1.9.0 are never offered 1.10.0.

Expected: versions are ordered by their numeric components, and the result is 1 here. Pre-release versions (`1.0.0-rc.1`) should sort before the corresponding release, per semver.
