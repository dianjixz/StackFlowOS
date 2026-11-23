"""Unified credential redaction for human-facing previews and audits (M8).

The worker tool executor uses :func:`redact_text` for approval previews and
``tool.invoked`` arguments; the operator assistant uses the same rules for its
audited arguments.  One implementation means a secret is masked the same way
no matter which path it takes.

Design constraints:

* **Pure and total** - no I/O, no configuration, never raises on arbitrary
  text.
* **Idempotent** - ``redact_text(redact_text(text)) == redact_text(text)``.
* **Structure preserving** - header names, keys, URLs, quotes and surrounding
  punctuation stay in place; only the credential value becomes ``***``.  A
  preview is never emptied or mangled beyond the secret itself.
* **Narrow** - plain prose, paths and commands (``/home/user/project``,
  ``make test``, ``echo hello``) are left untouched; inline rules only fire on
  an explicit ``key=value`` pair, an auth header, a URL with userinfo, or a
  recognisable token shape.

This is defence in depth for humans, not a security boundary: a caller can
always encode a secret in a form these rules do not recognise.
"""

from __future__ import annotations

import re

#: What a credential value is replaced with.
MASK = "***"

#: Key/argument names whose values are secrets.  Kept identical to the
#: operator audit rule that predates M8 so both paths agree.
SECRET_KEY = re.compile(
    r"(?i)(key|token|secret|password|credential|authorization)")

#: Whole-value auth/cookie headers.  Matching the full line (not just the
#: first word) is what stops ``Authorization: Bearer abc`` from leaking
#: ``abc`` while only ``Bearer`` is hidden.
_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)"
    r"(\s*[:=]\s*)[^\r\n]+")

#: A bare ``Bearer <token>`` outside a header line.  Eight characters is the
#: shortest realistic opaque bearer token; shorter words are ordinary prose.
_BEARER = re.compile(r"(?i)(\bbearer)(\s+)[A-Za-z0-9._~+/=-]{8,}")

#: ``api_key=...`` / ``token: ...`` style pairs; the key name is preserved.
_SECRET_PAIR = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|secret|token|password|passwd|credential)\b"
    r"(\s*[:=]\s*)([\"']?)([^\s\"',;]{3,})")

#: Environment variable assignments whose *name* ends in a secret suffix
#: (``SFO_LLM_API_KEY=...``, ``MY_TOKEN=...``).  Requiring the suffix keeps
#: ordinary prose and paths out of the match.
_ENV_PAIR = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_]*_(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL))"
    r"(\s*=\s*)([\"']?)([^\s\"',;]{3,})",
    re.IGNORECASE)

#: ``scheme://user:pass@host`` -> ``scheme://***@host`` (credentials only).
_USERINFO = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")

#: JSON Web Tokens: three base64url segments, the first starting with ``eyJ``.
_JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")

#: GitHub tokens (classic and fine-grained).
_GITHUB = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")

#: npm access tokens.
_NPM = re.compile(r"\bnpm_[A-Za-z0-9]{20,}")

#: OpenAI-style API keys (``sk-...``; also covers ``sk-proj-...``).
_OPENAI = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}")


def is_secret_key(key: object) -> bool:
    """True when *key* names a secret and its value must be masked whole."""
    return bool(SECRET_KEY.search(str(key)))


def _mask_header(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{MASK}"


def _mask_bearer(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{MASK}"


def _mask_pair(match: re.Match[str]) -> str:
    name, separator, quote = match.group(1), match.group(2), match.group(3)
    return f"{name}{separator}{quote}{MASK}"


def redact_text(text: str) -> str:
    """Return *text* with inline credentials replaced by :data:`MASK`.

    Pure and idempotent; non-string input is returned unchanged.  The order
    follows docs/security-fix-plan.md §6.17.3: headers first (so the value of
    ``Authorization`` is never partially visible), then bearer tokens,
    key/value pairs, URL userinfo, and finally known token shapes.
    """
    if not isinstance(text, str) or not text:
        return text
    text = _AUTH_HEADER.sub(_mask_header, text)
    text = _BEARER.sub(_mask_bearer, text)
    text = _SECRET_PAIR.sub(_mask_pair, text)
    text = _ENV_PAIR.sub(_mask_pair, text)
    text = _USERINFO.sub(r"\1***@", text)
    text = _JWT.sub(MASK, text)
    text = _GITHUB.sub(MASK, text)
    text = _NPM.sub(MASK, text)
    text = _OPENAI.sub(MASK, text)
    return text
