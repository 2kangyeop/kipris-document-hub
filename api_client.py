from __future__ import annotations

import html
import os
import re
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.request import Request, build_opener

from core import (
    NumberItem,
    canonical_document_label,
    document_pdf_filename,
    is_excluded_admin_document_title,
    normalize_number,
    safe_error_text,
)


DOMESTIC_BASE = "https://plus.kipris.or.kr/kipo-api/kipi"
DOMESTIC_OPENAPI_BASE = "https://plus.kipris.or.kr/openapi/rest"
FOREIGN_BASE = "https://plus.kipris.or.kr/openapi/rest"
GOOGLE_PATENTS_BASE = "https://patents.google.com/patent"
MAX_XML_BYTES = 10 * 1024 * 1024
MAX_HTML_BYTES = 20 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
ALLOWED_REMOTE_HOST_SUFFIXES = (
    "kipris.or.kr",
    "patents.google.com",
    "patentimages.storage.googleapis.com",
)

SUPPORTED_KIPRIS_COUNTRIES = {
    "US", "EP", "WO", "JP", "CN", "TW", "RU", "CO", "SE", "ES", "IL", "IN", "VN"
}

ADMIN_HISTORY_URL = (
    f"{DOMESTIC_OPENAPI_BASE}/RelatedDocsonfilePatService/relatedDocsonfileInfo"
)
ADMIN_PDF_SERVICES = {
    "opinion_notice": "IntermediateDocumentOPService",
    "rejection_decision": "IntermediateDocumentREService",
}


@dataclass(frozen=True)
class DownloadResult:
    destination: Path
    source: str
    canonical_number: str


@dataclass(frozen=True)
class AdminDocument:
    application_number: str
    document_number: str
    document_date: str
    title: str
    status: str


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].strip().lower()


def _find_text(root: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for element in root.iter():
        if _local_name(element.tag) in wanted and element.text:
            value = element.text.strip()
            if value:
                return value
    return ""


def _parse_admin_documents(root: ET.Element) -> list[AdminDocument]:
    documents: list[AdminDocument] = []
    for element in root.iter():
        if _local_name(element.tag) != "relateddocsonfileinfo":
            continue
        application_number = normalize_number(
            _find_text(element, "applicationNumber")
        )
        document_number = normalize_number(_find_text(element, "documentNumber"))
        title = _find_text(element, "documentTitle")
        if not document_number or not title:
            continue
        documents.append(
            AdminDocument(
                application_number=application_number,
                document_number=document_number,
                document_date=normalize_number(_find_text(element, "documentDate")),
                title=title,
                status=_find_text(element, "status"),
            )
        )
    return documents


def _admin_pdf_service(title: str) -> str:
    compact = re.sub(r"\s+", "", title)
    if "의견제출통지서" in compact or "거절이유통지서" in compact:
        return ADMIN_PDF_SERVICES["opinion_notice"]
    if "거절결정서" in compact:
        return ADMIN_PDF_SERVICES["rejection_decision"]
    return ""


def _redact_secrets(message: str) -> str:
    return re.sub(
        r"(?i)((?:accessKey|ServiceKey)=)[^&\s'\"]+",
        r"\1***",
        message,
    )


def _validate_remote_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https":
        raise RuntimeError("보안을 위해 HTTPS 주소만 다운로드할 수 있습니다.")
    if not any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_REMOTE_HOST_SUFFIXES):
        raise RuntimeError(f"허용되지 않은 다운로드 서버입니다: {host or '호스트 없음'}")


def _normalize_download_url(url: str) -> str:
    """공식 API가 돌려준 허용 서버의 HTTP 원문 주소만 HTTPS로 승격합니다."""
    value = html.unescape(str(url or "").strip())
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = any(
        host == suffix or host.endswith("." + suffix)
        for suffix in ALLOWED_REMOTE_HOST_SUFFIXES
    )
    if parsed.scheme.lower() == "http" and allowed:
        value = parsed._replace(scheme="https").geturl()
    _validate_remote_url(value)
    return value


def _read_limited(response, limit: int, label: str) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > limit:
                raise RuntimeError(f"{label} 응답이 허용 크기를 초과했습니다.")
        except ValueError:
            pass
    data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError(f"{label} 응답이 허용 크기를 초과했습니다.")
    return data


def _extract_google_pdf_url(response_text: str) -> str:
    """Extract a Google Patents PDF URL regardless of HTML attribute order."""
    normalized = html.unescape(response_text or "")
    normalized = normalized.replace("\\u002F", "/").replace("\\/", "/")
    for tag in re.findall(r"<meta\b[^>]*>", normalized, flags=re.IGNORECASE):
        attributes = {
            name.lower(): html.unescape(value)
            for name, _, value in re.findall(
                r"([:\w-]+)\s*=\s*([\"'])(.*?)\2",
                tag,
                flags=re.IGNORECASE | re.DOTALL,
            )
        }
        if attributes.get("name", "").lower() == "citation_pdf_url":
            candidate = attributes.get("content", "").strip()
            if candidate:
                return candidate
    for tag in re.findall(r"<a\b[^>]*>", normalized, flags=re.IGNORECASE):
        attributes = {
            name.lower(): html.unescape(value)
            for name, _, value in re.findall(
                r"([:\w-]+)\s*=\s*([\"'])(.*?)\2",
                tag,
                flags=re.IGNORECASE | re.DOTALL,
            )
        }
        candidate = attributes.get("href", "").strip()
        if candidate.lower().split("?", 1)[0].endswith(".pdf") and (
            attributes.get("itemprop", "").lower() == "pdflink"
            or "patentimages.storage.googleapis.com" in candidate.lower()
        ):
            return candidate
    direct = re.search(
        r"https://patentimages\.storage\.googleapis\.com/[^\s\"'<>]+?\.pdf(?:\?[^\s\"'<>]*)?",
        normalized,
        flags=re.IGNORECASE,
    )
    return html.unescape(direct.group(0)) if direct else ""


class KiprisApiClient:
    def __init__(
        self,
        api_key: str,
        google_fallback: bool,
        log,
        allow_insecure_tls: bool = False,
        cancel_event: threading.Event | None = None,
    ):
        self.api_key = api_key.strip()
        self.google_fallback = google_fallback
        self.log = log
        if allow_insecure_tls:
            raise ValueError(
                "TLS 인증서 검증 비활성화는 보안 정책상 허용되지 않습니다. "
                "사내 CA 인증서를 Windows 신뢰 저장소에 등록해 주세요."
            )
        self.allow_insecure_tls = False
        self.cancel_event = cancel_event
        self.opener = build_opener()
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
        }

    def _open(self, url: str, params: dict[str, str] | None = None, timeout: int = 40):
        if params:
            separator = "&" if "?" in url else "?"
            url = url + separator + urlencode(params)
        _validate_remote_url(url)
        try:
            response = self.opener.open(Request(url, headers=self.headers), timeout=timeout)
            _validate_remote_url(response.geturl())
            return response
        except Exception as exc:
            raise RuntimeError(
                f"보안 네트워크 요청 실패: {_redact_secrets(str(exc))}"
            ) from exc

    def _request_xml(self, url: str, params: dict[str, str]) -> ET.Element:
        with self._open(url, params=params, timeout=40) as response:
            content = _read_limited(response, MAX_XML_BYTES, "XML")
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise RuntimeError("API 응답이 XML 형식이 아닙니다.") from exc
        code = _find_text(root, "resultCode")
        message = _find_text(root, "resultMsg")
        if code and code not in {"00", "0"}:
            raise RuntimeError(f"KIPRISPlus API 오류 {code}: {message or '상세 메시지 없음'}")
        return root

    def _save_pdf(self, url: str, destination: Path) -> None:
        url = _normalize_download_url(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            try:
                if 1000 <= destination.stat().st_size <= MAX_DOWNLOAD_BYTES:
                    with destination.open("rb") as existing:
                        if existing.read(5) == b"%PDF-":
                            self.log(f"  이미 저장된 정상 PDF 건너뜀: {destination.name}")
                            return
            except OSError:
                pass
        temporary_path: Path | None = None
        converted_path: Path | None = None
        try:
            with self._open(url, timeout=90) as response:
                declared = response.headers.get("Content-Length")
                if declared:
                    try:
                        if int(declared) > MAX_DOWNLOAD_BYTES:
                            raise RuntimeError("PDF가 허용 크기(500MB)를 초과했습니다.")
                    except ValueError:
                        pass
                with tempfile.NamedTemporaryFile(
                    prefix=".kipris_download_",
                    suffix=".payload",
                    dir=destination.parent,
                    delete=False,
                ) as stream:
                    temporary_path = Path(stream.name)
                    total = 0
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise RuntimeError("PDF가 허용 크기(500MB)를 초과했습니다.")
                        stream.write(chunk)
            with temporary_path.open("rb") as downloaded:
                is_pdf = downloaded.read(5) == b"%PDF-"
            if is_pdf:
                if temporary_path.stat().st_size < 1000:
                    raise RuntimeError("저장된 PDF 파일이 비정상적으로 작습니다.")
                os.replace(temporary_path, destination)
                temporary_path = None
            else:
                from pdf_tools import convert_document_payload_to_pdf

                with tempfile.NamedTemporaryFile(
                    prefix=".kipris_converted_",
                    suffix=".pdf",
                    dir=destination.parent,
                    delete=False,
                ) as converted:
                    converted_path = Path(converted.name)
                payload_type, page_count = convert_document_payload_to_pdf(
                    temporary_path, converted_path
                )
                self.log(
                    f"  KIPRISPlus {payload_type}을 PDF로 변환했습니다. "
                    f"({page_count:,}페이지)"
                )
                os.replace(converted_path, destination)
                converted_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            if converted_path is not None:
                converted_path.unlink(missing_ok=True)

    def _domestic_digits(self, item: NumberItem) -> str:
        if item.foreign_parts and item.foreign_parts[0] == "KR":
            _, digits, _ = item.foreign_parts
            return digits
        return normalize_number(item.number)

    def _resolve_domestic_open_number(self, open_number: str) -> str:
        if not self.api_key:
            raise RuntimeError("국내 공개번호 변환에는 KIPRISPlus API Key가 필요합니다.")
        root = self._request_xml(
            f"{DOMESTIC_OPENAPI_BASE}/patUtiModInfoSearchSevice/openNumberSearchInfo",
            {"openNumber": open_number, "docsStart": "1", "accessKey": self.api_key},
        )
        application_number = _find_text(root, "ApplicationNumber", "applicationNumber")
        if not application_number:
            raise RuntimeError("공개번호에 해당하는 국내 출원번호를 찾지 못했습니다.")
        return normalize_number(application_number)

    def _domestic_pdf_path(self, application_number: str) -> tuple[str, str]:
        if not self.api_key:
            raise RuntimeError("국내 공개공보 API 이용에는 KIPRISPlus API Key가 필요합니다.")
        root = self._request_xml(
            f"{DOMESTIC_BASE}/patUtiModInfoSearchSevice/getPubFullTextInfoSearch",
            {"applicationNumber": application_number, "ServiceKey": self.api_key},
        )
        path = _find_text(root, "path")
        doc_name = _find_text(root, "docName") or f"{application_number}A.pdf"
        if not path:
            raise RuntimeError("국내 공개공보 PDF 경로가 제공되지 않았습니다.")
        return path, doc_name

    def _download_domestic(self, item: NumberItem, output_dir: Path) -> DownloadResult:
        digits = self._domestic_digits(item)
        if item.kind == "publication":
            application_number = self._resolve_domestic_open_number(digits)
            path, doc_name = self._domestic_pdf_path(application_number)
        elif item.kind == "application":
            application_number = digits
            path, doc_name = self._domestic_pdf_path(application_number)
        else:
            try:
                application_number = digits
                path, doc_name = self._domestic_pdf_path(application_number)
            except Exception:
                application_number = self._resolve_domestic_open_number(digits)
                path, doc_name = self._domestic_pdf_path(application_number)
        destination = (
            output_dir
            / application_number
            / document_pdf_filename(application_number, "공개공보")
        )
        self._save_pdf(path, destination)
        return DownloadResult(destination, "KIPRISPlus API", application_number)

    def _resolve_admin_application_number(self, item: NumberItem) -> str:
        digits = self._domestic_digits(item)
        if item.kind == "publication":
            return self._resolve_domestic_open_number(digits)
        return digits

    def _admin_document_list(self, application_number: str) -> list[AdminDocument]:
        if not self.api_key:
            raise RuntimeError("행정처리 이력 API 이용에는 KIPRISPlus API Key가 필요합니다.")
        root = self._request_xml(
            ADMIN_HISTORY_URL,
            {"applicationNumber": application_number, "accessKey": self.api_key},
        )
        return _parse_admin_documents(root)

    def _admin_pdf_path(self, document: AdminDocument, service: str) -> str:
        url = f"{DOMESTIC_OPENAPI_BASE}/{service}/pdfInfoV2"
        attempts = [
            {
                "applicationNumber": document.application_number,
                "sendNumber": document.document_number,
                "accessKey": self.api_key,
            },
            {
                "sendNumber": document.document_number,
                "accessKey": self.api_key,
            },
        ]
        errors: list[str] = []
        for params in attempts:
            try:
                root = self._request_xml(url, params)
                path = _find_text(root, "filePath", "path")
                if path:
                    return path
                errors.append("PDF 경로 없음")
            except Exception as exc:
                errors.append(safe_error_text(exc))
        raise RuntimeError(" / ".join(errors[-2:]))

    def download_admin_documents_api(
        self, item: NumberItem, output_dir: Path
    ) -> tuple[int, str, int, list[tuple[str, str]]]:
        """행정이력 목록을 조회하고 공식 PDF_V2 지원 문서를 저장합니다."""
        if not item.is_domestic:
            raise RuntimeError("국내 행정서류 API는 국내 문헌만 지원합니다.")
        application_number = self._resolve_admin_application_number(item)
        documents = self._admin_document_list(application_number)
        if not documents and item.kind == "auto":
            resolved = self._resolve_domestic_open_number(self._domestic_digits(item))
            if resolved != application_number:
                application_number = resolved
                documents = self._admin_document_list(application_number)

        folder = output_dir / application_number
        folder.mkdir(parents=True, exist_ok=True)
        saved = 0
        unsupported = 0
        failures: list[tuple[str, str]] = []
        included_documents = [
            document
            for document in documents
            if not is_excluded_admin_document_title(document.title)
        ]
        skipped = len(documents) - len(included_documents)
        if skipped:
            self.log(f"  제외: [출원서등 보정]보정서 {skipped}개")
        label_counts = Counter(
            canonical_document_label(doc.title) for doc in included_documents
        )
        label_occurrences: defaultdict[str, int] = defaultdict(int)
        for document in included_documents:
            if self.cancel_event is not None and self.cancel_event.is_set():
                self.log("  사용자 중지 요청으로 남은 API 행정서류를 건너뜁니다.")
                break
            service = _admin_pdf_service(document.title)
            if not service:
                unsupported += 1
                continue
            try:
                path = self._admin_pdf_path(document, service)
                label = canonical_document_label(document.title)
                label_occurrences[label] += 1
                filename = document_pdf_filename(
                    application_number,
                    document.title,
                    label_occurrences[label],
                    label_counts[label],
                )
                destination = folder / filename
                self._save_pdf(path, destination)
                self.log(f"  저장: {filename} [KIPRISPlus PDF_V2]")
                saved += 1
            except Exception as exc:
                safe_error = safe_error_text(exc)
                failures.append((document.title, safe_error))
                self.log(
                    f"  행정서류 API 원문 실패, 브라우저 보완 예정: "
                    f"{document.title} / {safe_error}"
                )
        return saved, application_number, unsupported, failures

    def _foreign_literature_number(self, item: NumberItem) -> tuple[str, str]:
        parts = item.foreign_parts
        if not parts or parts[0] == "KR":
            raise RuntimeError("외국 공개·등록번호 형식이 아닙니다.")
        country, digits, kind_code = parts
        if country not in SUPPORTED_KIPRIS_COUNTRIES:
            raise RuntimeError(f"KIPRISPlus 해외 API 미지원 국가코드: {country}")
        if self.api_key:
            lookup_errors: list[str] = []
            for open_number in dict.fromkeys((digits + kind_code, digits)):
                try:
                    root = self._request_xml(
                        f"{FOREIGN_BASE}/ForeignPatentAdvencedSearchService/openNumberSearch",
                        {
                            "openNumber": open_number,
                            "collectionValues": country,
                            "accessKey": self.api_key,
                        },
                    )
                    literature_number = _find_text(root, "ltrtno")
                    if literature_number and self._literature_kind_matches(
                        literature_number, kind_code
                    ):
                        self.log(
                            f"  KIPRISPlus 문헌번호 변환: {item.number} → "
                            f"{literature_number}"
                        )
                        return country, literature_number
                except Exception as exc:
                    lookup_errors.append(safe_error_text(exc))

        if not kind_code:
            raise RuntimeError("KIPRISPlus에서 외국 문헌번호를 찾지 못했습니다.")
        if country == "JP" and len(digits) >= 7 and digits[:4].isdigit():
            normalized_digits = digits[:4] + digits[4:].zfill(8)
        else:
            normalized_digits = digits.zfill(12)
        normalized_kind = kind_code if len(kind_code) > 1 else kind_code + "0"
        literature_number = normalized_digits + normalized_kind
        self.log(
            f"  KIPRISPlus 문헌번호 예비 변환: {item.number} → "
            f"{literature_number}"
        )
        return country, literature_number

    @staticmethod
    def _literature_kind_matches(literature_number: str, requested_kind: str) -> bool:
        if not requested_kind:
            return True
        returned = re.search(r"([A-Z][0-9]?)$", literature_number.upper())
        if not returned:
            return False
        returned_kind = returned.group(1)
        requested_kind = requested_kind.upper()
        if len(requested_kind) == 1:
            return returned_kind.startswith(requested_kind)
        return returned_kind == requested_kind

    @staticmethod
    def _foreign_document_label(item: NumberItem) -> str:
        parts = item.foreign_parts
        if item.kind == "registration":
            return "등록공보"
        if not parts:
            return "공개공보"
        country, digits, kind_code = parts
        if kind_code[:1] in {"B", "C", "E", "U", "Y", "S"}:
            return "등록공보"
        # 미국의 숫자 7~8자리 + A는 구 제도의 등록특허 번호입니다.
        if country == "US" and kind_code == "A" and len(digits) <= 8:
            return "등록공보"
        return "공개공보"

    def _download_foreign_kipris(self, item: NumberItem, output_dir: Path) -> DownloadResult:
        if not self.api_key:
            raise RuntimeError("외국공보 KIPRISPlus API 이용에는 API Key가 필요합니다.")
        country, literature_number = self._foreign_literature_number(item)
        root = self._request_xml(
            f"{FOREIGN_BASE}/ForeignPatentImageAndFullTextService/openFullTextInfo",
            {
                "literatureNumber": literature_number,
                "countryCode": country,
                "accessKey": self.api_key,
            },
        )
        path = _find_text(root, "path")
        if not path:
            raise RuntimeError("외국 공개·등록공보 원문 경로가 제공되지 않았습니다.")
        label = self._foreign_document_label(item)
        destination = (
            output_dir
            / item.number
            / document_pdf_filename(item.number, label)
        )
        self._save_pdf(path, destination)
        return DownloadResult(destination, "KIPRISPlus 해외특허 API", item.number)

    def _download_google(self, item: NumberItem, output_dir: Path) -> DownloadResult:
        if not item.foreign_parts:
            raise RuntimeError("Google Patents 대체 다운로드에는 국가코드가 포함된 공개번호가 필요합니다.")
        pdf_url = ""
        page_errors: list[str] = []
        country, digits, kind_code = item.foreign_parts
        locales = {
            "JP": ("en", "ja"),
            "CN": ("en", "zh"),
            "TW": ("en", "zh"),
        }.get(country, ("en",))
        identifiers = [item.number]
        if kind_code.endswith("0"):
            identifiers.append(f"{country}{digits}{kind_code[0]}")
        for identifier in dict.fromkeys(identifiers):
            for locale in locales:
                page_url = f"{GOOGLE_PATENTS_BASE}/{identifier}/{locale}"
                try:
                    with self._open(page_url, timeout=40) as response:
                        response_text = _read_limited(
                            response, MAX_HTML_BYTES, "HTML"
                        ).decode("utf-8", errors="replace")
                    pdf_url = _extract_google_pdf_url(response_text)
                    if pdf_url:
                        break
                except Exception as exc:
                    page_errors.append(safe_error_text(exc))
            if pdf_url:
                break
        if not pdf_url:
            detail = f" ({' / '.join(page_errors[-2:])})" if page_errors else ""
            raise RuntimeError(
                "Google Patents 국가별 페이지에서 PDF 링크를 찾지 못했습니다."
                + detail
            )
        label = self._foreign_document_label(item)
        destination = (
            output_dir
            / item.number
            / document_pdf_filename(item.number, label)
        )
        self._save_pdf(pdf_url, destination)
        return DownloadResult(destination, "Google Patents", item.number)

    def download_publication(self, item: NumberItem, output_dir: Path) -> DownloadResult:
        if item.is_domestic:
            try:
                return self._download_domestic(item, output_dir)
            except Exception as kipris_error:
                if self.google_fallback and item.foreign_parts:
                    self.log(f"  KIPRISPlus 실패, Google Patents로 재시도: {kipris_error}")
                    return self._download_google(item, output_dir)
                raise
        if self.google_fallback:
            try:
                self.log("  외국공보를 Google Patents에서 확인합니다.")
                return self._download_google(item, output_dir)
            except Exception as google_error:
                self.log(
                    "  Google Patents PDF 없음, KIPRISPlus 해외 API로 재시도: "
                    f"{safe_error_text(google_error)}"
                )
        return self._download_foreign_kipris(item, output_dir)
