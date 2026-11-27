from __future__ import annotations

import base64
import json
import time
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def parse_service_account(value: str) -> dict[str, str]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Service Account credential is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Service Account credential must be a JSON object")
    required = ("project_id", "client_email", "private_key", "token_uri")
    missing = [name for name in required if not str(payload.get(name) or "").strip()]
    if missing:
        raise ValueError(f"Service Account credential is missing: {', '.join(missing)}")
    return {str(key): str(item) for key, item in payload.items() if item is not None}


def service_account_access_token(value: str, timeout: int = 45) -> tuple[str, dict[str, str]]:
    credential = parse_service_account(value)
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": credential.get("private_key_id", "")}
    claims = {
        "iss": credential["client_email"],
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "aud": credential["token_uri"],
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    ).encode("ascii")
    try:
        private_key = serialization.load_pem_private_key(
            credential["private_key"].encode("utf-8"), password=None
        )
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, TypeError) as exc:
        raise ValueError("Service Account private_key cannot be parsed") from exc
    assertion = f"{signing_input.decode('ascii')}.{_b64url(signature)}"
    request = Request(
        credential["token_uri"],
        data=urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }
        ).encode("ascii"),
        headers={"content-type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    token = str(payload.get("access_token") or "")
    if not token:
        raise RuntimeError("Google OAuth did not return access_token")
    return token, credential


def vertex_openai_base_url(api_root: str, project_id: str) -> str:
    root = api_root.strip().rstrip("/")
    if "/projects/" in root and root.endswith("/endpoints/openapi"):
        return root
    parsed = urlparse(root)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Vertex API URL must be a valid http(s) address")
    hostname = parsed.hostname or ""
    marker = "-aiplatform.googleapis.com"
    location = hostname[: -len(marker)] if hostname.endswith(marker) else "global"
    location = location or "global"
    return (
        f"{root}/v1beta1/projects/{quote(project_id, safe='')}/locations/"
        f"{quote(location, safe='')}/endpoints/openapi"
    )


def vertex_model_name(value: str) -> str:
    value = value.strip()
    return value if "/" in value else f"google/{value}"
