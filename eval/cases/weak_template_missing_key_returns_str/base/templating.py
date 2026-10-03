"""Tiny str.format-based templating for notification emails."""


def render(template: str, ctx: dict) -> str:
    return template.format(**ctx)
