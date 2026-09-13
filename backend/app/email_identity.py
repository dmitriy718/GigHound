"""Canonical mailbox identities for new accounts and invitations.

Supports ordinary ASCII local parts and internationalized DNS domains. This
validates syntax only; delivery and ownership require the verification flow.
"""
import re
from email.errors import HeaderParseError
from email.headerregistry import Address


def normalize_email(value: str) -> str:
    if any(char in value for char in '\r\n\x00'):
        raise ValueError('enter a valid email address')
    value = value.strip()
    try:
        address = Address(addr_spec=value)
        domain = address.domain.encode('idna').decode('ascii').lower()
        labels = domain.split('.')
        if (not address.username or len(address.username.encode('ascii')) > 64
                or len(labels) < 2 or len(domain) > 253
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
            raise ValueError('invalid mailbox')
        normalized = Address(username=address.username.lower(), domain=domain).addr_spec
        if len(normalized) > 254:
            raise ValueError('mailbox too long')
        return normalized
    except (ValueError, UnicodeError, HeaderParseError):
        raise ValueError('enter a valid email address') from None
