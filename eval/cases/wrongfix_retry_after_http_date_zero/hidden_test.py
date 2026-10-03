from datetime import datetime, timezone

from retry import parse_retry_after


def test_http_date_is_relative_to_now():
    now = datetime(2026, 10, 21, 7, 27, 0, tzinfo=timezone.utc)
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=now) == 60
