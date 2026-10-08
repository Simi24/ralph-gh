"""The `## Gate verdict` comment body. GitHub caps comments at 65536 chars,
so the last 60000 bytes are kept (the end holds the blocking findings)."""

CAP = 60000


def verdict_comment(text: str, label: str) -> str:
    data = text.encode()
    header = f"## Gate verdict ({label})\n\n"
    if len(data) <= CAP:
        return header + text
    kept = data[-CAP:].decode(errors="ignore")
    omitted = len(data) - CAP
    return header + f"_Truncated: the first {omitted} bytes are omitted; the full verdict is in the conductor's state dir._\n\n" + kept
