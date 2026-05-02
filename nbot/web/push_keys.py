import base64
import json
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _public_key_to_webpush_base64(public_key) -> str:
    numbers = public_key.public_numbers()
    x = numbers.x.to_bytes(32, "big")
    y = numbers.y.to_bytes(32, "big")
    return _b64url(b"\x04" + x + y)


def ensure_vapid_keys(data_dir: str) -> dict:
    push_dir = os.path.join(data_dir, "push")
    os.makedirs(push_dir, exist_ok=True)

    meta_path = os.path.join(push_dir, "vapid.json")
    private_key_path = os.path.join(push_dir, "vapid_private.pem")

    if os.path.exists(meta_path) and os.path.exists(private_key_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        return {
            "public_key": meta["public_key"],
            "private_key_path": private_key_path,
            "subject": meta.get("subject", "mailto:admin@localhost"),
        }

    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    with open(private_key_path, "wb") as f:
        f.write(pem)

    meta = {
        "public_key": _public_key_to_webpush_base64(private_key.public_key()),
        "subject": os.getenv("VAPID_SUBJECT", "mailto:admin@localhost"),
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return {
        "public_key": meta["public_key"],
        "private_key_path": private_key_path,
        "subject": meta["subject"],
    }
