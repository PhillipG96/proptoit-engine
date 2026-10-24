from typing import Optional
"""
import logging

logger = logging.getLogger(__name__)

Input sanitization utilities.
Cleans user input to prevent injection attacks and malformed data.
"""
import re
from html import escape as html_escape


def sanitize_email(email: str) -> str | None:
    """Normalize and validate an email address. Returns None if invalid."""
    if not email or not isinstance(email, str):
        return None
    email = email.strip().lower()
    if not re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email):
        return None
    # Reject obviously malicious patterns
    if '..' in email or email.startswith('.') or email.endswith('.'):
        return None
    return email


def sanitize_name(name: str) -> str | None:
    """Sanitize a display name. Max 100 chars, alphanumeric + common chars."""
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    if len(name) > 100 or len(name) < 1:
        return None
    # Only allow safe characters
    if not re.match(r"^[a-zA-Z0-9\s\-_'.,&@!()]+$", name):
        return None
    return name


def sanitize_text(text: str, max_length: int = 1000) -> str:
    """Escape HTML entities and truncate. Prevents XSS via stored user text."""
    if not text or not isinstance(text, str):
        return ""
    text = text[:max_length]
    return html_escape(text, quote=True)


def sanitize_query_param(value: str, max_length: int = 255) -> str:
    """
    Sanitize a query parameter value.
    - Truncates to max_length
    - Strips leading/trailing whitespace
    - Removes null bytes
    - Allows URL-safe characters
    Returns empty string if value is None or not a string.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    # Strip whitespace and remove null bytes
    value = value.strip().replace('\x00', '')
    # Truncate
    return value[:max_length]


def sanitize_url_path(path: str) -> str:
    """
    Sanitize a URL path segment (e.g., parcel_id, county_id).
    - Only allows alphanumeric, dots, hyphens, underscores, slashes, AND spaces
    - Prevents directory traversal
    - Truncates to 255 chars
    """
    if not path or not isinstance(path, str):
        return ""
    # Prevent directory traversal
    path = path.replace('..', '').replace('...', '')
    # Remove null bytes
    path = path.replace('\x00', '')
    # Strip
    path = path.strip()
    # Only allow safe characters (added space — parcel IDs contain spaces)
    path = re.sub(r'[^a-zA-Z0-9._/ -]', '', path)
    return path[:255]


def sanitize_list_param(value: str, max_length: int = 255, max_items: int = 20) -> list[str]:
    """
    Sanitize a comma-separated list parameter (e.g., distress_types).
    - Splits on comma
    - Strips each item
    - Truncates to max_length per item
    - Limits total items
    Returns empty list if value is empty.
    """
    if not value or not isinstance(value, str):
        return []
    items = [sanitize_query_param(item.strip(), max_length) for item in value.split(',')]
    # Filter empty strings
    items = [i for i in items if i]
    return items[:max_items]


def sanitize_number(value, min_val=None, max_val=None, default=0) -> float | int | None:
    """
    Sanitize a numeric parameter.
    Returns the value clamped between min_val and max_val, or default if invalid.
    """
    if value is None:
        return default
    try:
        num = float(value)
    except (ValueError, TypeError):
        return default
    if min_val is not None and num < min_val:
        return default
    if max_val is not None and num > max_val:
        return default
    return int(num) if num == int(num) else num


def strip_html_tags(text: str, max_length: int = 5000) -> str:
    """Remove all HTML tags from text. Prevents injection via stored HTML."""
    if not text or not isinstance(text, str):
        return ""
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Remove HTML entities
    import html
    text = html.unescape(text)
    # Strip whitespace
    text = text.strip()
    return text[:max_length]


def validate_county_id(county_id: str) -> str | None:
    """Validate a county_id parameter. Only known counties allowed."""
    if not county_id or not isinstance(county_id, str):
        return "hamilton"  # default
    county_id = county_id.strip().lower()
    # Only allow alphanumeric and underscores
    if not re.match(r'^[a-z0-9_]+$', county_id):
        return "hamilton"  # default
    return county_id[:50]


def sanitize_ip_address(ip: str) -> str:
    """Validate and sanitize an IP address. Returns 'unknown' if invalid."""
    if not ip or not isinstance(ip, str):
        return "unknown"
    ip = ip.strip()
    # Remove port if present
    if ':' in ip:
        ip = ip.split(':')[0]
    # Validate IPv4 or IPv6
    try:
        import ipaddress
        ipaddress.ip_address(ip)
        return ip
    except ValueError:
        return "unknown"
