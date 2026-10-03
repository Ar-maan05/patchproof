# truncate_utf8 raises UnicodeDecodeError

`truncate_utf8("日本語", 4)` raises `UnicodeDecodeError: unexpected end of data`. The 4-byte limit lands in the middle of the second character.

Expected: the longest prefix that is valid text and fits in the byte budget, so here `"日"`. No garbage characters in the output.
