import pytest

from leadmap.phone import is_mobile, normalize_phone


@pytest.mark.parametrize("raw,expected", [
    ("۰۹۱۲ ۳۴۵ ۶۷۸۹", "09123456789"),
    ("٠٩١٢-٣٤٥-٦٧٨٩", "09123456789"),
    ("+98 912 345 6789", "09123456789"),
    ("+۹۸ ۹۱۲ ۳۴۵ ۶۷۸۹", "09123456789"),
    ("0098 912 345 6789", "09123456789"),
    ("98 912 345 6789", "09123456789"),
    ("9123456789", "09123456789"),
    ("026 3456 7890", "02634567890"),
    ("+98 26 3456 7890", "02634567890"),
    ("‎026-34567890‏", "02634567890"),
    ("(026) 3456-7890", "02634567890"),
    ("", None),
    (None, None),
    ("---", None),
])
def test_normalize(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("phone,mobile", [
    ("09123456789", True),
    ("09901234567", True),
    ("02634567890", False),
    ("0912345678", False),
    ("091234567890", False),
    (None, False),
])
def test_is_mobile(phone, mobile):
    assert is_mobile(phone) is mobile
