"""
Thin bcrypt compatibility shim.
Uses real bcrypt if available (production), falls back to werkzeug's
generate_password_hash/check_password_hash (PBKDF2-SHA256) for local dev.
The API surface is identical so auth.py needs no changes.
"""
try:
    import bcrypt as _bcrypt  # noqa: F401
    from bcrypt import hashpw, checkpw, gensalt  # noqa: F401
    USING_REAL_BCRYPT = True
except ImportError:
    import hashlib, os, hmac

    USING_REAL_BCRYPT = False

    def gensalt(rounds=12):
        return os.urandom(16)

    def hashpw(password: bytes, salt: bytes) -> bytes:
        dk = hashlib.pbkdf2_hmac('sha256', password, salt, 260000)
        return b'pbkdf2$' + salt.hex().encode() + b'$' + dk.hex().encode()

    def checkpw(password: bytes, hashed: bytes) -> bool:
        try:
            _, salt_hex, dk_hex = hashed.split(b'$')
            salt = bytes.fromhex(salt_hex.decode())
            dk = hashlib.pbkdf2_hmac('sha256', password, salt, 260000)
            return hmac.compare_digest(dk.hex().encode(), dk_hex)
        except Exception:
            return False
