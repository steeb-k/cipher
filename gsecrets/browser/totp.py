# SPDX-License-Identifier: GPL-3.0-only
"""Generating one-time passwords for browser requests.

The protocol layer works with pykeepass entries rather than the application's
SafeEntry wrappers, so it cannot use SafeEntry.otp_token(). The parsing here
mirrors SafeEntry's: an entry's otp field holds either a full otpauth:// URI or
a bare base32 secret, and both are accepted.
"""

from __future__ import annotations

import binascii
import logging

from pyotp import TOTP, parse_uri


def _parse(otp_uri: str) -> TOTP | None:
    try:
        if otp_uri.startswith("otpauth://"):
            return parse_uri(otp_uri)  # type: ignore[return-value]
        return TOTP(otp_uri)
    except ValueError:
        logging.debug("Could not parse OTP field")
        return None


def has_totp(entry) -> bool:
    """Whether `entry` has a usable one-time password configured."""
    otp_uri = getattr(entry, "otp", None)
    return bool(otp_uri) and _parse(otp_uri) is not None


def current_token(entry) -> str | None:
    """Return the one-time password for `entry` now, or None.

    Returns None rather than raising for an unparseable or malformed secret;
    a broken OTP field on one entry must not fail a whole credential lookup.
    """
    otp_uri = getattr(entry, "otp", None)
    if not otp_uri:
        return None

    totp = _parse(otp_uri)
    if totp is None:
        return None

    try:
        return totp.now()
    except binascii.Error:
        # An invalid base32 secret. SafeEntry.otp_token() swallows this too.
        logging.debug("Could not generate OTP (likely an invalid base32 secret)")
        return None
