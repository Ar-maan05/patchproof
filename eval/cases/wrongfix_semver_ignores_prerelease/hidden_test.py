from semver import compare


def test_prerelease_is_older_than_release():
    assert compare("1.0.0-rc.1", "1.0.0") == -1


def test_prerelease_ordering():
    assert compare("1.0.0-alpha", "1.0.0-beta") == -1
