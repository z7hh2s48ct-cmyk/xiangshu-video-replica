from __future__ import annotations

import ipaddress

from app import first_frames, viral_media
from app.net_safety import FAKE_IP_NETWORK


def test_fake_ip_network_is_the_benchmarking_range() -> None:
    assert FAKE_IP_NETWORK == ipaddress.ip_network("198.18.0.0/15")


def test_both_lanes_share_one_constant_source() -> None:
    """C16/dedup: neither lane keeps a private copy of the range any more."""
    assert first_frames.FAKE_IP_NETWORK is FAKE_IP_NETWORK
    assert viral_media.FAKE_IP_NETWORK is FAKE_IP_NETWORK


def test_no_private_range_constant_remains() -> None:
    assert not hasattr(first_frames, "APILIO_PROXY_FAKE_IP_NETWORK")
    assert not hasattr(viral_media, "_FAKE_IP_NETWORK")


def test_pinned_connection_is_public_api() -> None:
    """C16: first_frames imports the public name, not a private one."""
    assert viral_media.pinned_connection is viral_media._pinned_connection
    assert first_frames._pinned_connection is viral_media.pinned_connection
