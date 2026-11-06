"""Owner authentication; credentials and sessions never leave server storage."""
import hashlib
import hmac
import secrets
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def password_hash(password, salt=None):
    if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
        raise ValueError('암호는 12~1024자로 입력하세요.')
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + digest


def verify_password(password, encoded):
    try:
        salt, _ = encoded.split(':', 1)
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_token():
    return secrets.token_urlsafe(32)
