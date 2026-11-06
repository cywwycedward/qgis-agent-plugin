"""How one pydantic failure reads to a caller.

Both failures that reach a caller -- the command envelope and a command's params
-- are rendered here, so the rule that the caller's input and pydantic's
documentation link stay out of the message cannot drift between them.  The error
type stays: it is the part a caller can branch on, unlike the wording.
"""

from __future__ import annotations

from pydantic import ValidationError


def describe(error: ValidationError) -> str:
    """Render every failure, one per line, naming the field it belongs to.

    A caller that sent three bad fields learns about all three at once instead
    of one retry at a time.  The rejected value and the documentation link are
    left out: this line reaches an agent's context.
    """

    lines = []
    for item in error.errors(include_url=False, include_input=False):
        field = ".".join(str(part) for part in item["loc"])
        detail = f"{item['msg']} [{item['type']}]"
        lines.append(f"{field}: {detail}" if field else detail)
    return "\n".join(lines)
