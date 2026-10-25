"""Text that leaves the machine or is kept from a microphone loses anything that looks like a number or an email."""
import re

_DIGIT_WORDS = (
    "zero|oh|one|two|three|four|five|six|seven|eight|nine|"
    "cero|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve"
)
_TOKEN = rf"(?:[0-9]|\b(?:{_DIGIT_WORDS})\b)"
# Four or more digits in a row, written or spoken, with spaces, dots, commas, brackets, slashes or dashes
# between them. "press 1, 2, 3, 4" in prompt text is caught too: that is the accepted price of catching
# "415, 555, 1234". Option keys are never redacted.
DIGIT_RUN = re.compile(rf"{_TOKEN}(?:[ \t\u00a0\u2000-\u200a\u202f\u205f\u3000.,()/\-]*{_TOKEN}){{3,}}", re.IGNORECASE | re.ASCII)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def redact(text: str) -> str:
    """Repeated until nothing changes: removing one run can leave its neighbours forming a new one."""
    for _ in range(10):
        out = DIGIT_RUN.sub("[number]", EMAIL.sub("[email]", text))
        if out == text:
            break
        text = out
    return text
