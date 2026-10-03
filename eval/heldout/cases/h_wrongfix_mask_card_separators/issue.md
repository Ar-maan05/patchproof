# mask_card hides the last four digits too

`mask_card("4111111111111111")` returns all asterisks. Support needs to see the last four digits, e.g. `************1111`.

Cards are also entered with dashes (`4111-1111-1111-1111`); the dashes should stay in place so the grouping is still readable: `****-****-****-1111`.
