# Last day of the billing period is billed to the next period

`in_period(date(2026, 3, 31), date(2026, 3, 1), date(2026, 3, 31))` is False. The period is documented as inclusive on both ends, so 31 March should be inside it.
