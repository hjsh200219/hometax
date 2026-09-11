"""Prompt locally: certificate password is never a command-line argument or saved file."""

import argparse
import base64
import getpass
import os
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--cert", type=Path, required=True)
parser.add_argument("--key", type=Path)
parser.add_argument("--type", choices=["der", "pfx"], default="der")
parser.add_argument("--validate-only", action="store_true")
parser.add_argument("--login-type", choices=["03", "04"])
args = parser.parse_args()
if not args.validate_only and not args.login_type:
    parser.error("--login-type is required for login (no mode guessing/retry)")
api_key = os.environ.get("HOMETAX_API_KEYS", "").split(",")[0].strip()
if not api_key:
    parser.error("Run with uv run --env-file .env")
payload = {
    "cert_type": args.type,
    "cert_file": base64.b64encode(args.cert.read_bytes()).decode(),
    "key_file": base64.b64encode(args.key.read_bytes()).decode() if args.key else None,
    "password": getpass.getpass("인증서 비밀번호: "),
}
path = "/v1/certificates/validate"
if not args.validate_only:
    path = "/v1/hometax/sessions"
    payload["login_type"] = args.login_type
with httpx.Client(base_url="http://127.0.0.1:8787", timeout=70, trust_env=False) as client:
    response = client.post(path, json=payload, headers={"Authorization": f"Bearer {api_key}"})
payload.clear()
print(response.status_code)
print(response.text)
