"""Encryption for secrets stored in the database (mailbox passwords). The key is MAILBOX_ENCRYPTION_KEY in .env, a
Fernet key; every machine running the app needs the same one, or it cannot read the passwords. Generate one with:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import os

from cryptography.fernet import Fernet, InvalidToken

ENV_NAME = "MAILBOX_ENCRYPTION_KEY"


class SecretsError(RuntimeError):
    """The key is missing or cannot read a stored secret; the message says what to do."""


def _fernet() -> Fernet:
    key = os.environ.get(ENV_NAME, "").strip()
    if not key:
        raise SecretsError(f"{ENV_NAME} is not set in .env (needed to store and read mailbox passwords).")
    try:
        return Fernet(key.encode())
    except ValueError as error:
        raise SecretsError(f"{ENV_NAME} in .env is not a valid key.") from error


def configured() -> bool:
    try:
        _fernet()
        return True
    except SecretsError:
        return False


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as error:
        raise SecretsError(f"Cannot read a stored password with this {ENV_NAME}: it is not the key it was saved with "
                           "(use the same key on every machine).") from error
