from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from asn1crypto import core, keys
from cryptography import x509
from cryptography.exceptions import InvalidKey, InvalidSignature
from cryptography.hazmat.decrepit.ciphers.algorithms import SEED
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.padding import PKCS7
from cryptography.hazmat.primitives.serialization import pkcs12

OID_SEED_CBC_WITH_SHA1 = "1.2.410.200004.1.15"
OID_PBES2 = "1.2.840.113549.1.5.13"
OID_PBKDF2 = "1.2.840.113549.1.5.12"
OID_SEED_CBC = "1.2.410.200004.1.4"

OID_HMAC_SHA1 = "1.2.840.113549.2.7"
OID_HMAC_SHA224 = "1.2.840.113549.2.8"
OID_HMAC_SHA256 = "1.2.840.113549.2.9"
OID_HMAC_SHA384 = "1.2.840.113549.2.10"
OID_HMAC_SHA512 = "1.2.840.113549.2.11"
OID_NPKI_RANDOM_NUMBER = "1.2.410.200004.10.1.1.3"

MIN_PBKDF_ITERATIONS = 1
MAX_PBKDF_ITERATIONS = 1_000_000


class CertificateError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(repr=False)
class CertificateMaterial:
    certificate: x509.Certificate
    private_key: rsa.RSAPrivateKey
    private_key_der: bytes
    random_number: bytes | None

    def sign(self, data: bytes) -> bytes:
        if not isinstance(data, bytes):
            raise CertificateError("invalid_data", "data must be bytes")
        return self.private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())

    def metadata(self) -> dict[str, str]:
        return {
            "serial": str(self.certificate.serial_number),
            "subject": self.certificate.subject.rfc4514_string(),
            "valid_from": _as_utc(self.certificate.not_valid_before_utc).isoformat(),
            "valid_until": _as_utc(self.certificate.not_valid_after_utc).isoformat(),
        }


class _AlgorithmIdentifier(core.Sequence):
    _fields = [
        ("algorithm", core.ObjectIdentifier),
        ("parameters", core.Any, {"optional": True}),
    ]


class _EncryptedPrivateKeyInfo(core.Sequence):
    _fields = [
        ("encryption_algorithm", _AlgorithmIdentifier),
        ("encrypted_data", core.OctetString),
    ]


def load_certificate(
    cert_bytes: bytes,
    key_bytes: bytes | None,
    password: str,
    cert_type: str,
) -> CertificateMaterial:
    _require_bytes("cert_bytes", cert_bytes)
    if not isinstance(password, str):
        raise CertificateError("invalid_password", "password must be a string")

    normalized_type = (cert_type or "").strip().lower()
    if normalized_type in {"npki", "der", "der_key"}:
        _require_bytes("key_bytes", key_bytes)
        return _load_npki(cert_bytes, key_bytes, password)
    if normalized_type in {"pfx", "p12", "pkcs12", "pkcs#12"}:
        return _load_pfx(cert_bytes, password)
    raise CertificateError("unsupported_cert_type", "unsupported certificate type")


def _load_npki(cert_bytes: bytes, key_bytes: bytes | None, password: str) -> CertificateMaterial:
    try:
        password_bytes = password.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CertificateError("invalid_password_encoding", "NPKI passwords must be ASCII") from exc

    certificate = _load_der_certificate(cert_bytes)
    private_key_der = _decrypt_npki_private_key(key_bytes or b"", password_bytes)
    private_key = _load_rsa_private_key(private_key_der, None)
    random_number = _extract_npki_random_number(private_key_der)
    _validate_material(certificate, private_key)
    return CertificateMaterial(certificate, private_key, private_key_der, random_number)


def _load_pfx(pfx_bytes: bytes, password: str) -> CertificateMaterial:
    try:
        loaded_key, certificate, _chain = pkcs12.load_key_and_certificates(
            pfx_bytes,
            password.encode("utf-8") if password is not None else None,
        )
    except (ValueError, InvalidKey) as exc:
        raise CertificateError("invalid_pfx", "PFX could not be loaded") from exc

    if certificate is None or loaded_key is None:
        raise CertificateError("invalid_pfx", "PFX must contain a certificate and private key")
    if not isinstance(loaded_key, rsa.RSAPrivateKey):
        raise CertificateError("unsupported_key", "only RSA private keys are supported")

    private_key_der = loaded_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _validate_material(certificate, loaded_key)
    return CertificateMaterial(certificate, loaded_key, private_key_der, None)


def _load_der_certificate(cert_bytes: bytes) -> x509.Certificate:
    try:
        return x509.load_der_x509_certificate(cert_bytes)
    except ValueError as exc:
        raise CertificateError(
            "invalid_certificate", "certificate DER could not be parsed"
        ) from exc


def _load_rsa_private_key(private_key_der: bytes, password: bytes | None) -> rsa.RSAPrivateKey:
    try:
        loaded_key = serialization.load_der_private_key(private_key_der, password=password)
    except (ValueError, TypeError) as exc:
        raise CertificateError("invalid_private_key", "private key could not be parsed") from exc

    if not isinstance(loaded_key, rsa.RSAPrivateKey):
        raise CertificateError("unsupported_key", "only RSA private keys are supported")
    return loaded_key


def _decrypt_npki_private_key(encrypted_key: bytes, password: bytes) -> bytes:
    try:
        info = _EncryptedPrivateKeyInfo.load(encrypted_key)
        alg_info = info["encryption_algorithm"]
        alg_oid = alg_info["algorithm"].dotted
        encrypted_data = info["encrypted_data"].native
    except (ValueError, TypeError) as exc:
        raise CertificateError(
            "invalid_private_key", "encrypted private key could not be parsed"
        ) from exc

    if alg_oid == OID_SEED_CBC_WITH_SHA1:
        key, iv = _seed_cbc_with_sha1_key_iv(alg_info["parameters"].parsed, password)
    elif alg_oid == OID_PBES2:
        key, iv = _pbes2_seed_key_iv(alg_info["parameters"].parsed, password)
    else:
        raise CertificateError("unsupported_key_encryption", "unsupported private key encryption")

    try:
        return _seed_cbc_decrypt(encrypted_data, key, iv)
    except ValueError as exc:
        raise CertificateError("decrypt_failed", "private key decryption failed") from exc


def _seed_cbc_with_sha1_key_iv(parameters: core.Asn1Value, password: bytes) -> tuple[bytes, bytes]:
    try:
        sequence = core.Sequence.load(parameters.dump())
        salt = sequence[0].native
        iterations = int(sequence[1].native)
    except (ValueError, TypeError, IndexError) as exc:
        raise CertificateError("invalid_private_key", "invalid seedCBCWithSHA1 parameters") from exc

    _validate_pbkdf_iterations(iterations)
    dk = _pbkdf1_sha1(password, salt, iterations, 20)
    return dk[:16], hashlib.sha1(dk[16:20]).digest()[:16]


def _pbes2_seed_key_iv(parameters: core.Asn1Value, password: bytes) -> tuple[bytes, bytes]:
    try:
        pbes2 = core.Sequence.load(parameters.dump())
        kdf = core.Sequence.load(pbes2[0].dump())
        enc = core.Sequence.load(pbes2[1].dump())
        kdf_oid = kdf[0].dotted
        enc_oid = enc[0].dotted
    except (ValueError, TypeError, IndexError) as exc:
        raise CertificateError("invalid_private_key", "invalid PBES2 parameters") from exc

    if kdf_oid != OID_PBKDF2:
        raise CertificateError("unsupported_key_encryption", "unsupported PBES2 KDF")
    if enc_oid != OID_SEED_CBC:
        raise CertificateError("unsupported_key_encryption", "unsupported PBES2 cipher")

    try:
        pbkdf2_params = core.Sequence.load(kdf[1].dump())
        salt = pbkdf2_params[0].native
        iterations = int(pbkdf2_params[1].native)
        key_length = (
            int(pbkdf2_params[2].native)
            if len(pbkdf2_params) >= 3 and isinstance(pbkdf2_params[2], core.Integer)
            else None
        )
        prf_oid = OID_HMAC_SHA1
        if len(pbkdf2_params) >= 3:
            prf_index = 3 if key_length is not None else 2
            if len(pbkdf2_params) > prf_index:
                prf = core.Sequence.load(pbkdf2_params[prf_index].dump())
                prf_oid = prf[0].dotted
        iv = enc[1].native
    except (ValueError, TypeError, IndexError) as exc:
        raise CertificateError("invalid_private_key", "invalid PBKDF2 parameters") from exc

    _validate_pbkdf_iterations(iterations)
    derive_length = key_length or 20
    if derive_length < 16 or derive_length > 128:
        raise CertificateError("invalid_private_key", "invalid PBKDF2 key length")
    if not isinstance(iv, bytes) or len(iv) != 16:
        raise CertificateError("invalid_private_key", "invalid SEED-CBC IV")

    return _pbkdf2(password, salt, iterations, derive_length, prf_oid)[:16], iv


def _pbkdf1_sha1(password: bytes, salt: bytes, iterations: int, length: int) -> bytes:
    if length > hashlib.sha1().digest_size:
        raise CertificateError("invalid_private_key", "PBKDF1 output length is too large")
    digest = hashlib.sha1(password + salt).digest()
    for _ in range(1, iterations):
        digest = hashlib.sha1(digest).digest()
    return digest[:length]


def _pbkdf2(password: bytes, salt: bytes, iterations: int, length: int, prf_oid: str) -> bytes:
    hash_name = {
        OID_HMAC_SHA1: "sha1",
        OID_HMAC_SHA224: "sha224",
        OID_HMAC_SHA256: "sha256",
        OID_HMAC_SHA384: "sha384",
        OID_HMAC_SHA512: "sha512",
    }.get(prf_oid)
    if hash_name is None:
        raise CertificateError("unsupported_key_encryption", "unsupported PBKDF2 PRF")
    return hashlib.pbkdf2_hmac(hash_name, password, salt, iterations, length)


def _seed_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    decryptor = Cipher(SEED(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = PKCS7(SEED.block_size).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _extract_npki_random_number(private_key_der: bytes) -> bytes | None:
    try:
        info = keys.PrivateKeyInfo.load(private_key_der)
        attributes = info["attributes"]
    except (ValueError, TypeError) as exc:
        raise CertificateError(
            "invalid_private_key", "private key attributes could not be parsed"
        ) from exc

    if attributes.native is None:
        return None

    for attribute in attributes:
        if attribute["type"].dotted != OID_NPKI_RANDOM_NUMBER:
            continue
        values = attribute["values"]
        if len(values) == 0:
            return None
        return _attribute_value_bytes(values[0])
    return None


def _attribute_value_bytes(value: core.Asn1Value) -> bytes:
    parsed = value.parsed if isinstance(value, core.Any) else value
    if isinstance(parsed, core.OctetString):
        return parsed.native
    if isinstance(parsed, core.BitString):
        contents = parsed.contents
        return contents[1:] if contents else b""
    raise CertificateError("invalid_private_key", "NPKI random number attribute is not bytes")


def _validate_material(certificate: x509.Certificate, private_key: rsa.RSAPrivateKey) -> None:
    now = datetime.now(UTC)
    if _as_utc(certificate.not_valid_before_utc) > now:
        raise CertificateError("certificate_not_yet_valid", "certificate is not yet valid")
    if _as_utc(certificate.not_valid_after_utc) < now:
        raise CertificateError("certificate_expired", "certificate is expired")

    public_key = certificate.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise CertificateError("unsupported_key", "only RSA certificates are supported")

    data = b"hometax-login-api certificate material check"
    signature = private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    try:
        public_key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise CertificateError("key_mismatch", "certificate and private key do not match") from exc


def _validate_pbkdf_iterations(iterations: int) -> None:
    if iterations < MIN_PBKDF_ITERATIONS or iterations > MAX_PBKDF_ITERATIONS:
        raise CertificateError("invalid_private_key", "PBKDF iterations are out of range")


def _require_bytes(name: str, value: Any) -> None:
    if not isinstance(value, bytes) or len(value) == 0:
        raise CertificateError("empty_input", f"{name} must be non-empty bytes")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
