"""Active PII Redaction pipeline for logs, session state, and long-term memory.

Combines deterministic regular-expression scrubbing (always active, zero latency)
with optional Google Cloud Sensitive Data Protection (Cloud DLP API) inspection
and de-identification before any payload is written to logs or persistent memory.
"""

from __future__ import annotations

import re
from typing import Any

from .config import get_settings

try:
    from google.cloud import dlp_v2
except ImportError:  # pragma: no cover - optional when Cloud DLP package unavailable
    dlp_v2 = None  # type: ignore[assignment]

# Pre-compiled sensitive data patterns (order matters: card numbers before phones)
_CREDIT_CARD_RE = re.compile(
    r"\b(?:\d[ -]*?){13,19}\b"
)
_SSN_RE = re.compile(
    r"\b\d{3}-\d{2}-\d{4}\b"
)
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}(?!\d)"
)
_CVV_RE = re.compile(
    r"(?i)\b(?:cvv|cvc|security\s+code)\s*[:=#]?\s*\d{3,4}\b"
)

SENSITIVE_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "card_number",
        "credit_card",
        "cvv",
        "password",
        "secret",
        "ssn",
        "token",
    }
)


class PIIRedactor:
    """Scrubs PII from free-form text and structured dictionaries/lists."""

    def __init__(
        self,
        enable_cloud_dlp: bool | None = None,
        gcp_project_id: str | None = None,
    ) -> None:
        settings = get_settings()
        self.enable_cloud_dlp = (
            settings.enable_cloud_dlp if enable_cloud_dlp is None else enable_cloud_dlp
        )
        self.gcp_project_id = (
            settings.google_cloud_project if gcp_project_id is None else gcp_project_id
        )

    def scrub_text(self, text: str) -> str:
        """Redact sensitive tokens from a string using regex + optional Cloud DLP."""
        if not text:
            return text

        # 1. Always apply fast local deterministic regex scrubbing first
        scrubbed = _CVV_RE.sub("[REDACTED_CVV]", text)
        scrubbed = _SSN_RE.sub("[REDACTED_SSN]", scrubbed)
        scrubbed = _CREDIT_CARD_RE.sub("[REDACTED_PAYMENT_CARD]", scrubbed)
        scrubbed = _EMAIL_RE.sub("[REDACTED_EMAIL]", scrubbed)
        scrubbed = _PHONE_RE.sub("[REDACTED_PHONE]", scrubbed)

        # 2. Optionally run Google Cloud Sensitive Data Protection (DLP) API
        if self.enable_cloud_dlp and self.gcp_project_id and dlp_v2 is not None:
            scrubbed = self._deidentify_via_cloud_dlp(scrubbed)

        return scrubbed

    def _deidentify_via_cloud_dlp(self, text: str) -> str:
        """Invoke Google Cloud DLP deidentify_content API with graceful fallback."""
        try:
            client = dlp_v2.DlpServiceClient()
            parent = f"projects/{self.gcp_project_id}/locations/global"
            inspect_config = {
                "info_types": [
                    {"name": "EMAIL_ADDRESS"},
                    {"name": "PHONE_NUMBER"},
                    {"name": "CREDIT_CARD_NUMBER"},
                    {"name": "US_SOCIAL_SECURITY_NUMBER"},
                    {"name": "STREET_ADDRESS"},
                    {"name": "PERSON_NAME"},
                ]
            }
            deidentify_config = {
                "info_type_transformations": {
                    "transformations": [
                        {
                            "primitive_transformation": {
                                "replace_with_info_type_config": {}
                            }
                        }
                    ]
                }
            }
            response = client.deidentify_content(
                request={
                    "parent": parent,
                    "inspect_config": inspect_config,
                    "deidentify_config": deidentify_config,
                    "item": {"value": text},
                }
            )
            return response.item.value
        except Exception:
            # Regex scrubbing has already executed, so fail safe without leaking PII
            return text

    def scrub_payload(self, payload: Any) -> Any:
        """Recursively redact PII across dicts, lists, tuples, and strings."""
        if isinstance(payload, str):
            return self.scrub_text(payload)
        if isinstance(payload, dict):
            sanitized: dict[Any, Any] = {}
            for key, value in payload.items():
                key_lower = str(key).lower()
                if key_lower in SENSITIVE_KEY_NAMES:
                    sanitized[key] = "[REDACTED_SECRET]"
                else:
                    sanitized[key] = self.scrub_payload(value)
            return sanitized
        if isinstance(payload, list):
            return [self.scrub_payload(item) for item in payload]
        if isinstance(payload, tuple):
            return tuple(self.scrub_payload(item) for item in payload)
        return payload


# Shared singleton redactor used across logging, guardrails, and memory pipelines
pii_redactor = PIIRedactor()
