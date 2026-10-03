from cards import mask_card


def test_separators_are_preserved():
    assert mask_card("4111-1111-1111-1111") == "****-****-****-1111"
