"""HTTP status classification for metrics labels."""


def classify(status: int) -> str:
    if status >= 500:
        return "server_error"
    if status >= 400:
        return "client_error"
    if status >= 300:
        return "redirect"
    if status >= 200:
        return "success"
    return "informational"
