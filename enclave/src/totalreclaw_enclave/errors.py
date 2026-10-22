"""Exception base whose message is safe to log.

``logs.RedactionFilter`` drops the message of every exception except
subclasses of ``SafeMessageError``: their messages are developer-written
constants that never interpolate a runtime value (enforced by review; the
message still passes ``redaction.scrub_text``).
"""


class SafeMessageError(Exception):
    pass
