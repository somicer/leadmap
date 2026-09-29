"""Phone number normalization for Iranian numbers."""
import re

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_MOBILE_RE = re.compile(r"^09\d{9}$")


def to_latin_digits(s: str) -> str:
    return s.translate(_DIGITS)


def normalize_phone(raw: str | None) -> str | None:
    """Return a normalized number (leading 0, digits only) or None."""
    if not raw:
        return None
    s = to_latin_digits(raw)
    # Drop bidi marks and everything that is not a digit or a leading '+'.
    s = s.strip()
    plus = s.startswith("+")
    s = re.sub(r"\D", "", s)
    if not s:
        return None
    if plus and s.startswith("98"):
        s = "0" + s[2:]
    elif s.startswith("0098"):
        s = "0" + s[4:]
    elif s.startswith("98") and len(s) >= 12:
        s = "0" + s[2:]
    elif not s.startswith("0") and len(s) == 10 and s.startswith("9"):
        s = "0" + s  # 912xxxxxxx
    return s


def is_mobile(phone: str | None) -> bool:
    return bool(phone and _MOBILE_RE.match(phone))
