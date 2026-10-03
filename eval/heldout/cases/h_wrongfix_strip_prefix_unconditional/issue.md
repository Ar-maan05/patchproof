# strip_prefix removes too much

`strip_prefix("foofoobar", "foo")` returns `"bar"`; expected `"foobar"` (only one leading `foo` removed).

If the string does not start with the prefix it should be returned unchanged.
