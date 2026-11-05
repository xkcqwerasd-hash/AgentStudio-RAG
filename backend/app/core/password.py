import hashlib
import hmac
import secrets
from typing import Optional

from app.core.config import settings

ALGORITHM = "pbkdf2_sha256"


def hash_password(password: str, iterations: Optional[int] = None) -> str:
    """
    Securely hash a password using PBKDF2-HMAC-SHA256 according to OWASP guidelines.
    Generates a cryptographically strong 16-byte random salt.
    Format: pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
    """
    if not password or not isinstance(password, str):
        raise ValueError("Password must be a non-empty string.")

    eff_iterations = iterations or getattr(settings, "PASSWORD_HASH_ITERATIONS", 600_000)
    salt = secrets.token_hex(16)  # 16 bytes = 32 hex chars

    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        eff_iterations,
    )
    hash_hex = key.hex()
    return f"{ALGORITHM}${eff_iterations}${salt}${hash_hex}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plaintext password against a stored PBKDF2-HMAC-SHA256 hash.
    Uses constant-time comparison to prevent timing attacks.
    """
    if not plain_password or not hashed_password:
        return False

    try:
        parts = hashed_password.split("$")
        if len(parts) != 4:
            return False

        algo, iterations_str, salt, stored_hash = parts
        if algo != ALGORITHM:
            return False

        iterations = int(iterations_str)
        if iterations <= 0:
            return False

        computed_key = hashlib.pbkdf2_hmac(
            "sha256",
            plain_password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations,
        )
        return hmac.compare_digest(computed_key.hex(), stored_hash)
    except Exception:
        return False


def dummy_verify_password() -> None:
    """
    Execute a dummy PBKDF2 operation with low iterations (or dummy check)
    to mitigate account enumeration timing attacks when username does not exist.
    """
    dummy_salt = "00000000000000000000000000000000"
    # Run a quick PBKDF2 calculation to prevent instant return
    hashlib.pbkdf2_hmac(
        "sha256",
        b"dummy_password_for_timing_mitigation",
        dummy_salt.encode("utf-8"),
        1000,
    )


def needs_rehash(hashed_password: str, target_iterations: Optional[int] = None) -> bool:
    """
    Check if a stored password hash was generated with fewer iterations than current target,
    allowing seamless hash upgrades during successful logins.
    """
    target = target_iterations or getattr(settings, "PASSWORD_HASH_ITERATIONS", 600_000)
    try:
        parts = hashed_password.split("$")
        if len(parts) != 4:
            return True
        algo, iterations_str, _, _ = parts
        if algo != ALGORITHM:
            return True
        return int(iterations_str) < target
    except Exception:
        return True
