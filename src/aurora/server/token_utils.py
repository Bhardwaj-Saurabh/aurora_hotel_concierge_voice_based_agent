"""
token_utils.py  -  least-privilege LiveKit token minting (goal.md ADR-015).

talk_server.py and create_token.py both minted tokens with identical grants
and no explicit TTL, defaulting to the SDK's implicit 6-hour expiry. Shared
here so the two never drift apart on what is fundamentally security config.

Every participant this project mints a token for is a caller or the demo
Aurora identity in a two-party room — neither needs anything beyond joining
the room, publishing audio, and subscribing to the other's audio. Every
other capability (room admin/create/list/record, data channels, metadata
updates, ingress admin, agent-session management) is explicitly denied
rather than left to the SDK's own defaults, so a future grant expansion is a
deliberate code change, not an accidental side effect of an SDK upgrade.
"""

from __future__ import annotations

import os
from datetime import timedelta

from livekit import api

_DEFAULT_TTL_MINUTES = 60


def build_video_grants(room: str) -> "api.VideoGrants":
    """Least-privilege grants: join, publish, subscribe — nothing else."""
    return api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=False,
        can_update_own_metadata=False,
        room_create=False,
        room_list=False,
        room_record=False,
        room_admin=False,
        ingress_admin=False,
        recorder=False,
        hidden=False,
        can_manage_agent_session=False,
    )


def token_ttl() -> timedelta:
    """How long a minted token remains valid. A single call session doesn't
    need anywhere near the SDK's implicit 6-hour default."""
    raw = os.getenv("LIVEKIT_TOKEN_TTL_MINUTES", "").strip()
    minutes = int(raw) if raw else _DEFAULT_TTL_MINUTES
    return timedelta(minutes=minutes)


def mint_token(
    *,
    api_key: str,
    api_secret: str,
    identity: str,
    name: str,
    room: str,
    ttl_minutes: int | None = None,
) -> str:
    """Build a JWT for one room participant with least-privilege grants."""
    ttl = timedelta(minutes=ttl_minutes) if ttl_minutes is not None else token_ttl()
    return (
        api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name(name)
        .with_grants(build_video_grants(room))
        .with_ttl(ttl)
        .to_jwt()
    )
