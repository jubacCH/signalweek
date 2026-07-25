"""URL canonicalization for signal deduplication.

Two links pointing at the same article should reduce to the same string so the
ingest pipeline can spot duplicates whether they arrive from the same feed
twice, a redirect, or a variant with tracking parameters.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Tracking-only query parameters that never change the destination content.
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "utm_reader",
        "utm_referrer",
        "utm_social",
        "utm_social-type",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "yclid",
        "_hsenc",
        "_hsmi",
        "ref",
        "ref_src",
        "ref_url",
        "s",
    }
)

_DEFAULT_PORTS: dict[str, str] = {"http": "80", "https": "443"}


def canonicalize_url(url: str) -> str:
    """Return a normalized form of ``url`` suitable for dedup comparisons.

    The transformations are conservative — they only strip parts that are
    universally understood not to change the resource identity:

    * scheme and host are lowercased
    * default ports are dropped
    * the fragment is removed
    * common tracking query parameters are removed
    * remaining query parameters are sorted (stable order)
    * an empty path becomes ``/``
    * a trailing slash is stripped from non-root paths
    """
    if not url:
        return ""

    stripped = url.strip()
    parts = urlsplit(stripped)

    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()

    netloc = hostname
    if parts.port is not None and _DEFAULT_PORTS.get(scheme) != str(parts.port):
        netloc = f"{hostname}:{parts.port}"
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo = f"{userinfo}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    kept_query.sort()
    query = urlencode(kept_query, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))
