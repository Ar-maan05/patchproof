# Client crashes on HTTP-date Retry-After

A 503 response with `Retry-After: Wed, 21 Oct 2026 07:28:00 GMT` makes `parse_retry_after` raise `ValueError`. The header is legal per RFC 9110 (either delta-seconds or an HTTP-date).

Expected: the number of seconds until that date, relative to `now` (default: current time). Delta-seconds keep working.
