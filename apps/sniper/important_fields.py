"""
important_fields.py

Central place for "what counts as a business-relevant control" so the
JS recorder script and any Python-side post-processing agree on the
same list. Keeping this in one file means we never have the JS and
Python definitions drift apart.
"""

# Keywords matched (case-insensitive, substring) against an element's
# id / name / placeholder / aria-label / associated <label> text /
# nearest preceding table-cell text.
IMPORTANT_KEYWORDS = [
    "customer", "recipient", "phone", "whatsapp", "email", "address",
    "state", "product", "variant", "quantity", "price", "discount",
    "delivery", "payment", "save", "submit", "delete", "confirm",
    "cancel", "search", "tag", "qty",
]

# Network requests are only recorded if the URL matches one of these
# (business-logic endpoints). Everything else -- css, fonts, images,
# analytics, AV/security vendor beacons, etc. -- is ignored outright.
NETWORK_INCLUDE_PATTERNS = [
    r"/save_order",
    r"/create_customer",
    r"/add_multi_order",
    r"/ajaxDataMulti(?:States)?\.php",
    r"/update_order",
    r"/delete_order",
]

# Belt-and-braces exclude list, checked even if an include pattern
# somehow matches (e.g. a font file that happens to live under /ajax/).
NETWORK_EXCLUDE_EXTENSIONS = (
    ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2",
    ".ttf", ".ico", ".map",
)

NETWORK_EXCLUDE_DOMAINS = [
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "google-analytics.com",
    "googletagmanager.com",
    "kaspersky-labs.com",
    "doubleclick.net",
    "facebook.net",
    "hotjar.com",
]

# DOM nodes we watch for after an action (Save, Submit, etc.) to decide
# what actually happened, instead of just logging "Clicked Save".
SUCCESS_INDICATOR_SELECTORS = [
    ".swal2-success", ".swal2-icon-success",
    ".swal2-error", ".swal2-icon-error",
    ".toast-success", ".toast-error", ".toast-message",
    ".alert-success", ".alert-danger",
    "[role='alert']",
]


def is_important_text(text: str) -> bool:
    """Return True if the given label/name/placeholder text matches an
    important-field keyword."""
    if not text:
        return False
    lowered = text.lower()
    return any(keyword in lowered for keyword in IMPORTANT_KEYWORDS)