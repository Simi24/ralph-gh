"""Branch names. Slugs contain only [a-z0-9-] and are at most 40 chars."""
import re

SLUG_MAX = 40


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:SLUG_MAX].strip("-")


def integration_branch(prefix: str, prd: int, title: str) -> str:
    return f"{prefix}/{prd}-{slugify(title)}"


def ticket_branch(prefix: str, prd: int, ticket: int) -> str:
    return f"{prefix}/{prd}-ticket-{ticket}"
