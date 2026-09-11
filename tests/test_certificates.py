from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from asn1crypto import core, keys
from cryptography import x509
from cryptography.hazmat.decrepit.ciphers.algorithms import SEED
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.padding import PKCS7
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from hometax_login.certificates import CertificateError, load_certificate

OID_SEED_CBC_WITH_SHA1 = "1.2.410.200004.1.15"
OID_PBES2 = "1.2.840.113549.1.5.13"
OID_PBKDF2 = "1.2.840.113549.1.5.12"
OID_SEED_CBC = "1.2.410.200004.1.4"
OID_HMAC_SHA256 = "1.2.840.113549.2.9"
OID_NPKI_RANDOM_NUMBER = "1.2.410.200004.10.1.1.3"
RANDOM_NUMBER = b"\x00\x00vid-random"


class AlgorithmIdentifier(core.Sequence):
    _fields = [
        ("algorithm", core.ObjectIdentifier),
        ("parameters", core.Any, {"optional": True}),
    ]


class EncryptedPrivateKeyInfo(core.Sequence):
    _fields = [
        ("encryption_algorithm", AlgorithmIdentifier),
        ("encrypted_data", core.OctetString),
    ]


def test_loads_seed_cbc_with_sha1_npki_and_signs() -> None:
    key, cert = _rsa_material()
    password = "safe-ascii-password"
    private_key_der = _pkcs8_der_with_random_number(key, RANDOM_NUMBER)

    material = load_certificate(
        cert.public_bytes(serialization.Encoding.DER),
        _encrypt_seed_cbc_with_sha1(private_key_der, password, iterations=19),
        password,
        "npki",
    )

    assert material.private_key_der == private_key_der
    assert material.random_number == RANDOM_NUMBER
    _verify_signature(material)
    metadata = material.metadata()
    assert metadata["serial"] == str(cert.serial_number)
    assert "CN=synthetic.test" in metadata["subject"]
    assert metadata["valid_from"].endswith("+00:00")


def test_loads_pbes2_seed_npki_with_prf_and_key_length() -> None:
    key, cert = _rsa_material()
    password = "pbkdf2-password"
    private_key_der = _pkcs8_der_with_random_number(key, RANDOM_NUMBER)

    material = load_certificate(
        cert.public_bytes(serialization.Encoding.DER),
        _encrypt_pbes2_seed(
            private_key_der,
            password,
            iterations=23,
            key_length=32,
            prf_oid=OID_HMAC_SHA256,
        ),
        password,
        "der",
    )

    assert material.private_key.public_key().public_numbers() == key.public_key().public_numbers()
    assert material.random_number == RANDOM_NUMBER
    _verify_signature(material)


def test_loads_utf8_password_pfx() -> None:
    key, cert = _rsa_material()
    pfx = pkcs12.serialize_key_and_certificates(
        b"friendly",
        key,
        cert,
        None,
        serialization.BestAvailableEncryption("비밀번호".encode()),
    )

    material = load_certificate(pfx, None, "비밀번호", "pfx")

    assert material.private_key.public_key().public_numbers() == key.public_key().public_numbers()
    assert material.random_number is None
    _verify_signature(material)


def test_rejects_non_ascii_npki_password() -> None:
    key, cert = _rsa_material()

    with pytest.raises(CertificateError) as exc:
        load_certificate(
            cert.public_bytes(serialization.Encoding.DER),
            _encrypt_seed_cbc_with_sha1(_pkcs8_der(key), "ascii", iterations=2),
            "한글",
            "npki",
        )

    assert exc.value.code == "invalid_password_encoding"


def test_rejects_expired_certificate() -> None:
    key, cert = _rsa_material(not_before_days=-20, not_after_days=-1)

    with pytest.raises(CertificateError) as exc:
        load_certificate(
            cert.public_bytes(serialization.Encoding.DER),
            _encrypt_seed_cbc_with_sha1(_pkcs8_der(key), "pw", iterations=2),
            "pw",
            "npki",
        )

    assert exc.value.code == "certificate_expired"


def test_rejects_not_yet_valid_certificate() -> None:
    key, cert = _rsa_material(not_before_days=1, not_after_days=20)

    with pytest.raises(CertificateError) as exc:
        load_certificate(
            cert.public_bytes(serialization.Encoding.DER),
            _encrypt_seed_cbc_with_sha1(_pkcs8_der(key), "pw", iterations=2),
            "pw",
            "npki",
        )

    assert exc.value.code == "certificate_not_yet_valid"


def test_rejects_mismatched_key() -> None:
    cert_key, cert = _rsa_material()
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with pytest.raises(CertificateError) as exc:
        load_certificate(
            cert.public_bytes(serialization.Encoding.DER),
            _encrypt_seed_cbc_with_sha1(_pkcs8_der(other_key), "pw", iterations=2),
            "pw",
            "npki",
        )

    assert cert_key.public_key().public_numbers() != other_key.public_key().public_numbers()
    assert exc.value.code == "key_mismatch"


def test_rejects_empty_invalid_unsupported_and_iteration_bounds() -> None:
    key, cert = _rsa_material()
    cert_der = cert.public_bytes(serialization.Encoding.DER)

    with pytest.raises(CertificateError) as exc:
        load_certificate(b"", b"x", "pw", "npki")
    assert exc.value.code == "empty_input"

    with pytest.raises(CertificateError) as exc:
        load_certificate(b"not-der", b"x", "pw", "npki")
    assert exc.value.code == "invalid_certificate"

    with pytest.raises(CertificateError) as exc:
        load_certificate(
            cert_der,
            _encrypt_seed_cbc_with_sha1(_pkcs8_der(key), "pw", iterations=2),
            "pw",
            "x509",
        )
    assert exc.value.code == "unsupported_cert_type"

    with pytest.raises(CertificateError) as exc:
        load_certificate(
            cert_der,
            _encrypt_seed_cbc_with_sha1(_pkcs8_der(key), "pw", iterations=1_000_001),
            "pw",
            "npki",
        )
    assert exc.value.code == "invalid_private_key"


def _rsa_material(
    not_before_days: int = -1,
    not_after_days: int = 30,
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Synthetic Test"),
            x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.test"),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=not_before_days))
        .not_valid_after(now + timedelta(days=not_after_days))
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _pkcs8_der(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _pkcs8_der_with_random_number(key: rsa.RSAPrivateKey, random_number: bytes) -> bytes:
    info = keys.PrivateKeyInfo.load(_pkcs8_der(key))
    info["attributes"] = keys.Attributes(
        [
            keys.Attribute(
                {
                    "type": OID_NPKI_RANDOM_NUMBER,
                    "values": [core.OctetString(random_number)],
                }
            )
        ]
    )
    return info.dump()


def _encrypt_seed_cbc_with_sha1(plain: bytes, password: str, iterations: int) -> bytes:
    salt = b"12345678"
    dk = _pbkdf1_sha1(password.encode("ascii"), salt, iterations, 20)
    key = dk[:16]
    iv = hashlib.sha1(dk[16:20]).digest()[:16]
    params = _sequence(core.OctetString(salt), core.Integer(iterations))
    return EncryptedPrivateKeyInfo(
        {
            "encryption_algorithm": {
                "algorithm": OID_SEED_CBC_WITH_SHA1,
                "parameters": params,
            },
            "encrypted_data": _seed_cbc_encrypt(plain, key, iv),
        }
    ).dump()


def _encrypt_pbes2_seed(
    plain: bytes,
    password: str,
    iterations: int,
    key_length: int,
    prf_oid: str,
) -> bytes:
    salt = b"87654321"
    iv = b"abcdefghijklmnop"
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt, iterations, key_length)
    kdf_params = _sequence(
        core.OctetString(salt),
        core.Integer(iterations),
        core.Integer(key_length),
        AlgorithmIdentifier({"algorithm": prf_oid, "parameters": core.Null()}),
    )
    pbes2_params = _sequence(
        AlgorithmIdentifier({"algorithm": OID_PBKDF2, "parameters": kdf_params}),
        AlgorithmIdentifier({"algorithm": OID_SEED_CBC, "parameters": core.OctetString(iv)}),
    )
    return EncryptedPrivateKeyInfo(
        {
            "encryption_algorithm": {
                "algorithm": OID_PBES2,
                "parameters": pbes2_params,
            },
            "encrypted_data": _seed_cbc_encrypt(plain, derived[:16], iv),
        }
    ).dump()


def _sequence(*values: core.Asn1Value) -> core.Sequence:
    body = b"".join(value.dump() for value in values)
    if len(body) < 128:
        length = bytes([len(body)])
    else:
        length_bytes = len(body).to_bytes((len(body).bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(length_bytes)]) + length_bytes
    return core.Sequence.load(b"\x30" + length + body)


def _pbkdf1_sha1(password: bytes, salt: bytes, iterations: int, length: int) -> bytes:
    digest = hashlib.sha1(password + salt).digest()
    for _ in range(1, iterations):
        digest = hashlib.sha1(digest).digest()
    return digest[:length]


def _seed_cbc_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    padder = PKCS7(SEED.block_size).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(SEED(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _verify_signature(material) -> None:
    data = b"sign me"
    material.certificate.public_key().verify(
        material.sign(data),
        data,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
