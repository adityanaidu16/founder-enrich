"""SMTP RCPT TO verification with catch-all detection.

Limitations to know about:
  - Many home/ISP networks block outbound port 25, so this can fail entirely
    on a teammate's laptop. We surface "smtp-blocked" rather than pretending.
  - Google Workspace tends to accept any RCPT regardless of validity, so
    deliverability cannot be confirmed for google-hosted domains. We still
    detect catch-all behavior and return a degraded confidence instead.
"""
from __future__ import annotations

import random
import smtplib
import socket
import string
from dataclasses import dataclass
from typing import List, Optional

SMTP_TIMEOUT = 8
HELO_HOST = "founder-enrich.local"
MAIL_FROM = "verify@founder-enrich.local"


@dataclass
class VerifyResult:
    deliverable: Optional[bool]  # True/False/None (couldn't check)
    catch_all: Optional[bool]
    reason: str  # one-line human note


def verify(email: str, mx_hosts: List[str]) -> VerifyResult:
    if not mx_hosts:
        return VerifyResult(None, None, "no-mx")

    domain = email.split("@", 1)[1]
    probe_local = "".join(random.choices(string.ascii_lowercase, k=14))
    probe_email = f"{probe_local}-no-such-mailbox@{domain}"

    for host in mx_hosts[:2]:
        try:
            with smtplib.SMTP(host, 25, timeout=SMTP_TIMEOUT) as smtp:
                smtp.ehlo(HELO_HOST)
                code_from, _ = smtp.mail(MAIL_FROM)
                if code_from >= 400:
                    return VerifyResult(None, None, f"mail-from-rejected:{code_from}")

                code_real, _ = smtp.rcpt(email)
                code_probe, _ = smtp.rcpt(probe_email)

                real_ok = 200 <= code_real < 300
                probe_ok = 200 <= code_probe < 300

                if probe_ok:
                    # Server accepts unknown mailboxes — can't confirm anything.
                    return VerifyResult(
                        deliverable=None if real_ok else False,
                        catch_all=True,
                        reason="catch-all",
                    )
                if real_ok:
                    return VerifyResult(True, False, "rcpt-accepted")
                return VerifyResult(False, False, f"rcpt-rejected:{code_real}")
        except (socket.timeout, smtplib.SMTPServerDisconnected):
            continue
        except (smtplib.SMTPException, ConnectionRefusedError, OSError) as e:
            return VerifyResult(None, None, f"smtp-error:{type(e).__name__}")
    return VerifyResult(None, None, "smtp-blocked")
