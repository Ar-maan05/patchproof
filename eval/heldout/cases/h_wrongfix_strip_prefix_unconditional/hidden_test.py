from prefix import strip_prefix


def test_non_matching_string_unchanged():
    assert strip_prefix("barfoo", "foo") == "barfoo"
