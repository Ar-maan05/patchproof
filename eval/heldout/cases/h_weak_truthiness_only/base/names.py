"""User display names."""


def display_name(user: dict) -> str:
    return user["first"] + " " + user["last"]
