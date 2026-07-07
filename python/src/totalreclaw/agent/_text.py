"""Small text helpers shared across the agent layer."""
from __future__ import annotations


def truncate_messages(messages: list[dict], max_chars: int = 12000) -> str:
    """Format and truncate messages to fit token budget."""
    lines = []
    total = 0
    for msg in messages:
        line = f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n\n".join(lines)
