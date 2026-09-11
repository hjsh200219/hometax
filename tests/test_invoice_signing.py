from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree

from hometax_login.certificates import CertificateMaterial
from hometax_login.invoice_signing import (
    DS_NS,
    HOMETAX_XPATH,
    TAX_INVOICE_NS,
    InvoiceSigningError,
    build_invoice_signature_template,
    sign_invoice_xml,
    verify_invoice_xml,
)


def test_signs_and_verifies_invoice_xml() -> None:
    material = _material()

    signed = sign_invoice_xml(_invoice_xml(), material)

    root = etree.fromstring(signed.encode())
    assert etree.QName(root).localname == "TaxInvoice"
    assert etree.QName(root).namespace == TAX_INVOICE_NS
    assert len(root.xpath(".//ds:Signature", namespaces={"ds": DS_NS})) == 1
    assert root.xpath("string(.//ds:SignatureValue)", namespaces={"ds": DS_NS}).strip()
    assert root.xpath("string(.//ds:XPath)", namespaces={"ds": DS_NS}) == HOMETAX_XPATH
    verify_invoice_xml(signed, material.certificate)


def test_signs_unicode_text_with_declared_legacy_encoding_as_utf8_input() -> None:
    material = _material()
    xml = '<?xml version="1.0" encoding="EUC-KR"?>' + _invoice_xml(header="안정")

    signed = sign_invoice_xml(xml, material)

    assert "<Header>안정</Header>" in signed
    verify_invoice_xml(signed, material.certificate)


def test_verification_rejects_signed_field_tampering() -> None:
    material = _material()
    signed = sign_invoice_xml(_invoice_xml(), material)

    tampered = signed.replace("<Header>stable</Header>", "<Header>changed</Header>")

    with pytest.raises(InvoiceSigningError) as exc:
        verify_invoice_xml(tampered, material.certificate)
    assert exc.value.code == "verify_failed"


def test_rejects_external_reference_uri_before_verify() -> None:
    material = _material()
    signed = sign_invoice_xml(_invoice_xml(), material)
    root = etree.fromstring(signed.encode())
    reference = root.xpath(".//ds:Reference", namespaces={"ds": DS_NS})[0]
    reference.set("URI", "file:///etc/passwd")

    with pytest.raises(InvoiceSigningError) as exc:
        verify_invoice_xml(etree.tostring(root, encoding="unicode"), material.certificate)
    assert exc.value.code == "external_reference"


def test_rejects_unknown_xpath_transform() -> None:
    material = _material()
    signed = sign_invoice_xml(_invoice_xml(), material)
    root = etree.fromstring(signed.encode())
    xpath = root.xpath(".//ds:XPath", namespaces={"ds": DS_NS})[0]
    xpath.text = "true()"

    with pytest.raises(InvoiceSigningError) as exc:
        verify_invoice_xml(etree.tostring(root, encoding="unicode"), material.certificate)
    assert exc.value.code == "unsupported_xpath"


def test_rejects_duplicate_signature() -> None:
    material = _material()
    signed = sign_invoice_xml(_invoice_xml(), material)
    root = etree.fromstring(signed.encode())
    signature = root.xpath(".//ds:Signature", namespaces={"ds": DS_NS})[0]
    root.append(etree.fromstring(etree.tostring(signature)))

    with pytest.raises(InvoiceSigningError) as exc:
        verify_invoice_xml(etree.tostring(root, encoding="unicode"), material.certificate)
    assert exc.value.code == "signature_count"


def test_rejects_non_root_signature() -> None:
    material = _material()
    root = etree.fromstring(_invoice_xml().encode())
    nested = root.xpath("//*[local-name()='Line']")[0]
    build_invoice_signature_template(root)
    signature = root.xpath(".//ds:Signature", namespaces={"ds": DS_NS})[0]
    root.remove(signature)
    nested.append(signature)

    with pytest.raises(InvoiceSigningError) as exc:
        sign_invoice_xml(etree.tostring(root, encoding="unicode"), material)
    assert exc.value.code == "invalid_signature_parent"


def test_rejects_doctype() -> None:
    material = _material()
    xml = "<!DOCTYPE TaxInvoice [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>" + _invoice_xml()

    with pytest.raises(InvoiceSigningError) as exc:
        sign_invoice_xml(xml, material)
    assert exc.value.code == "doctype_forbidden"


def test_rejects_processing_instruction() -> None:
    material = _material()
    xml = _invoice_xml().replace("<Header>stable</Header>", "<?work test?><Header>stable</Header>")

    with pytest.raises(InvoiceSigningError) as exc:
        sign_invoice_xml(xml, material)
    assert exc.value.code == "processing_instruction_forbidden"


def test_rejects_legacy_sha1_template_by_default() -> None:
    material = _material()
    root = etree.fromstring(_invoice_xml().encode())
    build_invoice_signature_template(root, algorithm="sha1")

    with pytest.raises(InvoiceSigningError) as exc:
        sign_invoice_xml(etree.tostring(root, encoding="unicode"), material)
    assert exc.value.code == "unsupported_signature_method"


def test_allows_legacy_sha1_when_explicitly_enabled() -> None:
    material = _material()
    root = etree.fromstring(_invoice_xml().encode())
    build_invoice_signature_template(root, algorithm="sha1")

    signed = sign_invoice_xml(
        etree.tostring(root, encoding="unicode"),
        material,
        allow_legacy_sha1=True,
    )

    verify_invoice_xml(signed, material.certificate, allow_legacy_sha1=True)


def test_rejects_wrong_root_namespace() -> None:
    material = _material()
    xml = _invoice_xml().replace(TAX_INVOICE_NS, "urn:wrong", 1)

    with pytest.raises(InvoiceSigningError) as exc:
        sign_invoice_xml(xml, material)
    assert exc.value.code == "unsupported_root"


def test_rejects_prefixed_root_tax_invoice() -> None:
    material = _material()
    xml = f"""
    <h:TaxInvoice xmlns:h="{TAX_INVOICE_NS}">
      <h:Header>stable</h:Header>
      <h:Line>1000</h:Line>
      <h:ExchangedDocument>excluded</h:ExchangedDocument>
    </h:TaxInvoice>
    """

    with pytest.raises(InvoiceSigningError) as exc:
        sign_invoice_xml(xml, material)
    assert exc.value.code == "unsupported_root_prefix"


def test_rejects_rebound_ds_prefix_in_xpath_namespace_context() -> None:
    material = _material()
    signed = sign_invoice_xml(_invoice_xml(), material)
    signed = signed.replace("<ds:XPath>", f'<XPath xmlns="{DS_NS}" xmlns:ds="urn:bad">')
    signed = signed.replace("</ds:XPath>", "</XPath>")

    with pytest.raises(InvoiceSigningError) as exc:
        verify_invoice_xml(signed, material.certificate)
    assert exc.value.code == "invalid_xpath_namespace"


def test_rejects_extra_key_info_retrieval_method() -> None:
    material = _material()
    signed = sign_invoice_xml(_invoice_xml(), material)
    root = etree.fromstring(signed.encode())
    key_info = root.xpath(".//ds:KeyInfo", namespaces={"ds": DS_NS})[0]
    etree.SubElement(key_info, f"{{{DS_NS}}}RetrievalMethod", URI="file:///etc/passwd")

    with pytest.raises(InvoiceSigningError) as exc:
        verify_invoice_xml(etree.tostring(root, encoding="unicode"), material.certificate)
    assert exc.value.code == "invalid_signature"


def test_rejects_extra_reference() -> None:
    material = _material()
    signed = sign_invoice_xml(_invoice_xml(), material)
    root = etree.fromstring(signed.encode())
    signed_info = root.xpath(".//ds:SignedInfo", namespaces={"ds": DS_NS})[0]
    etree.SubElement(signed_info, f"{{{DS_NS}}}Reference", URI="https://example.invalid")

    with pytest.raises(InvoiceSigningError) as exc:
        verify_invoice_xml(etree.tostring(root, encoding="unicode"), material.certificate)
    assert exc.value.code == "invalid_signature"


def test_rejects_embedded_certificate_replacement() -> None:
    material = _material()
    other_material = _material()
    signed = sign_invoice_xml(_invoice_xml(), material)
    root = etree.fromstring(signed.encode())
    certificate_node = root.xpath(".//ds:X509Certificate", namespaces={"ds": DS_NS})[0]
    certificate_node.text = base64.b64encode(
        other_material.certificate.public_bytes(serialization.Encoding.DER)
    ).decode("ascii")

    with pytest.raises(InvoiceSigningError) as exc:
        verify_invoice_xml(etree.tostring(root, encoding="unicode"), material.certificate)
    assert exc.value.code == "certificate_mismatch"


def test_rejects_invalid_embedded_certificate_base64() -> None:
    material = _material()
    signed = sign_invoice_xml(_invoice_xml(), material)
    root = etree.fromstring(signed.encode())
    certificate_node = root.xpath(".//ds:X509Certificate", namespaces={"ds": DS_NS})[0]
    certificate_node.text = "not valid base64!"

    with pytest.raises(InvoiceSigningError) as exc:
        verify_invoice_xml(etree.tostring(root, encoding="unicode"), material.certificate)
    assert exc.value.code == "invalid_certificate"


def test_rejects_duplicate_id_attributes() -> None:
    material = _material()
    xml = f"""
    <TaxInvoice xmlns="{TAX_INVOICE_NS}">
      <Header ID="same">stable</Header>
      <Line Id="same"><Amount>1000</Amount></Line>
      <ExchangedDocument>excluded</ExchangedDocument>
    </TaxInvoice>
    """

    with pytest.raises(InvoiceSigningError) as exc:
        sign_invoice_xml(xml, material)
    assert exc.value.code == "duplicate_id"


def _invoice_xml(*, header: str = "stable") -> str:
    return f"""
    <TaxInvoice xmlns="{TAX_INVOICE_NS}">
      <Header>{header}</Header>
      <Line>
        <Amount>1000</Amount>
      </Line>
      <TaxInvoice>
        <NestedIgnoredByNameOnlyGuard>not-used-as-root</NestedIgnoredByNameOnlyGuard>
      </TaxInvoice>
      <ExchangedDocument>excluded-by-hometax-transform</ExchangedDocument>
    </TaxInvoice>
    """


def _material() -> CertificateMaterial:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Synthetic Test"),
            x509.NameAttribute(NameOID.COMMON_NAME, "invoice-signing.test"),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    private_key_der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return CertificateMaterial(cert, key, private_key_der, None)
