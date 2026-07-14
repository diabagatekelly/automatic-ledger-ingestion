"""Shared Meta Graph API constants for the WhatsApp integration.

Centralized so a version bump (e.g. v21.0 -> v22.0) is made in ONE place and
cannot be applied to only one of the call sites — inbound media download
(``src.media``) and outbound message send (``src.messaging``).
"""

from __future__ import annotations

# Graph API version pinned to match the rest of the Meta integration (see
# docs/STATUS.md "Meta setup"). Bump deliberately, not implicitly.
GRAPH_API_BASE = "https://graph.facebook.com/v21.0"
TIMEOUT_SECONDS = 30
