from __future__ import annotations

import base64
import binascii
from collections import Counter
from dataclasses import dataclass
from typing import Final

import xmlsec
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from lxml import etree

from .certificates import CertificateMaterial

DS_NS: Final = "http://www.w3.org/2000/09/xmldsig#"
DS: Final = f"{{{DS_NS}}}"
MAX_INVOICE_XML_BYTES: Final = 1_048_576
TAX_INVOICE_NS: Final = (
    "urn:kr:or:kec:standard:Tax:ReusableAggregateBusinessInformationEntitySchemaModule:1:0"
)
ROOT_TAG: Final = "TaxInvoice"
HOMETAX_XPATH: Final = (
    "not(self::*[name()='TaxInvoice'] | "
    "ancestor-or-self::*[name()='ExchangedDocument'] | "
    "ancestor-or-self::ds:Signature)"
)

ALGORITHM_SHA256: Final = "sha256"
ALGORITHM_SHA1: Final = "sha1"

_C14N_WITH_COMMENTS_URI: Final = xmlsec.constants.TransformInclC14NWithComments.href
_XPATH_URI: Final = xmlsec.constants.TransformXPath.href
_RSA_SHA256_URI: Final = xmlsec.constants.TransformRsaSha256.href
_SHA256_URI: Final = xmlsec.constants.TransformSha256.href
_RSA_SHA1_URI: Final = xmlsec.constants.TransformRsaSha1.href
_SHA1_URI: Final = xmlsec.constants.TransformSha1.href


class InvoiceSigningError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class _AlgorithmProfile:
    signature_transform: object
    digest_transform: object
    signature_uri: str
    digest_uri: str


_ALGORITHMS: Final = {
    ALGORITHM_SHA256: _AlgorithmProfile(
        xmlsec.constants.TransformRsaSha256,
        xmlsec.constants.TransformSha256,
        _RSA_SHA256_URI,
        _SHA256_URI,
    ),
    ALGORITHM_SHA1: _AlgorithmProfile(
        xmlsec.constants.TransformRsaSha1,
        xmlsec.constants.TransformSha1,
        _RSA_SHA1_URI,
        _SHA1_URI,
    ),
}


def sign_invoice_xml(
    xml_text: str,
    material: CertificateMaterial,
    *,
    allow_legacy_sha1: bool = False,
) -> str:
    root = _parse_invoice_xml(xml_text)
    signatures = _find_signatures(root)
    if len(signatures) == 0:
        signature = build_invoice_signature_template(root)
    elif len(signatures) == 1:
        signature = signatures[0]
    else:
        raise InvoiceSigningError(
            "duplicate_signature",
            "invoice XML must contain at most one Signature",
        )

    _validate_signature_contract(signature, allow_legacy_sha1=allow_legacy_sha1)
    profile = _profile_from_template(signature, allow_legacy_sha1=allow_legacy_sha1)
    certificate_der = material.certificate.public_bytes(serialization.Encoding.DER)
    _set_embedded_certificate(signature, certificate_der)

    private_key_pem = material.private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    try:
        key = xmlsec.Key.from_memory(private_key_pem, xmlsec.constants.KeyDataFormatPem)
        key.load_cert_from_memory(
            certificate_der,
            xmlsec.constants.KeyDataFormatCertDer,
        )
    except xmlsec.Error as exc:
        raise InvoiceSigningError(
            "key_load_failed",
            "invoice signing key could not be loaded",
        ) from exc

    ctx = _signature_context(key)
    _enable_transforms(ctx, profile)
    try:
        ctx.sign(signature)
    except xmlsec.Error as exc:
        raise InvoiceSigningError("sign_failed", "invoice XML signature generation failed") from exc
    signed = etree.tostring(root, encoding="unicode", xml_declaration=False)
    verify_invoice_xml(
        signed,
        material.certificate,
        allow_legacy_sha1=allow_legacy_sha1,
    )
    return signed


def verify_invoice_xml(
    xml_text: str,
    certificate: x509.Certificate,
    *,
    allow_legacy_sha1: bool = False,
) -> None:
    root = _parse_invoice_xml(xml_text)
    signatures = _find_signatures(root)
    if len(signatures) != 1:
        raise InvoiceSigningError(
            "signature_count",
            "invoice XML must contain exactly one Signature",
        )

    signature = signatures[0]
    _validate_signature_contract(signature, allow_legacy_sha1=allow_legacy_sha1)
    profile = _profile_from_template(signature, allow_legacy_sha1=allow_legacy_sha1)
    certificate_der = certificate.public_bytes(serialization.Encoding.DER)
    _assert_embedded_certificate(signature, certificate_der)

    try:
        key = xmlsec.Key.from_memory(
            certificate_der,
            xmlsec.constants.KeyDataFormatCertDer,
        )
    except xmlsec.Error as exc:
        raise InvoiceSigningError(
            "key_load_failed",
            "invoice verification certificate could not be loaded",
        ) from exc
    ctx = _signature_context(key)
    _enable_transforms(ctx, profile)
    try:
        ctx.verify(signature)
    except xmlsec.Error as exc:
        raise InvoiceSigningError(
            "verify_failed",
            "invoice XML signature verification failed",
        ) from exc


def build_invoice_signature_template(
    root: etree._Element,
    *,
    algorithm: str = ALGORITHM_SHA256,
) -> etree._Element:
    _validate_root(root)
    profile = _ALGORITHMS.get(algorithm)
    if profile is None:
        raise InvoiceSigningError("unsupported_algorithm", "unsupported signature algorithm")

    signature = xmlsec.template.create(
        root,
        xmlsec.constants.TransformInclC14NWithComments,
        profile.signature_transform,
        ns="ds",
    )
    root.append(signature)
    reference = xmlsec.template.add_reference(signature, profile.digest_transform, uri="")
    reference.set("URI", "")
    xpath_transform = xmlsec.template.add_transform(reference, xmlsec.constants.TransformXPath)
    xpath = etree.SubElement(xpath_transform, f"{DS}XPath", nsmap={"ds": DS_NS})
    xpath.text = HOMETAX_XPATH
    xmlsec.template.add_transform(reference, xmlsec.constants.TransformInclC14NWithComments)
    key_info = xmlsec.template.ensure_key_info(signature)
    x509_data = xmlsec.template.add_x509_data(key_info)
    xmlsec.template.x509_data_add_certificate(x509_data)
    return signature


def _parse_invoice_xml(xml_text: str) -> etree._Element:
    if not isinstance(xml_text, str):
        raise InvoiceSigningError("invalid_input", "invoice XML must be a string")
    data = xml_text.encode("utf-8")
    if len(data) == 0:
        raise InvoiceSigningError("empty_xml", "invoice XML must be non-empty")
    if len(data) > MAX_INVOICE_XML_BYTES:
        raise InvoiceSigningError("xml_too_large", "invoice XML is too large")
    upper_xml = xml_text.upper()
    if "<!DOCTYPE" in upper_xml or "<!ENTITY" in upper_xml:
        raise InvoiceSigningError("doctype_forbidden", "invoice XML must not include a DOCTYPE")

    parser = etree.XMLParser(
        encoding="utf-8",
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        remove_blank_text=False,
    )
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise InvoiceSigningError("invalid_xml", "invoice XML could not be parsed") from exc

    tree = root.getroottree()
    if tree.docinfo.doctype:
        raise InvoiceSigningError("doctype_forbidden", "invoice XML must not include a DOCTYPE")
    if tree.xpath("//processing-instruction()"):
        raise InvoiceSigningError(
            "processing_instruction_forbidden",
            "invoice XML must not include processing instructions",
        )
    _validate_root(root)
    _reject_duplicate_ids(root)
    return root


def _validate_root(root: etree._Element) -> None:
    qname = etree.QName(root)
    if qname.localname != ROOT_TAG or qname.namespace != TAX_INVOICE_NS:
        raise InvoiceSigningError(
            "unsupported_root",
            "invoice XML root must be TaxInvoice in the HomeTax tax invoice namespace",
        )
    if root.prefix is not None:
        raise InvoiceSigningError(
            "unsupported_root_prefix",
            "invoice XML root TaxInvoice must use the default namespace",
        )


def _find_signatures(root: etree._Element) -> list[etree._Element]:
    return root.xpath(".//ds:Signature", namespaces={"ds": DS_NS})


def _reject_duplicate_ids(root: etree._Element) -> None:
    ids: list[str] = []
    for element in root.iter():
        for attr_name in ("ID", "Id", "id"):
            value = element.get(attr_name)
            if value:
                ids.append(value)
    duplicated = [value for value, count in Counter(ids).items() if count > 1]
    if duplicated:
        raise InvoiceSigningError("duplicate_id", "invoice XML contains duplicate ID attributes")


def _validate_signature_contract(signature: etree._Element, *, allow_legacy_sha1: bool) -> None:
    if etree.QName(signature).namespace != DS_NS:
        raise InvoiceSigningError("invalid_signature", "Signature must use the XMLDSig namespace")
    parent = signature.getparent()
    if parent is None or parent is not signature.getroottree().getroot():
        raise InvoiceSigningError("invalid_signature_parent", "Signature must be a root child")

    _assert_exact_children(signature, ("SignedInfo", "SignatureValue", "KeyInfo"))
    signed_info = _single_child(signature, "SignedInfo")
    _assert_exact_children(
        signed_info,
        ("CanonicalizationMethod", "SignatureMethod", "Reference"),
    )
    c14n_method = _single_child(signed_info, "CanonicalizationMethod")
    signature_method = _single_child(signed_info, "SignatureMethod")
    reference = _single_child(signed_info, "Reference")
    _assert_exact_children(reference, ("Transforms", "DigestMethod", "DigestValue"))
    transforms = _single_child(reference, "Transforms")
    transform_nodes = _children(transforms, "Transform")
    digest_method = _single_child(reference, "DigestMethod")
    _single_child(reference, "DigestValue")

    if c14n_method.get("Algorithm") != _C14N_WITH_COMMENTS_URI:
        raise InvoiceSigningError("unsupported_c14n", "unsupported canonicalization method")
    if reference.get("URI", "") != "":
        raise InvoiceSigningError(
            "external_reference",
            "invoice signature Reference URI must be empty",
        )

    allowed_signature_methods = {_RSA_SHA256_URI}
    allowed_digest_methods = {_SHA256_URI}
    if allow_legacy_sha1:
        allowed_signature_methods.add(_RSA_SHA1_URI)
        allowed_digest_methods.add(_SHA1_URI)
    if signature_method.get("Algorithm") not in allowed_signature_methods:
        raise InvoiceSigningError("unsupported_signature_method", "unsupported signature method")
    if digest_method.get("Algorithm") not in allowed_digest_methods:
        raise InvoiceSigningError("unsupported_digest_method", "unsupported digest method")

    transform_algorithms = [node.get("Algorithm") for node in transform_nodes]
    if transform_algorithms != [_XPATH_URI, _C14N_WITH_COMMENTS_URI]:
        raise InvoiceSigningError(
            "unsupported_transform",
            "unsupported signature transform sequence",
        )

    _assert_exact_children(transform_nodes[0], ("XPath",))
    _assert_exact_children(transform_nodes[1], ())
    xpath_node = _single_child(transform_nodes[0], "XPath")
    if xpath_node.nsmap.get("ds") != DS_NS:
        raise InvoiceSigningError("invalid_xpath_namespace", "XPath must bind ds to XMLDSig")
    if xpath_node.text != HOMETAX_XPATH:
        raise InvoiceSigningError("unsupported_xpath", "unsupported invoice signature XPath")

    _single_child(signature, "SignatureValue")
    key_info = _single_child(signature, "KeyInfo")
    _assert_exact_children(key_info, ("X509Data",))
    x509_data = _single_child(key_info, "X509Data")
    _assert_exact_children(x509_data, ("X509Certificate",))
    _single_child(x509_data, "X509Certificate")


def _set_embedded_certificate(signature: etree._Element, certificate_der: bytes) -> None:
    certificate_node = _embedded_certificate_node(signature)
    certificate_node.text = base64.b64encode(certificate_der).decode("ascii")


def _assert_embedded_certificate(signature: etree._Element, certificate_der: bytes) -> None:
    embedded_der = _decode_embedded_certificate(_embedded_certificate_node(signature))
    if embedded_der != certificate_der:
        raise InvoiceSigningError(
            "certificate_mismatch",
            "embedded signature certificate does not match verification certificate",
        )


def _embedded_certificate_node(signature: etree._Element) -> etree._Element:
    key_info = _single_child(signature, "KeyInfo")
    x509_data = _single_child(key_info, "X509Data")
    return _single_child(x509_data, "X509Certificate")


def _decode_embedded_certificate(certificate_node: etree._Element) -> bytes:
    encoded = "".join((certificate_node.text or "").split())
    if not encoded:
        raise InvoiceSigningError("invalid_certificate", "embedded certificate is empty")
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvoiceSigningError(
            "invalid_certificate",
            "embedded certificate is not valid base64",
        ) from exc


def _profile_from_template(
    signature: etree._Element,
    *,
    allow_legacy_sha1: bool,
) -> _AlgorithmProfile:
    signed_info = _single_child(signature, "SignedInfo")
    signature_method = _single_child(signed_info, "SignatureMethod")
    algorithm = signature_method.get("Algorithm")
    if algorithm == _RSA_SHA256_URI:
        return _ALGORITHMS[ALGORITHM_SHA256]
    if algorithm == _RSA_SHA1_URI and allow_legacy_sha1:
        return _ALGORITHMS[ALGORITHM_SHA1]
    raise InvoiceSigningError("unsupported_signature_method", "unsupported signature method")


def _single_child(parent: etree._Element, local_name: str) -> etree._Element:
    children = _children(parent, local_name)
    if len(children) != 1:
        raise InvoiceSigningError("invalid_signature", f"Signature must contain one {local_name}")
    return children[0]


def _assert_exact_children(parent: etree._Element, local_names: tuple[str, ...]) -> None:
    actual = [etree.QName(child).localname for child in parent if isinstance(child.tag, str)]
    expected = list(local_names)
    if actual != expected:
        raise InvoiceSigningError(
            "invalid_signature",
            "Signature contains unsupported XMLDSig child elements",
        )
    for child in parent:
        if isinstance(child.tag, str) and etree.QName(child).namespace != DS_NS:
            raise InvoiceSigningError(
                "invalid_signature",
                "Signature contains unsupported non-XMLDSig child elements",
            )


def _children(parent: etree._Element, local_name: str) -> list[etree._Element]:
    return [
        child
        for child in parent
        if isinstance(child.tag, str)
        and etree.QName(child).namespace == DS_NS
        and etree.QName(child).localname == local_name
    ]


def _signature_context(key: xmlsec.Key) -> xmlsec.SignatureContext:
    ctx = xmlsec.SignatureContext()
    ctx.key = key
    return ctx


def _enable_transforms(ctx: xmlsec.SignatureContext, profile: _AlgorithmProfile) -> None:
    ctx.enable_signature_transform(xmlsec.constants.TransformInclC14NWithComments)
    ctx.enable_signature_transform(profile.signature_transform)
    ctx.enable_reference_transform(xmlsec.constants.TransformXPath)
    ctx.enable_reference_transform(xmlsec.constants.TransformInclC14NWithComments)
    ctx.enable_reference_transform(profile.digest_transform)
