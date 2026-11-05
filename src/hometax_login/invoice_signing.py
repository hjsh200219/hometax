from __future__ import annotations

import base64
import binascii
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import xmlsec
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
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


def sign_signed_info(
    signed_info: str,
    invoice_xml: str,
    material: CertificateMaterial,
    *,
    alternatives: tuple[str, ...] = (),
    on_unverified_digest: Callable[[str], None] | None = None,
) -> str:
    """홈택스 HTML5(MagicLine) 발급 서명: C04 가 준 SignedInfo 문자열 자체에 RSA 서명한다.

    홈택스 웹은 원본 XML 에 서명 요소를 붙이지 않는다. 서버가 만든 `szSignedInfo` 문자열(UTF-8)을
    RSASSA-PKCS1-v1_5 + SHA-256 으로 서명해 그 Base64 를 보내고, 서버가 Signature 를 조립한다
    (공개 스크립트 common_teet_sgnt.xml·ML4Web_API.min.js 확인, 2026-11-05).
    SignedInfo 가 미리보기와 대조한 XML 을 가리키는지 확인하려고, 같은 XML 로 이 모듈이 계산한
    DigestValue 와 SignedInfo 의 DigestValue 가 같을 때만 서명한다.

    `on_unverified_digest` 를 주면(사용자가 명시적으로 고른 「홈택스 웹 동등」 모드)
    다이제스트가 어긋나도
    서명하고 그 사실을 콜백으로 알린다. 홈택스 웹 클라이언트도 szSignedInfo 를 대조 없이 서명한다.
    이때도 구조·알고리즘·변환 조합·XPath 고정 검사는 그대로 적용된다.
    `alternatives` 에는 호출자가 미리보기와 대조를 마친 XML 만 넣는다.
    """
    if not isinstance(signed_info, str) or not signed_info.strip():
        raise InvoiceSigningError("signed_info_missing", "SignedInfo is empty")
    if len(signed_info.encode("utf-8")) > 65536:
        raise InvoiceSigningError("signed_info_too_large", "SignedInfo is too large")
    upper = signed_info.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise InvoiceSigningError("doctype_forbidden", "SignedInfo must not include a DOCTYPE")
    parser = etree.XMLParser(
        encoding="utf-8", resolve_entities=False, no_network=True, load_dtd=False
    )
    try:
        node = etree.fromstring(signed_info.encode("utf-8"), parser=parser)
    except etree.XMLSyntaxError as exc:
        raise InvoiceSigningError("signed_info_invalid", "SignedInfo could not be parsed") from exc
    qname = etree.QName(node)
    if qname.localname != "SignedInfo" or qname.namespace != DS_NS:
        raise InvoiceSigningError("signed_info_root", "SignedInfo root is not ds:SignedInfo")
    method = _single_child(node, "SignatureMethod").get("Algorithm")
    if method != _RSA_SHA256_URI:
        raise InvoiceSigningError("signed_info_method", "SignedInfo is not rsa-sha256")
    reference = _single_child(node, "Reference")
    if reference.get("URI") != "":
        raise InvoiceSigningError("signed_info_reference", "SignedInfo must reference the document")
    if _single_child(reference, "DigestMethod").get("Algorithm") != _SHA256_URI:
        raise InvoiceSigningError("signed_info_digest_method", "SignedInfo digest is not sha256")
    actual_digest = "".join(_single_child(reference, "DigestValue").itertext()).strip()

    # 홈택스가 SignedInfo 에 적은 정준화·변환을 그대로 써서 같은 XML 의 다이제스트를 다시 계산한다.
    # 실발행 SignedInfo 는 C14N(주석 제외) → XPath 순서였다(2026-11-05) — 이 모듈의 enveloped
    # 템플릿(XPath → C14N 주석 포함)과 달라 고정 템플릿으로는 대조할 수 없다.
    # 대조 후보: 미리보기와 대조한 XML(필수) + C04 가 함께 준 다른 XML(예: trnsXmlCntn).
    # 다른 후보는 TaxInvoice XML 로 읽히지 않으면 건너뛴다.
    expected = [_reference_digest(node, invoice_xml, material)]
    checked = ["xml"]
    for index, alternative in enumerate(alternatives):
        try:
            expected.append(_reference_digest(node, alternative, material))
            checked.append(f"alt{index}")
        except InvoiceSigningError as exc:
            if exc.code.startswith("signed_info_") and exc.code not in {
                "signed_info_xml_signed",
                "signed_info_digest_failed",
            }:
                raise
            checked.append(f"alt{index}:{exc.code}")
    if not actual_digest or actual_digest not in expected:
        # 원인을 한 번에 알 수 있게 정준화·변환 알고리즘과 대조한 후보를 함께 알린다(값 없음).
        detail = (
            "SignedInfo digest does not match the invoice XML"
            f" (algorithms hometax={_signed_info_algorithms(node)} checked={checked})"
        )
        if on_unverified_digest is None:
            raise InvoiceSigningError("signed_info_digest_mismatch", detail)
        on_unverified_digest(detail)

    message = signed_info.encode("utf-8")
    try:
        signature = material.private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        material.certificate.public_key().verify(
            signature, message, padding.PKCS1v15(), hashes.SHA256()
        )
    except Exception as exc:  # 키 종류·서명 실패는 모두 서명 실패로 닫는다
        raise InvoiceSigningError("sign_failed", "SignedInfo signature failed") from exc
    return base64.b64encode(signature).decode("ascii")


_C14N_URI: Final = xmlsec.constants.TransformInclC14N.href
_SIGNED_INFO_C14N: Final = {
    _C14N_URI: xmlsec.constants.TransformInclC14N,
    _C14N_WITH_COMMENTS_URI: xmlsec.constants.TransformInclC14NWithComments,
}
_SIGNED_INFO_REFERENCE_TRANSFORMS: Final = {
    **_SIGNED_INFO_C14N,
    _XPATH_URI: xmlsec.constants.TransformXPath,
}


def _pinned_reference_transforms(signed_info: etree._Element) -> list[str]:
    """SignedInfo 변환이 고정 규격(두 조합·HOMETAX_XPATH)과 같은지 확인하고 URI 목록을 돌려준다.

    서버가 보낸 XPath 식이나 namespace 선언을 그대로 평가하지 않는다 — 식이 다르면 거절한다.
    """
    reference = _single_child(signed_info, "Reference")
    transforms = _single_child(reference, "Transforms")
    _assert_exact_children(transforms, ("Transform",) * len(_children(transforms, "Transform")))
    uris = []
    for transform in _children(transforms, "Transform"):
        uri = transform.get("Algorithm")
        if uri == _XPATH_URI:
            _assert_exact_children(transform, ("XPath",))
            xpath = _single_child(transform, "XPath")
            if len(xpath) or " ".join((xpath.text or "").split()) != " ".join(
                HOMETAX_XPATH.split()
            ):
                raise InvoiceSigningError("signed_info_xpath", "unexpected SignedInfo XPath")
            if xpath.nsmap.get("ds") != DS_NS:
                raise InvoiceSigningError("signed_info_xpath", "SignedInfo XPath ds prefix")
        elif len(transform):
            raise InvoiceSigningError("signed_info_transform", "unexpected transform parameters")
        uris.append(uri)
    allowed = [
        [_C14N_URI, _XPATH_URI],
        [_XPATH_URI, _C14N_WITH_COMMENTS_URI],
    ]
    if uris not in allowed:
        raise InvoiceSigningError("signed_info_transform", "unsupported SignedInfo transforms")
    return uris


def _reference_digest(
    signed_info: etree._Element, invoice_xml: str, material: CertificateMaterial
) -> str:
    """고정 규격 안에서 SignedInfo 와 같은 정준화·변환으로 invoice_xml 다이제스트를 계산한다."""
    c14n_uri = _single_child(signed_info, "CanonicalizationMethod").get("Algorithm")
    c14n = _SIGNED_INFO_C14N.get(c14n_uri)
    if c14n is None:
        raise InvoiceSigningError("signed_info_c14n", "unsupported SignedInfo canonicalization")
    uris = _pinned_reference_transforms(signed_info)
    root = _parse_invoice_xml(invoice_xml)
    if _find_signatures(root):
        raise InvoiceSigningError("signed_info_xml_signed", "invoice XML is already signed")
    signature = xmlsec.template.create(root, c14n, xmlsec.constants.TransformRsaSha256, ns="ds")
    root.append(signature)
    reference = xmlsec.template.add_reference(signature, xmlsec.constants.TransformSha256, uri="")
    reference.set("URI", "")
    for uri in uris:
        added = xmlsec.template.add_transform(reference, _SIGNED_INFO_REFERENCE_TRANSFORMS[uri])
        if uri == _XPATH_URI:
            xpath = etree.SubElement(added, f"{DS}XPath", nsmap={"ds": DS_NS})
            xpath.text = HOMETAX_XPATH
    key_info = xmlsec.template.ensure_key_info(signature)
    xmlsec.template.x509_data_add_certificate(xmlsec.template.add_x509_data(key_info))

    private_key_pem = material.private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    try:
        key = xmlsec.Key.from_memory(private_key_pem, xmlsec.constants.KeyDataFormatPem)
        ctx = _signature_context(key)
        ctx.enable_signature_transform(c14n)
        ctx.enable_signature_transform(xmlsec.constants.TransformRsaSha256)
        for constant in _SIGNED_INFO_REFERENCE_TRANSFORMS.values():
            ctx.enable_reference_transform(constant)
        ctx.enable_reference_transform(xmlsec.constants.TransformSha256)
        ctx.sign(signature)
    except xmlsec.Error as exc:
        raise InvoiceSigningError("signed_info_digest_failed", "digest computation failed") from exc
    digest = signature.find(f"{DS}SignedInfo/{DS}Reference/{DS}DigestValue")
    return "".join(digest.itertext()).strip() if digest is not None else ""


def _signed_info_algorithms(signed_info: etree._Element) -> list[str]:
    reference = signed_info.find(f"{DS}Reference")
    transforms = [] if reference is None else reference.findall(f"{DS}Transforms/{DS}Transform")
    method = signed_info.find(f"{DS}CanonicalizationMethod")
    algorithms = [method.get("Algorithm", "") if method is not None else ""]
    algorithms += [transform.get("Algorithm", "") for transform in transforms]
    return [value.rsplit("/", 1)[-1].rsplit("#", 1)[-1] for value in algorithms]


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
