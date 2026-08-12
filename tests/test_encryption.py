from cryptography.fernet import Fernet

from agent_toolkit import encryption


def test_enc_dec_roundtrip(monkeypatch):
    monkeypatch.setattr(encryption, "_fernet", None)
    monkeypatch.setattr(encryption, "_warned", False)
    monkeypatch.setenv("DB_ENCRYPTION_KEY", Fernet.generate_key().decode())
    ct = encryption.enc("secret")
    assert ct != "secret"
    assert encryption.dec(ct) == "secret"


def test_enc_without_key_returns_plaintext(monkeypatch):
    monkeypatch.setattr(encryption, "_fernet", None)
    monkeypatch.setattr(encryption, "_warned", False)
    monkeypatch.delenv("DB_ENCRYPTION_KEY", raising=False)
    assert encryption.enc("secret") == "secret"


def test_dec_handles_none():
    assert encryption.dec(None) is None


def test_dec_falls_back_to_plaintext_for_unencrypted_rows(monkeypatch):
    monkeypatch.setattr(encryption, "_fernet", None)
    monkeypatch.setattr(encryption, "_warned", False)
    monkeypatch.setenv("DB_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert encryption.dec("plain-legacy-value") == "plain-legacy-value"
