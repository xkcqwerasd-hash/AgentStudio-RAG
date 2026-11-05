import base64
import hashlib
import os
from pathlib import Path
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.logging import logger


_cached_fernet: Optional[Fernet] = None


def get_encryption_fernet() -> Fernet:
    """
    Get or initialize the Fernet cipher for model API key encryption.
    Master key source priority:
    1. settings.MODEL_ENCRYPTION_KEY (if explicitly set in environment)
    2. Persistent local keyfile in settings.STORAGE_DIR / '.model_master_key'
    
    Plaintext storage is strictly prohibited. An encryption key is guaranteed to exist.
    """
    global _cached_fernet
    if _cached_fernet is not None:
        return _cached_fernet

    # 1. Check environment variable
    env_key = getattr(settings, "MODEL_ENCRYPTION_KEY", None)
    if env_key and env_key.strip():
        # Ensure 32 url-safe base64-encoded bytes
        raw_env = env_key.strip().encode("utf-8")
        try:
            # Check if it's already a valid Fernet key (32 base64 url-safe bytes)
            if len(raw_env) == 44:
                decoded = base64.urlsafe_b64decode(raw_env)
                if len(decoded) == 32:
                    _cached_fernet = Fernet(raw_env)
                    return _cached_fernet
        except Exception:
            pass
        # Derive 32-byte key using SHA-256
        derived_key = base64.urlsafe_b64encode(hashlib.sha256(raw_env).digest())
        _cached_fernet = Fernet(derived_key)
        return _cached_fernet

    # 2. Check or create persistent storage keyfile
    storage_dir = Path(settings.STORAGE_DIR)
    storage_dir.mkdir(parents=True, exist_ok=True)
    key_file = storage_dir / ".model_master_key"

    if key_file.exists():
        try:
            with open(key_file, "rb") as f:
                saved_key = f.read().strip()
            if len(saved_key) == 44:
                _cached_fernet = Fernet(saved_key)
                return _cached_fernet
        except Exception as exc:
            logger.warning(f"Failed to read existing master key file: {exc}. Recreating.")

    # Generate new cryptographically secure Fernet key
    new_key = Fernet.generate_key()
    try:
        with open(key_file, "wb") as f:
            f.write(new_key)
        # Attempt to restrict file permissions on POSIX systems
        try:
            os.chmod(key_file, 0o600)
        except Exception:
            pass
    except Exception as exc:
        logger.error(f"Failed to persist master key to {key_file}: {exc}")
        raise RuntimeError(
            "Cannot initialize secure API key encryption. "
            "Please ensure data/storage is writable or configure MODEL_ENCRYPTION_KEY."
        ) from exc

    _cached_fernet = Fernet(new_key)
    return _cached_fernet


def encrypt_api_key(plain_key: Optional[str]) -> Optional[str]:
    """
    Encrypt an API key using Fernet.
    Returns ciphertext as a utf-8 string, or None if input is empty.
    """
    if not plain_key:
        return None
    plain_str = plain_key.strip()
    if not plain_str:
        return None
    fernet = get_encryption_fernet()
    encrypted_bytes = fernet.encrypt(plain_str.encode("utf-8"))
    return encrypted_bytes.decode("utf-8")


def decrypt_api_key(ciphertext: Optional[str]) -> Optional[str]:
    """
    Decrypt an encrypted API key ciphertext.
    Returns the plaintext API key.
    Raises ValueError if ciphertext is corrupted or decryption fails.
    """
    if not ciphertext:
        return None
    cipher_str = ciphertext.strip()
    if not cipher_str:
        return None
    fernet = get_encryption_fernet()
    try:
        decrypted_bytes = fernet.decrypt(cipher_str.encode("utf-8"))
        return decrypted_bytes.decode("utf-8")
    except InvalidToken as exc:
        logger.error("Failed to decrypt API key: Invalid token or mismatched master key.")
        raise ValueError("Decryption failed: API Key cannot be decrypted with the current master key.") from exc
    except Exception as exc:
        logger.error(f"Failed to decrypt API key: {exc}")
        raise ValueError(f"Decryption failed: {str(exc)}") from exc


def mask_api_key(api_key: Optional[str]) -> str:
    """
    Mask an API key for safe display in UI or logs.
    Example: 'sk-abcdef1234567890' -> 'sk-****7890'
    Never returns the plain API key.
    """
    if not api_key:
        return ""
    key = api_key.strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "********"
    prefix_len = 3
    if key.startswith("sk-") and len(key) > 10:
        prefix_len = 5
    return f"{key[:prefix_len]}****{key[-4:]}"
