"""Shared network-safety primitives for the outbound media / image lanes.

Findings C16 (a cross-module *private* import) and the duplicated synthetic
proxy range both pointed at the same gap: the outbound download lanes had no
shared home for the one fact they genuinely agree on. This module is the single
source for that range.

The two resolvers — ``first_frames.require_safe_provider_download_url`` and
``viral_media._resolve_public_http_url`` — are deliberately **not** merged here.
They enforce materially different security policies (HTTPS-only vs http+https,
port allow-lists, IDNA encoding, distinct exception types, and different
fake-IP treatment: an allow-listed-host pass-through vs an authenticated DoH
re-resolution). Unifying them would risk an SSRF regression in one lane just to
remove duplication, so each lane keeps its own policy and only the shared
constant is centralised.
"""

from __future__ import annotations

import ipaddress

# RFC 2544 benchmarking range. The configured upstream proxy answers DNS with a
# synthetic address inside this block; each lane decides — by its own policy —
# whether such an answer is acceptable for the host it is about to fetch.
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
