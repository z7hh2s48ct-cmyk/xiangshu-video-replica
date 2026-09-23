"""Process-start identity for worker leases and log correlation."""

from __future__ import annotations

import os
import re
import socket
import uuid


def new_worker_instance_id(worker_name: str, configured_id: str | None = None) -> str:
    """Call once at CLI startup; an explicit ID is the operator's logical label.

    Host/PID alone collide across containers and process restarts. The random
    suffix distinguishes starts even when both are identical. Restrict labels
    because the resulting identifier is also embedded in the logging format.
    This is an observation/owner identifier, never a replacement lease token.
    """
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", (configured_id or worker_name).strip())[:48]
    host = re.sub(r"[^A-Za-z0-9_.-]", "_", socket.gethostname())[:48]
    return f"{label or 'worker'}:{host or 'host'}:{os.getpid()}:{uuid.uuid4().hex}"
