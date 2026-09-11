"""Create a local API credential without printing it or overwriting an existing file."""

import os
import secrets
from pathlib import Path

target = Path(__file__).resolve().parents[1] / ".env"
try:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print("Existing .env preserved")
else:
    with os.fdopen(fd, "w") as stream:
        stream.write(f"HOMETAX_API_KEYS={secrets.token_urlsafe(32)}\nHOMETAX_SESSION_TTL=600\n")
    print("Created .env with permissions 0600")
