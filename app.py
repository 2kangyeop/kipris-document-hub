from __future__ import annotations

import csv
import hashlib
import os
import queue
import re
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

BooleanVar = None
Canvas = None
END = None
EXTENDED = None
Listbox = None
StringVar = None
filedialog = None
messagebox = None

ctk = None
Image = None
ImageOps = None
ImageTk = None

try:
    from playwright.sync_api import (
        Error as PlaywrightError,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ModuleNotFoundError:
    Page = Any
    sync_playwright = None

    class PlaywrightError(Exception):
        pass

    class PlaywrightTimeoutError(Exception):
        pass

try:
    import keyring
except ModuleNotFoundError:
    keyring = None

from api_client import MAX_DOWNLOAD_BYTES, KiprisApiClient, _validate_remote_url
from core import (
    ADMIN_DOCUMENT_NAME_PATTERN,
    AMENDMENT_OPINION_PATTERNS,
    NumberItem,
    blob_url_from_iframe_src,
    canonical_document_label,
    document_pdf_filename,
    is_downloadable_admin_control,
    is_excluded_admin_document_title,
    kipris_document_candidate_url,
    load_number_file,
    normalize_number,
    parse_open_document_call,
    parse_text_items,
    safe_error_text,
    safe_filename,
)
extract_pdf_text = None
list_folder_pdf_files = None
list_pdf_folders = None
merge_pdfs = None
render_pdf_page = None


BASE_URL = "https://www.kipris.or.kr/khome/search/searchResult.do?tab=patent"
DEFAULT_DELAY_SECONDS = 0.35
CREDENTIAL_SERVICE = "KIPRIS Document Downloader"
CREDENTIAL_USERNAME = "REST AccessKey"
COLOR_BG = "#090D14"
COLOR_CARD = "#131A25"
COLOR_CARD_ALT = "#101620"
COLOR_BORDER = "#263246"
COLOR_TEXT = "#F4F7FC"
COLOR_MUTED = "#8D9AAF"
COLOR_ACCENT = "#4B8DFF"
COLOR_ACCENT_HOVER = "#3677E6"
RENDERED_FALLBACK_MARKER = b"%KIPRIS_SCREEN_RENDER_FALLBACK"

APPLICATION_PATTERNS = (
    "특허출원서",
    "실용신안등록출원서",
    "국어번역문",
)

EXAMINATION_PATTERNS = (
    "의견제출통지서",
    "거절결정서",
    "등록결정서",
    "보정요구서",
    "보정명령서",
    "협의통지서",
    "최후거절이유통지서",
)


def app_resource_path(relative_path: str) -> Path:
    """일반 실행과 PyInstaller 단일 EXE에서 같은 리소스 경로를 사용합니다."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / relative_path
    return Path(__file__).resolve().parent / relative_path


def write_csv_atomic(path: Path, header: list[str], rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            prefix=".kipris_report_",
            suffix=".csv",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def is_rendered_fallback_pdf(path: Path) -> bool:
    """Return True for PDFs created by the emergency Chrome print fallback."""
    try:
        size = path.stat().st_size
        if size <= 0:
            return False
        with path.open("rb") as stream:
            head = stream.read(min(size, 256 * 1024))
            if size > 256 * 1024:
                stream.seek(max(0, size - 256 * 1024))
                tail = stream.read(256 * 1024)
            else:
                tail = b""
        sample = head + tail
        return any(
            marker in sample
            for marker in (
                RENDERED_FALLBACK_MARKER,
                b"Skia/PDF",
                b"HeadlessChrome",
            )
        )
    except OSError:
        return False


def extract_viewer_page_count(text: str, explicit_values=()) -> int:
    """Extract a plausible total-page count from a PDF/image viewer UI."""
    candidates: list[int] = []
    for value in explicit_values:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if 1 <= number <= 5000:
            candidates.append(number)
    normalized = re.sub(r"\s+", " ", str(text or ""))
    for match in re.finditer(
        r"(?<!\d)(\d{1,4})(\s*)/(\s*)(\d{1,4})(?!\d)", normalized
    ):
        current = int(match.group(1))
        number = int(match.group(4))
        nearby = normalized[max(0, match.start() - 20):match.end() + 20].lower()
        has_spacing = bool(match.group(2) or match.group(3))
        has_page_marker = "페이지" in nearby or "page" in nearby
        if current <= number and (has_spacing or has_page_marker) and 1 <= number <= 5000:
            candidates.append(number)
    patterns = (
        r"(?:전체|총)\s*(\d{1,4})\s*(?:페이지|쪽|page)",
        r"(?:페이지|page)\s*\d{1,4}\s*(?:/|of)\s*(\d{1,4})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
            number = int(match.group(1))
            if 1 <= number <= 5000:
                candidates.append(number)
    return max(candidates, default=0)


def _document_payload_suffix(data: bytes) -> str:
    signature = data[:16]
    if signature.startswith(b"%PDF-"):
        return ".pdf"
    if signature.startswith(b"PK\x03\x04"):
        return ".zip"
    if signature.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    if signature.startswith(b"\x89PNG"):
        return ".png"
    if signature.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if signature[:6] in {b"GIF87a", b"GIF89a"}:
        return ".gif"
    if signature.startswith(b"BM"):
        return ".bmp"
    if signature[4:12] in {b"jP  \r\n\x87\n", b"ftypjp2 ", b"ftypjpx "}:
        return ".jp2"
    return ""


def load_saved_api_key() -> str:
    environment_key = os.environ.get("KIPRIS_API_KEY", "").strip()
    if environment_key:
        return environment_key
    if keyring is None:
        return ""
    try:
        return (keyring.get_password(CREDENTIAL_SERVICE, CREDENTIAL_USERNAME) or "").strip()
    except Exception:
        return ""


def save_api_key(api_key: str) -> None:
    if keyring is None:
        raise RuntimeError("Windows 자격 증명 저장 모듈이 설치되지 않았습니다.")
    keyring.set_password(CREDENTIAL_SERVICE, CREDENTIAL_USERNAME, api_key)


def delete_saved_api_key() -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(CREDENTIAL_SERVICE, CREDENTIAL_USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass


class KiprisAdminDownloader:
    """KIPRIS 화면에서 국내 출원서와 공개된 심사서류만 내려받습니다."""

    def __init__(
        self,
        output_dir: Path,
        log,
        headless: bool = False,
        allow_insecure_tls: bool = False,
        cancel_event: threading.Event | None = None,
    ):
        self.output_dir = output_dir
        self.log = log
        self.headless = headless
        self.allow_insecure_tls = allow_insecure_tls
        self.cancel_event = cancel_event
        self.playwright = None
        self.browser = None
        self.context = None
        self.page: Page | None = None

    def __enter__(self):
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright가 설치되지 않았습니다. 프로그램 폴더의 "
                "install_windows.bat를 먼저 실행해 주세요."
            )
        self.playwright = sync_playwright().start()
        launch_errors: list[str] = []
        for channel in ("chrome", "msedge"):
            try:
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-popup-blocking",
                ]
                if self.allow_insecure_tls:
                    launch_args.append("--ignore-certificate-errors")
                self.browser = self.playwright.chromium.launch(
                    channel=channel,
                    headless=self.headless,
                    args=launch_args,
                )
                self.log(f"시스템 브라우저 사용: {channel}")
                break
            except PlaywrightError as exc:
                launch_errors.append(f"{channel}: {safe_error_text(exc)}")
        if self.browser is None:
            raise RuntimeError(
                "Google Chrome 또는 Microsoft Edge를 실행하지 못했습니다. "
                "Chrome이 설치되어 있는지 확인해 주세요. 별도의 Playwright Chromium은 "
                "사내 인증서 문제 때문에 사용하지 않습니다."
            )
        self.context = self.browser.new_context(
            accept_downloads=True,
            locale="ko-KR",
            ignore_https_errors=self.allow_insecure_tls,
        )
        self.context.set_default_timeout(15000)
        self.page = self.context.new_page()
        if self.allow_insecure_tls:
            self.log("  주의: 사용자가 HTTPS 인증서 오류 허용을 활성화했습니다.")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def _domestic_digits(self, item: NumberItem) -> str:
        if item.foreign_parts and item.foreign_parts[0] == "KR":
            return item.foreign_parts[1]
        return normalize_number(item.number)

    def _query_candidates(self, item: NumberItem) -> list[str]:
        number = self._domestic_digits(item)
        if item.kind == "application":
            return [f"AN=[{number}]"]
        if item.kind == "publication":
            return [f"OPN=[{number}]"]
        return [f"AN=[{number}]", f"OPN=[{number}]"]

    def _dismiss_search_modal(self) -> None:
        assert self.page is not None
        modal = self.page.locator("#modalSearchDetail.modal.active")
        if modal.count() == 0 or not modal.first.is_visible():
            return
        try:
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(250)
        except Exception:
            pass
        if modal.count() and modal.first.is_visible():
            self.page.evaluate(
                """() => {
                    const modal = document.querySelector('#modalSearchDetail.modal.active');
                    if (!modal) return;
                    modal.classList.remove('active');
                    modal.setAttribute('aria-hidden', 'true');
                    modal.style.display = 'none';
                    document.body.classList.remove('modal-open');
                }"""
            )
        self.log("  상세검색 안내창을 닫았습니다.")

    def _select_detail_page(self, before_pages: list[Page]) -> bool:
        assert self.context is not None
        for _ in range(60):
            live_pages = [page for page in self.context.pages if not page.is_closed()]
            new_pages = [page for page in live_pages if page not in before_pages]
            candidates = list(reversed(new_pages)) + list(reversed(live_pages))
            for candidate in candidates:
                try:
                    tab = candidate.get_by_role(
                        "button", name="통합행정정보", exact=True
                    )
                    if tab.count() and tab.first.is_visible():
                        self.page = candidate
                        self.log("  KIPRIS 상세화면을 확인했습니다.")
                        return True
                except Exception:
                    continue
            time.sleep(0.25)
        return False

    def _open_detail(self, item: NumberItem) -> str:
        assert self.context is not None
        for query in self._query_candidates(item):
            if self.page is None or self.page.is_closed():
                self.page = self.context.new_page()
            self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            query_box = self.page.locator('textarea[name="queryText"]').first
            query_box.wait_for(state="visible", timeout=30000)
            query_box.fill(query)
            self._dismiss_search_modal()
            search_button = self.page.locator(
                'button.btn-search[onclick*="rightTotalSearch"]'
            ).first
            if search_button.count() == 0:
                search_button = self.page.get_by_role(
                    "button", name="검색", exact=True
                ).first
            search_button.wait_for(state="visible", timeout=30000)
            try:
                search_button.click(timeout=10000)
            except PlaywrightTimeoutError:
                self.log("  검색 버튼 가림을 감지하여 KIPRIS 검색 함수를 직접 실행합니다.")
                self.page.evaluate(
                    """() => {
                        if (typeof window.rightTotalSearch !== 'function') {
                            throw new Error('rightTotalSearch 함수를 찾지 못했습니다.');
                        }
                        window.rightTotalSearch();
                    }"""
                )
            self.log(f"  KIPRIS 검색 실행: {query}")
            try:
                result = self.page.locator('button[title="상세내용 보기"]').first
                result.wait_for(state="visible", timeout=15000)
                before_pages = list(self.context.pages)
                try:
                    result.click(timeout=15000)
                except PlaywrightError:
                    if not any(
                        page not in before_pages and not page.is_closed()
                        for page in self.context.pages
                    ):
                        raise
                if not self._select_detail_page(before_pages):
                    raise PlaywrightTimeoutError(
                        "상세화면 또는 통합행정정보 탭을 찾지 못했습니다."
                    )
                application_number = ""
                try:
                    application_number = self.page.locator(
                        'input[name="resultCheck"][value]:not([value="0"])'
                    ).first.get_attribute("value") or ""
                except Exception:
                    pass
                return normalize_number(application_number) or self._domestic_digits(item)
            except PlaywrightTimeoutError:
                continue
        raise RuntimeError("KIPRIS 검색결과를 찾지 못했습니다.")

    def _validate_pdf(self, destination: Path) -> None:
        if not destination.exists() or destination.stat().st_size < 1000:
            raise RuntimeError("저장된 PDF 파일이 비정상적으로 작습니다.")
        if destination.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("저장된 PDF가 허용 크기(500MB)를 초과했습니다.")
        with destination.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise RuntimeError("저장 결과가 PDF 형식이 아닙니다.")

    def _write_pdf_bytes(
        self, destination: Path, data: bytes, minimum_pages: int = 0
    ) -> None:
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("PDF가 허용 크기(500MB)를 초과했습니다.")
        if not data.lstrip().startswith(b"%PDF-"):
            raise RuntimeError("응답 내용이 PDF 형식이 아닙니다.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".kipris_browser_",
                suffix=".pdf",
                dir=destination.parent,
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(data)
            self._validate_pdf(temporary_path)
            if minimum_pages > 1:
                from pdf_tools import validate_pdf_for_processing

                _, response_pages = validate_pdf_for_processing(temporary_path)
                if response_pages < minimum_pages:
                    raise RuntimeError(
                        "원문 응답이 전체 문서보다 짧습니다: "
                        f"{response_pages}/{minimum_pages}페이지"
                    )
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _save_playwright_download(
        self, download, destination: Path, minimum_pages: int = 0
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".kipris_event_",
                suffix=".pdf",
                dir=destination.parent,
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
            download.save_as(str(temporary_path))
            self._validate_pdf(temporary_path)
            if minimum_pages > 1:
                from pdf_tools import validate_pdf_for_processing

                _, downloaded_pages = validate_pdf_for_processing(temporary_path)
                if downloaded_pages < minimum_pages:
                    raise RuntimeError(
                        "다운로드 결과가 전체 문서보다 짧습니다: "
                        f"{downloaded_pages}/{minimum_pages}페이지"
                    )
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _save_captured_pdf_responses(
        self,
        responses: list[Any],
        destination: Path,
        expected_pages: int = 0,
    ) -> bool:
        for response in reversed(responses):
            try:
                content_type = (response.headers.get("content-type") or "").lower()
                url = response.url.lower()
                if not (
                    "pdf" in content_type
                    or "octet-stream" in content_type
                    or "download" in content_type
                    or "attachment" in (response.headers.get("content-disposition") or "").lower()
                    or any(
                        token in url
                        for token in (
                            "pdf",
                            "document",
                            "download",
                            "file",
                            "image",
                            "original",
                            "viewer",
                        )
                    )
                ):
                    continue
                declared = response.headers.get("content-length") or ""
                if declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                    continue
                body = response.body()
                if body.lstrip().startswith(b"%PDF-"):
                    if expected_pages > 1:
                        try:
                            import pymupdf

                            with pymupdf.open(
                                stream=body, filetype="pdf"
                            ) as document:
                                if document.page_count < expected_pages:
                                    continue
                        except Exception:
                            continue
                    self._write_pdf_bytes(destination, body, expected_pages)
                    self.log("  PDF 네트워크 응답에서 원문을 저장했습니다.")
                    return True
            except Exception:
                continue
        return False

    def _popup_total_pages(self, page: Page) -> int:
        total = 0
        for frame in page.frames:
            try:
                details = frame.evaluate(
                    """() => {
                        const values = [];
                        const app = globalThis.PDFViewerApplication;
                        if (app) {
                            values.push(app.pagesCount);
                            values.push(app.pdfDocument && app.pdfDocument.numPages);
                        }
                        const selectors = [
                            '[data-page-count]', '[data-total-pages]',
                            'input[id*="page" i]', 'input[name*="page" i]',
                            '[aria-label*="페이지"]', '[aria-label*="page" i]'
                        ];
                        for (const element of document.querySelectorAll(selectors.join(','))) {
                            values.push(element.dataset && element.dataset.pageCount);
                            values.push(element.dataset && element.dataset.totalPages);
                            values.push(element.getAttribute('max'));
                            values.push(element.getAttribute('aria-valuemax'));
                        }
                        return {
                            text: (document.body && document.body.innerText || '').slice(0, 200000),
                            values
                        };
                    }"""
                )
                total = max(
                    total,
                    extract_viewer_page_count(
                        details.get("text", ""), details.get("values", [])
                    ),
                )
            except Exception:
                continue
        return total

    def _save_pdfjs_original(
        self, page: Page, destination: Path, expected_pages: int = 0
    ) -> bool:
        """Download the complete PDF bytes already loaded by a PDF.js viewer."""
        for frame in page.frames:
            try:
                available = frame.evaluate(
                    """() => Boolean(
                        globalThis.PDFViewerApplication &&
                        globalThis.PDFViewerApplication.pdfDocument &&
                        typeof globalThis.PDFViewerApplication.pdfDocument.getData === 'function'
                    )"""
                )
                if not available:
                    continue
                with page.expect_download(timeout=30000) as download_info:
                    frame.evaluate(
                        """async name => {
                            const bytes = await globalThis.PDFViewerApplication.pdfDocument.getData();
                            const blob = new Blob([bytes], {type: 'application/pdf'});
                            const url = URL.createObjectURL(blob);
                            try {
                                const anchor = document.createElement('a');
                                anchor.href = url;
                                anchor.download = name;
                                anchor.style.display = 'none';
                                document.body.appendChild(anchor);
                                anchor.click();
                                anchor.remove();
                            } finally {
                                setTimeout(() => URL.revokeObjectURL(url), 30000);
                            }
                        }""",
                        destination.name,
                    )
                self._save_playwright_download(
                    download_info.value, destination, expected_pages
                )
                self.log("  PDF 뷰어 메모리에서 전체 원문을 저장했습니다.")
                return True
            except Exception:
                continue
        return False

    def _save_captured_document_payloads(
        self,
        responses: list[Any],
        destination: Path,
        expected_pages: int = 0,
    ) -> bool:
        """Save complete ZIP/TIFF payloads or all captured page images as one PDF."""
        from pdf_tools import convert_document_payload_to_pdf

        destination.parent.mkdir(parents=True, exist_ok=True)
        image_payloads: list[tuple[str, bytes]] = []
        seen_images: set[str] = set()
        image_total_bytes = 0
        for response in reversed(responses):
            payload_path: Path | None = None
            converted_path: Path | None = None
            try:
                declared = response.headers.get("content-length") or ""
                if declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                    continue
                content_type = (response.headers.get("content-type") or "").lower()
                url = response.url.lower()
                if not (
                    "image" in content_type
                    or "octet-stream" in content_type
                    or "zip" in content_type
                    or "tiff" in content_type
                    or any(
                        token in url
                        for token in ("document", "original", "viewer", "image", "file", "download")
                    )
                ):
                    continue
                body = response.body()
                if not body or len(body) > MAX_DOWNLOAD_BYTES:
                    continue
                suffix = _document_payload_suffix(body)
                if not suffix or suffix == ".pdf":
                    continue
                if suffix in {".png", ".jpg", ".gif", ".bmp", ".jp2"}:
                    # A single image can be only the current viewer page. Merge it only
                    # after every page reported by the viewer has been collected.
                    if len(body) < 12 * 1024:
                        continue
                    try:
                        import pymupdf

                        with pymupdf.open(
                            stream=body, filetype=suffix.lstrip(".")
                        ) as image_document:
                            rect = image_document[0].rect
                            if min(rect.width, rect.height) < 300:
                                continue
                    except Exception:
                        continue
                    digest = hashlib.sha256(body).hexdigest()
                    if digest not in seen_images:
                        if image_total_bytes + len(body) > MAX_DOWNLOAD_BYTES:
                            continue
                        seen_images.add(digest)
                        image_payloads.append((suffix, body))
                        image_total_bytes += len(body)
                    continue
                with tempfile.NamedTemporaryFile(
                    prefix=".kipris_payload_",
                    suffix=suffix,
                    dir=destination.parent,
                    delete=False,
                ) as stream:
                    payload_path = Path(stream.name)
                    stream.write(body)
                with tempfile.NamedTemporaryFile(
                    prefix=".kipris_payload_pdf_",
                    suffix=".pdf",
                    dir=destination.parent,
                    delete=False,
                ) as stream:
                    converted_path = Path(stream.name)
                payload_type, page_count = convert_document_payload_to_pdf(
                    payload_path, converted_path
                )
                if expected_pages > 1 and page_count < expected_pages:
                    continue
                if (
                    not expected_pages
                    and page_count == 1
                    and suffix == ".tiff"
                    and not any(token in url for token in ("original", "download", "full", "file"))
                ):
                    continue
                os.replace(converted_path, destination)
                converted_path = None
                self.log(
                    f"  브라우저에서 감지한 {payload_type}을 전체 PDF로 변환했습니다. "
                    f"({page_count:,}페이지)"
                )
                return True
            except Exception:
                continue
            finally:
                if payload_path is not None:
                    payload_path.unlink(missing_ok=True)
                if converted_path is not None:
                    converted_path.unlink(missing_ok=True)

        if expected_pages <= 1 or len(image_payloads) < expected_pages:
            return False
        # Responses were scanned newest-first. Restore viewer request order and use
        # exactly the expected number of distinct full-page images.
        selected = list(reversed(image_payloads))[-expected_pages:]
        archive_path: Path | None = None
        converted_path: Path | None = None
        try:
            import zipfile

            with tempfile.NamedTemporaryFile(
                prefix=".kipris_pages_",
                suffix=".zip",
                dir=destination.parent,
                delete=False,
            ) as stream:
                archive_path = Path(stream.name)
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for index, (suffix, body) in enumerate(selected, 1):
                    archive.writestr(f"page_{index:05d}{suffix}", body)
            with tempfile.NamedTemporaryFile(
                prefix=".kipris_pages_pdf_",
                suffix=".pdf",
                dir=destination.parent,
                delete=False,
            ) as stream:
                converted_path = Path(stream.name)
            _, page_count = convert_document_payload_to_pdf(
                archive_path, converted_path
            )
            if page_count != expected_pages:
                return False
            os.replace(converted_path, destination)
            converted_path = None
            self.log(
                f"  뷰어의 전체 페이지 이미지를 PDF로 저장했습니다. "
                f"({page_count:,}페이지)"
            )
            return True
        finally:
            if archive_path is not None:
                archive_path.unlink(missing_ok=True)
            if converted_path is not None:
                converted_path.unlink(missing_ok=True)

    def _load_all_popup_pages(self, page: Page, expected_pages: int) -> int:
        """Advance a paginated image viewer so every page response is captured."""
        if expected_pages <= 1:
            return 0
        clicked = 0
        # Avoid excessive automated requests on unusually large or malformed viewers.
        for _ in range(min(expected_pages - 1, 500)):
            best: tuple[int, Any] | None = None
            for frame in page.frames:
                try:
                    controls = frame.locator(
                        "button, a, input[type='button'], input[type='image'], [role='button']"
                    )
                    count = min(controls.count(), 120)
                except Exception:
                    continue
                for index in range(count):
                    control = controls.nth(index)
                    try:
                        if not control.is_visible() or not control.is_enabled():
                            continue
                        details = " ".join(
                            filter(
                                None,
                                (
                                    (control.inner_text(timeout=500) or "").strip(),
                                    (control.get_attribute("title") or "").strip(),
                                    (control.get_attribute("aria-label") or "").strip(),
                                    (control.get_attribute("id") or "").strip(),
                                    (control.get_attribute("class") or "").strip(),
                                    (control.get_attribute("value") or "").strip(),
                                ),
                            )
                        ).lower()
                        score = 0
                        if any(
                            marker in details
                            for marker in (
                                "다음페이지", "다음 페이지", "next page", "pagenext",
                                "page-next", "nextpage", "btnnextpage",
                            )
                        ):
                            score += 10
                        if "next" in details and "page" in details:
                            score += 8
                        if "다음" in details and ("페이지" in details or "page" in details):
                            score += 8
                        if any(marker in details for marker in ("문서 다음", "next document")):
                            score = 0
                        if score and (best is None or score > best[0]):
                            best = (score, control)
                    except Exception:
                        continue
            if best is None:
                break
            try:
                best[1].click(timeout=5000, force=True)
                clicked += 1
                page.wait_for_timeout(650)
            except Exception:
                break
        if clicked:
            self.log(
                f"  전체 페이지 확인을 위해 뷰어 다음 페이지를 {clicked:,}회 이동했습니다."
            )
        return clicked

    def _save_url_with_browser_session(
        self,
        page: Page,
        source_frame,
        absolute_url: str,
        destination: Path,
        minimum_pages: int = 0,
    ) -> None:
        """Chromium이 이미 신뢰한 사내 인증서·세션으로 URL을 PDF로 저장합니다."""
        _validate_remote_url(absolute_url)
        self.log("  별도 요청 실패: Chrome 세션 내부 다운로드로 안전하게 재시도합니다.")
        host_page = (
            self.page
            if self.page is not None and not self.page.is_closed()
            else page
        )
        fetch_frame = host_page.main_frame
        with host_page.expect_download(timeout=45000) as download_info:
            fetch_frame.evaluate(
                """async ({url, name, maxBytes}) => {
                    const response = await fetch(url, {
                        credentials: 'include',
                        cache: 'no-store',
                        redirect: 'follow'
                    });
                    if (!response.ok) {
                        throw new Error(`HTTP ${response.status}`);
                    }
                    const declared = Number(response.headers.get('content-length') || 0);
                    if (declared && declared > maxBytes) {
                        throw new Error('PDF가 허용 크기(500MB)를 초과했습니다.');
                    }
                    const blob = await response.blob();
                    if (blob.size < 1000 || blob.size > maxBytes) {
                        throw new Error('PDF 크기가 허용 범위를 벗어났습니다.');
                    }
                    const signature = new Uint8Array(await blob.slice(0, 5).arrayBuffer());
                    const expected = [37, 80, 68, 70, 45];
                    if (!expected.every((value, index) => signature[index] === value)) {
                        throw new Error('응답 내용이 PDF 형식이 아닙니다.');
                    }
                    const objectUrl = URL.createObjectURL(blob);
                    try {
                        const anchor = document.createElement('a');
                        anchor.href = objectUrl;
                        anchor.download = name;
                        anchor.style.display = 'none';
                        document.body.appendChild(anchor);
                        anchor.click();
                        anchor.remove();
                    } finally {
                        setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
                    }
                }""",
                {
                    "url": absolute_url,
                    "name": destination.name,
                    "maxBytes": MAX_DOWNLOAD_BYTES,
                },
            )
        self._save_playwright_download(
            download_info.value, destination, minimum_pages
        )
        self.log("  Chrome 세션 내부 다운로드에서 PDF 원문을 저장했습니다.")

    def _save_rendered_popup_pdf(
        self, page: Page, destination: Path, expected_pages: int = 0
    ) -> None:
        """Save the document that headless Chromium rendered when no binary URL exists."""
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(500)
        try:
            body_text = page.locator("body").inner_text(timeout=5000).strip()
        except Exception:
            body_text = ""
        lowered = body_text.lower()
        if len(body_text) < 20 and not page.locator(
            "canvas, img, embed, object, iframe"
        ).count():
            raise RuntimeError("팝업에 인쇄할 문서 내용이 없습니다.")
        if any(
            marker in lowered
            for marker in (
                "페이지를 찾을 수 없습니다",
                "서비스 이용에 불편을 드려",
                "access denied",
                "forbidden",
            )
        ):
            raise RuntimeError("팝업에 오류 화면이 표시되어 PDF로 저장하지 않았습니다.")

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".kipris_rendered_",
                suffix=".pdf",
                dir=destination.parent,
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
            page.emulate_media(media="screen")
            page.pdf(
                path=str(temporary_path),
                print_background=True,
                prefer_css_page_size=True,
                format="A4",
                margin={
                    "top": "8mm",
                    "right": "8mm",
                    "bottom": "8mm",
                    "left": "8mm",
                },
            )
            if expected_pages > 1:
                from pdf_tools import validate_pdf_for_processing

                _, rendered_pages = validate_pdf_for_processing(temporary_path)
                if rendered_pages < expected_pages:
                    raise RuntimeError(
                        "화면 인쇄 결과가 전체 문서보다 짧아 저장하지 않았습니다: "
                        f"{rendered_pages}/{expected_pages}페이지"
                    )
            with temporary_path.open("ab") as stream:
                stream.write(b"\n" + RENDERED_FALLBACK_MARKER + b"\n")
            self._validate_pdf(temporary_path)
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        self.log(
            "  원본 PDF를 찾지 못해 화면 인쇄본을 임시 저장했습니다. "
            "다음 실행에서도 원본 다운로드를 다시 시도합니다."
        )

    def _save_from_popup_download_control(
        self, page: Page, destination: Path, expected_pages: int = 0
    ) -> bool:
        """Click the viewer's own original/PDF download control before printing."""
        selector = ", ".join(
            (
                "a[download]",
                "[title*='다운']",
                "[aria-label*='다운']",
                "[title*='저장']",
                "[aria-label*='저장']",
                "[title*='원문']",
                "[aria-label*='원문']",
                "[onclick*='download' i]",
                "[onclick*='downFile' i]",
                "[onclick*='pdf' i]",
                "[id*='download' i]",
                "[id*='save' i]",
                "[class*='download' i]",
                "[class*='save' i]",
            )
        )
        attempted: set[str] = set()
        for _ in range(2):
            candidates: list[tuple[int, Any, str]] = []
            for frame in page.frames:
                try:
                    controls = frame.locator(selector)
                    control_count = min(controls.count(), 40)
                except Exception:
                    continue
                for index in range(control_count):
                    control = controls.nth(index)
                    try:
                        if not control.is_visible() or not control.is_enabled():
                            continue
                        details = " ".join(
                            filter(
                                None,
                                (
                                    (control.inner_text(timeout=1000) or "").strip(),
                                    (control.get_attribute("title") or "").strip(),
                                    (control.get_attribute("aria-label") or "").strip(),
                                    (control.get_attribute("id") or "").strip(),
                                    (control.get_attribute("class") or "").strip(),
                                    (control.get_attribute("onclick") or "").strip(),
                                ),
                            )
                        )
                        lowered = details.lower()
                        if not details or any(
                            token in lowered
                            for token in ("인쇄", "print", "이미지 저장")
                        ):
                            continue
                        score = 0
                        if "download" in lowered or "다운" in details:
                            score += 5
                        if "원문" in details:
                            score += 4
                        if "pdf" in lowered:
                            score += 3
                        if "save" in lowered or "저장" in details:
                            score += 2
                        if score >= 3:
                            candidates.append((score, control, details[:300]))
                    except Exception:
                        continue

            candidates.sort(key=lambda item: item[0], reverse=True)
            for _, control, details in candidates[:4]:
                signature = details.lower()
                if signature in attempted:
                    continue
                attempted.add(signature)
                captured_responses: list[Any] = []

                def capture_response(response):
                    if len(captured_responses) < 200:
                        captured_responses.append(response)

                page.context.on("response", capture_response)
                try:
                    try:
                        with page.expect_download(timeout=5000) as download_info:
                            control.click(timeout=5000, force=True)
                        self._save_playwright_download(
                            download_info.value, destination, expected_pages
                        )
                        self.log(
                            "  팝업의 원문 다운로드 버튼에서 PDF를 저장했습니다."
                        )
                        return True
                    except PlaywrightTimeoutError:
                        page.wait_for_timeout(500)
                        if self._save_captured_pdf_responses(
                            captured_responses, destination
                        ):
                            self.log(
                                "  팝업의 원문 버튼 응답에서 PDF를 저장했습니다."
                            )
                            return True
                    except Exception:
                        continue
                finally:
                    page.context.remove_listener("response", capture_response)
            page.wait_for_timeout(300)
        return False

    def _save_popup_pdf(
        self,
        page: Page,
        destination: Path,
        captured_responses: list[Any] | None = None,
    ) -> None:
        page.wait_for_timeout(500)
        expected_pages = self._popup_total_pages(page)
        if expected_pages:
            self.log(f"  문서 뷰어 전체 페이지 수 확인: {expected_pages:,}페이지")
        if self._save_from_popup_download_control(
            page, destination, expected_pages
        ):
            return
        if self._save_pdfjs_original(page, destination, expected_pages):
            return
        responses = captured_responses if captured_responses is not None else []
        if self._save_captured_pdf_responses(
            responses, destination, expected_pages
        ):
            return
        if self._save_captured_document_payloads(
            responses, destination, expected_pages
        ):
            return
        if expected_pages > 1:
            self._load_all_popup_pages(page, expected_pages)
            if self._save_captured_pdf_responses(
                responses, destination, expected_pages
            ):
                return
            if self._save_captured_document_payloads(
                responses, destination, expected_pages
            ):
                return
        sources: list[tuple[str, str, Any]] = []
        for frame in page.frames:
            try:
                source_elements = frame.locator(
                    "iframe[src], embed[src], object[data], a[href]"
                ).all()
            except Exception:
                continue
            for element in source_elements:
                try:
                    tag_name = element.evaluate(
                        "element => element.tagName.toLowerCase()"
                    )
                    source = (
                        element.get_attribute("src")
                        or element.get_attribute("data")
                        or element.get_attribute("href")
                        or ""
                    ).strip()
                    if not source or source.lower().startswith("javascript:"):
                        continue
                    if "viewer.html?file=" in source:
                        try:
                            source = blob_url_from_iframe_src(source)
                        except Exception:
                            pass
                    candidate = kipris_document_candidate_url(
                        source, frame.url or page.url
                    )
                    if candidate:
                        sources.append((candidate, frame.url or page.url, frame))
                except Exception:
                    continue

            try:
                resource_urls = frame.evaluate(
                    """() => performance.getEntriesByType('resource')
                        .map(entry => entry.name)
                        .filter(Boolean)"""
                )
                for resource_url in resource_urls[-200:]:
                    candidate = kipris_document_candidate_url(
                        resource_url, frame.url or page.url
                    )
                    if candidate:
                        sources.append((candidate, frame.url or page.url, frame))
            except Exception:
                pass

        errors: list[str] = []
        seen_sources: set[str] = set()
        for source, base_url, source_frame in sources:
            if source in seen_sources:
                continue
            seen_sources.add(source)
            try:
                if source.startswith("blob:"):
                    with page.expect_download(timeout=30000) as download_info:
                        source_frame.evaluate(
                            """({url, name}) => {
                                const a = document.createElement('a');
                                a.href = url;
                                a.download = name;
                                document.body.appendChild(a);
                                a.click();
                                a.remove();
                            }""",
                            {"url": source, "name": destination.name},
                        )
                    self._save_playwright_download(
                        download_info.value, destination, expected_pages
                    )
                    return

                absolute_url = kipris_document_candidate_url(source, base_url)
                if not absolute_url:
                    continue
                _validate_remote_url(absolute_url)
                try:
                    response = page.context.request.get(absolute_url, timeout=30000)
                    _validate_remote_url(response.url)
                    if response.ok:
                        declared = response.headers.get("content-length") or ""
                        if declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                            raise RuntimeError("PDF가 허용 크기(500MB)를 초과했습니다.")
                        self._write_pdf_bytes(
                            destination, response.body(), expected_pages
                        )
                        return
                    raise RuntimeError(f"원문 주소 응답 오류: HTTP {response.status}")
                except Exception as request_error:
                    errors.append(safe_error_text(request_error))
                    self._save_url_with_browser_session(
                        page,
                        source_frame,
                        absolute_url,
                        destination,
                        expected_pages,
                    )
                    return
            except Exception as exc:
                errors.append(safe_error_text(exc))
        try:
            self._save_rendered_popup_pdf(page, destination, expected_pages)
            return
        except Exception as exc:
            errors.append(safe_error_text(exc))
        detail = " / ".join(errors[-3:]) if errors else "PDF 주소를 찾지 못했습니다."
        raise RuntimeError(f"팝업에서 PDF 원문을 저장하지 못했습니다: {detail}")

    def _download_admin_documents(
        self,
        folder: Path,
    ) -> int:
        assert self.page is not None
        self.page.get_by_role("button", name="통합행정정보", exact=True).click()
        table = self.page.get_by_role("table", name="통합행정정보보기")
        table.wait_for(state="visible", timeout=30000)
        controls = table.locator("tbody a, tbody button").all()
        document_controls: list[
            tuple[Any, str, str, tuple[str, str, str, str] | None]
        ] = []
        seen: set[tuple[str, str, str]] = set()
        for control in controls:
            try:
                if not control.is_visible() or not control.is_enabled():
                    continue
                name = (control.inner_text() or "").strip()
                href = (control.get_attribute("href") or "").strip()
                onclick = (control.get_attribute("onclick") or "").strip()
                title = (control.get_attribute("title") or "").strip()
                tag_name = control.evaluate("(element) => element.tagName.toLowerCase()")
                row_text = control.locator("xpath=ancestor::tr").inner_text().strip()
                if not is_downloadable_admin_control(
                    name, href, onclick, title, row_text, tag_name
                ):
                    continue
                display_name = name
                if not display_name or display_name in {"보기", "원문", "원문보기"}:
                    matching_line = next(
                        (
                            line.strip()
                            for line in row_text.splitlines()
                            if ADMIN_DOCUMENT_NAME_PATTERN.search(line)
                        ),
                        "통합행정정보_원문",
                    )
                    display_name = matching_line
                if is_excluded_admin_document_title(display_name):
                    self.log(f"  제외: {display_name}")
                    continue
                signature = (display_name, href, onclick)
                if signature in seen:
                    continue
                seen.add(signature)
                open_document_args = parse_open_document_call(href, onclick)
                document_controls.append(
                    (control, display_name, row_text, open_document_args)
                )
                self.log(f"  원문 링크 감지: {display_name}")
                if open_document_args:
                    self.log(
                        "  openDocument 식별: "
                        f"문서번호 {open_document_args[1]}, "
                        f"문서코드 {open_document_args[2]}"
                    )
            except Exception as exc:
                self.log(f"  원문 링크 확인 실패: {safe_error_text(exc)}")

        self.log(f"  통합행정정보 원문 후보: {len(document_controls)}개")
        if not document_controls:
            raise RuntimeError(
                "통합행정정보 표에서 파란색 원문 링크를 찾지 못했습니다."
            )
        count = 0
        failures: list[tuple[str, str]] = []
        label_counts = Counter(
            canonical_document_label(name)
            for _, name, _, _ in document_controls
        )
        label_occurrences: defaultdict[str, int] = defaultdict(int)
        for document_index, (
            control,
            name,
            row_text,
            open_document_args,
        ) in enumerate(document_controls, 1):
            if self.cancel_event is not None and self.cancel_event.is_set():
                self.log("  사용자 중지 요청으로 남은 행정서류를 건너뜁니다.")
                break
            label = canonical_document_label(name)
            label_occurrences[label] += 1
            filename = document_pdf_filename(
                folder.name,
                name,
                label_occurrences[label],
                label_counts[label],
            )
            destination = folder / filename
            if destination.exists():
                try:
                    self._validate_pdf(destination)
                    if is_rendered_fallback_pdf(destination):
                        self.log(
                            f"  기존 화면 인쇄본 감지: 원본 PDF를 다시 시도합니다. "
                            f"({filename})"
                        )
                    else:
                        self.log(f"  이미 저장된 원문 건너뜀: {filename}")
                        continue
                except Exception:
                    pass
            popup = None
            captured_responses: list[Any] = []
            downloads: list[Any] = []
            context = self.page.context
            before_pages = list(context.pages)

            def capture_response(response):
                if len(captured_responses) >= 1000:
                    captured_responses.pop(0)
                captured_responses.append(response)

            def capture_download(download):
                if len(downloads) < 20:
                    downloads.append(download)

            context.on("response", capture_response)
            self.page.on("download", capture_download)
            try:
                saved_from_response = False
                control.scroll_into_view_if_needed(timeout=10000)
                if open_document_args:
                    try:
                        self.page.evaluate(
                            """args => {
                                if (typeof window.openDocument !== 'function') {
                                    throw new Error('openDocument 함수를 찾지 못했습니다.');
                                }
                                window.openDocument(...args);
                            }""",
                            list(open_document_args),
                        )
                        self.log(
                            f"  openDocument 직접 실행: {open_document_args[1]}"
                        )
                    except Exception as direct_exc:
                        self.log(
                            "  openDocument 직접 실행 실패, 링크 클릭으로 재시도: "
                            f"{safe_error_text(direct_exc)}"
                        )
                        control.click(timeout=15000, force=True)
                else:
                    control.click(timeout=15000, force=True)
                for _ in range(20 if open_document_args else 40):
                    self.page.wait_for_timeout(250)
                    new_pages = [page for page in context.pages if page not in before_pages]
                    if downloads or new_pages:
                        break

                new_pages = [page for page in context.pages if page not in before_pages]
                if open_document_args and not downloads and not new_pages:
                    saved_from_response = self._save_captured_pdf_responses(
                        captured_responses, destination
                    )
                    if not saved_from_response:
                        self.log(
                            "  직접 실행 결과가 없어 원래 문서 링크를 강제 클릭합니다."
                        )
                        control.click(timeout=15000, force=True)
                        for _ in range(40):
                            self.page.wait_for_timeout(250)
                            new_pages = [
                                page
                                for page in context.pages
                                if page not in before_pages
                            ]
                            if downloads or new_pages:
                                break

                new_pages = [page for page in context.pages if page not in before_pages]
                popup = new_pages[-1] if new_pages else None
                if popup is not None:
                    try:
                        popup.wait_for_load_state("domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    popup.wait_for_timeout(700)

                if downloads:
                    self._save_playwright_download(downloads[-1], destination)
                    self.log("  직접 다운로드 이벤트에서 원문을 저장했습니다.")
                elif saved_from_response:
                    pass
                elif popup is not None:
                    self._save_popup_pdf(
                        popup, destination, captured_responses
                    )
                elif self._save_captured_pdf_responses(captured_responses, destination):
                    pass
                else:
                    raise RuntimeError("문서 링크를 눌렀지만 새 창 또는 PDF 응답이 발생하지 않았습니다.")
                self.log(f"  저장: {filename} [KIPRIS 통합행정정보]")
                count += 1
            except Exception as exc:
                safe_error = safe_error_text(exc)
                failures.append((name, safe_error))
                self.log(f"  원문 저장 실패, 다음 서류 계속: {name} / {safe_error}")
            finally:
                context.remove_listener("response", capture_response)
                self.page.remove_listener("download", capture_download)
                if popup is not None:
                    try:
                        popup.close()
                    except Exception:
                        pass
            time.sleep(0.1)

        if failures:
            failure_path = folder / "document_failures.csv"
            write_csv_atomic(failure_path, ["문서명", "실패 사유"], failures)
            self.log(f"  개별 원문 실패 기록: {failure_path.name}")
        if count == 0:
            raise RuntimeError(
                f"원문 후보 {len(document_controls)}개를 찾았지만 모두 저장에 실패했습니다. "
                "document_failures.csv를 확인해 주세요."
            )
        return count

    def download_admin_item(
        self,
        item: NumberItem,
    ) -> tuple[int, str]:
        if not item.is_domestic:
            raise RuntimeError("출원서·심사서류 자동 저장은 국내 문헌만 지원합니다.")
        application_number = self._open_detail(item)
        folder = self.output_dir / application_number
        folder.mkdir(parents=True, exist_ok=True)
        count = self._download_admin_documents(folder)
        return count, application_number


class App:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.root.title("KIPRIS Document Hub 6.13 Legacy Engine")
        self.root.geometry(
            f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0"
        )
        self.root.minsize(1280, 760)
        self.root.configure(fg_color=COLOR_BG)
        self.output_dir = StringVar(value=str(Path.home() / "Downloads" / "KIPRIS"))
        self.api_key = StringVar(value=load_saved_api_key())
        self.want_publication = BooleanVar(value=True)
        self.want_all_admin = BooleanVar(value=True)
        self.open_folder_on_finish = BooleanVar(value=False)
        self.google_fallback = BooleanVar(value=True)
        self.allow_insecure_tls = BooleanVar(value=False)
        self.running = False
        self.cancel_event = threading.Event()
        self.total_items = 0
        self.messages: queue.Queue[str] = queue.Queue(maxsize=2000)
        self.progress_updates: queue.Queue[tuple[int, int]] = queue.Queue()
        self.settings_window = None
        self.settings_api_key = None
        self.pdf_root = Path(self.output_dir.get())
        self.pdf_folders: list[Path] = []
        self.current_pdf_folder: Path | None = None
        self.pdf_files: list[Path] = []
        self.preview_path: Path | None = None
        self.preview_page = 0
        self.preview_page_count = 0
        self.preview_image = None
        self.viewer_photo = None
        self.viewer_background_photo = None
        self.viewer_background_source = None
        self.viewer_empty_message = "왼쪽 파일 목록에서 PDF를 선택하세요."
        self.viewer_zoom = 1.0
        self.viewer_fit_height = True
        self.viewer_render_generation = 0
        self.viewer_resize_job = None
        self.viewer_render_lock = threading.Lock()
        self.status_blink_job = None
        self.status_blink_on = False
        self.fullscreen = False
        self._load_brand_background()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)
        self.root.bind("<F11>", self.toggle_fullscreen)
        self.root.bind("<Escape>", self.exit_fullscreen)
        self.root.after(0, self.maximize_window)
        self.root.after(300, self.refresh_pdf_list)
        self.root.after(150, self._drain_messages)

    def _load_brand_background(self):
        try:
            with Image.open(app_resource_path("assets/rocket_background.png")) as image:
                self.viewer_background_source = image.convert("RGB").copy()
        except Exception:
            self.viewer_background_source = None

    def _build_ui(self):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        main = ctk.CTkFrame(self.root, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=14, pady=10)

        header = ctk.CTkFrame(main, fg_color="transparent")
        header.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            header,
            text="KIPRIS  DOCUMENT  HUB",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COLOR_TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            header,
            text="DESIGNED BY  이강엽 심사관",
            text_color="#9CB6D9",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(side="right", padx=(8, 12))
        ctk.CTkLabel(
            header,
            text="API + 통합행정정보",
            corner_radius=10,
            fg_color="#19335E",
            text_color="#AFCBFF",
            padx=9,
            pady=3,
        ).pack(side="right", padx=(0, 7))

        content = ctk.CTkFrame(main, fg_color="transparent")
        content.pack(fill="both", expand=True)
        content.grid_columnconfigure(0, weight=2, minsize=430)
        content.grid_columnconfigure(1, weight=5, minsize=760)
        content.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(content, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        right = ctk.CTkFrame(content, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        input_card = self._card(left)
        input_card.pack(fill="x", pady=(0, 7))
        self._section_title(
            input_card,
            "문헌 번호",
            "국내·외 공보번호를 여러 개 입력할 수 있습니다.",
        )
        self.number_text = ctk.CTkTextbox(
            input_card,
            height=88,
            corner_radius=8,
            border_width=1,
            border_color=COLOR_BORDER,
            fg_color=COLOR_CARD_ALT,
            text_color=COLOR_TEXT,
            font=ctk.CTkFont(family="Consolas", size=15),
        )
        self.number_text.pack(fill="x", padx=12, pady=(2, 7))
        button_row = ctk.CTkFrame(input_card, fg_color="transparent")
        button_row.pack(fill="x", padx=12, pady=(0, 5))
        load_button = self._secondary_button(
            button_row, "파일 불러오기", self.load_file
        )
        load_button.configure(width=108)
        load_button.pack(side="left")
        clear_button = self._secondary_button(
            button_row, "목록 지우기", lambda: self.number_text.delete("1.0", END)
        )
        clear_button.configure(width=90)
        clear_button.pack(side="left", padx=8)
        ctk.CTkLabel(
            input_card,
            text=(
                "예: 출원 1020230104168 · 공개 1020250012345A · "
                "외국 US20230123456A1\n"
                "복수 입력: 번호마다 줄바꿈 또는 쉼표(,), 세미콜론(;)으로 구분"
            ),
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=12),
            anchor="w",
            justify="left",
            wraplength=410,
        ).pack(fill="x", padx=12, pady=(0, 10))

        option_card = self._card(left)
        option_card.pack(fill="x", pady=(0, 7))
        self._section_title(
            option_card,
            "다운로드 설정",
            "REST AccessKey는 이 카드 우측 상단 설정에서 관리",
        )
        settings_button = self._secondary_button(
            option_card, "설정", self.open_settings
        )
        settings_button.configure(width=68)
        settings_button.place(relx=1.0, x=-12, y=10, anchor="ne")
        option_grid = ctk.CTkFrame(option_card, fg_color="transparent")
        option_grid.pack(fill="x", padx=10, pady=(1, 6))
        option_grid.grid_columnconfigure(0, weight=1)
        option_grid.grid_columnconfigure(1, weight=1)
        for index, (label, variable) in enumerate(
            (
                ("공개·등록공보 PDF", self.want_publication),
                ("통합행정정보 전체", self.want_all_admin),
            )
        ):
            ctk.CTkCheckBox(
                option_grid,
                text=label,
                variable=variable,
                corner_radius=5,
                border_width=2,
                fg_color=COLOR_ACCENT,
                hover_color=COLOR_ACCENT_HOVER,
                text_color=COLOR_TEXT,
                font=ctk.CTkFont(size=13),
            ).grid(row=0, column=index, sticky="w", padx=6, pady=3)
        ctk.CTkCheckBox(
            option_grid,
            text="외국공보 Google Patents 사용",
            variable=self.google_fallback,
            corner_radius=5,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=12),
        ).grid(row=1, column=0, sticky="w", padx=6, pady=3)
        ctk.CTkLabel(
            option_grid,
            text="KIPRIS 브라우저는 항상 숨김으로 실행됩니다.",
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=12),
        ).grid(row=1, column=1, sticky="w", padx=6, pady=3)
        ctk.CTkLabel(
            option_grid,
            text="완료된 PDF는 아래 문헌 폴더·PDF 목록에 자동 표시됩니다.",
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=12),
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=3)

        output_card = self._card(left)
        output_card.pack(fill="x", pady=(0, 7))
        output_row = ctk.CTkFrame(output_card, fg_color="transparent")
        output_row.pack(fill="x", padx=12, pady=9)
        ctk.CTkLabel(
            output_row,
            text="저장 폴더",
            width=62,
            anchor="w",
            font=ctk.CTkFont(weight="bold"),
            text_color=COLOR_TEXT,
        ).pack(side="left")
        ctk.CTkEntry(
            output_row,
            textvariable=self.output_dir,
            height=32,
            corner_radius=8,
            border_color=COLOR_BORDER,
            fg_color=COLOR_CARD_ALT,
        ).pack(side="left", fill="x", expand=True, padx=10)
        self._secondary_button(output_row, "찾아보기", self.choose_output).pack(side="right")

        progress_row = ctk.CTkFrame(left, fg_color="transparent")
        progress_row.pack(fill="x", pady=(0, 6))
        self.progress_bar = ctk.CTkProgressBar(
            progress_row,
            height=12,
            corner_radius=6,
            progress_color=COLOR_ACCENT,
            fg_color="#202B3B",
            mode="determinate",
        )
        self.progress_bar.pack(side="left", fill="x", expand=True, padx=(2, 10))
        self.progress_bar.set(0)
        self.progress_label = ctk.CTkLabel(
            progress_row,
            text="0 / 0  (0%)",
            width=96,
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=11),
        )
        self.progress_label.pack(side="right")

        action_row = ctk.CTkFrame(left, fg_color="transparent")
        action_row.pack(fill="x", pady=(0, 7))
        self.start_button = ctk.CTkButton(
            action_row,
            text="다운로드 시작",
            command=self.start,
            height=40,
            corner_radius=10,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        self.start_button.pack(side="left", fill="x", expand=True)
        self.stop_button = self._secondary_button(
            action_row, "중지", self.request_cancel
        )
        self.stop_button.configure(width=68, height=40, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        self.status_label = ctk.CTkLabel(
            action_row,
            text="준비",
            width=98,
            height=40,
            corner_radius=10,
            fg_color="#202B3B",
            text_color="#AFCBFF",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.status_label.pack(side="right", padx=(10, 0))

        self._build_pdf_file_panel(left)

        log_card = self._card(left)
        log_card.pack(fill="x")
        log_card.configure(height=142)
        log_card.pack_propagate(False)
        self._section_title(log_card, "진행 기록", "실시간 결과")
        self.log_text = ctk.CTkTextbox(
            log_card,
            height=92,
            corner_radius=8,
            border_width=1,
            border_color=COLOR_BORDER,
            fg_color="#0B111A",
            text_color="#B8C8DF",
            font=ctk.CTkFont(family="Consolas", size=11),
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(2, 10))
        self._build_pdf_panel(right)

    def _build_pdf_file_panel(self, parent):
        file_card = self._card(parent)
        file_card.pack(fill="x", pady=(0, 7))
        self._section_title(
            file_card,
            "문헌 폴더와 PDF 파일",
            "선택한 출원번호 또는 공보번호 폴더의 파일만 표시합니다.",
        )

        lists_frame = ctk.CTkFrame(file_card, fg_color="transparent")
        lists_frame.pack(fill="x", padx=12, pady=(3, 7))

        folder_panel = ctk.CTkFrame(
            lists_frame, fg_color="transparent", width=158, height=125
        )
        folder_panel.pack(side="left", fill="both", padx=(0, 7))
        folder_panel.pack_propagate(False)
        self.pdf_folder_count_label = ctk.CTkLabel(
            folder_panel,
            text="문헌 폴더 (0)",
            anchor="w",
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.pdf_folder_count_label.pack(fill="x", pady=(0, 4))
        folder_list_frame = ctk.CTkFrame(folder_panel, fg_color="transparent")
        folder_list_frame.pack(fill="both", expand=True)
        self.pdf_folder_listbox = Listbox(
            folder_list_frame,
            height=5,
            selectmode="browse",
            exportselection=False,
            bg="#0B111A",
            fg="#D6E2F2",
            selectbackground=COLOR_ACCENT,
            selectforeground="#FFFFFF",
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_ACCENT,
            relief="flat",
            activestyle="none",
            font=("Malgun Gothic", 10),
        )
        folder_scroll = ctk.CTkScrollbar(
            folder_list_frame,
            command=self.pdf_folder_listbox.yview,
            button_color="#3B4A61",
            button_hover_color=COLOR_ACCENT,
        )
        self.pdf_folder_listbox.configure(yscrollcommand=folder_scroll.set)
        self.pdf_folder_listbox.pack(side="left", fill="both", expand=True)
        folder_scroll.pack(side="right", fill="y", padx=(5, 0))
        self.pdf_folder_listbox.bind(
            "<<ListboxSelect>>", self.on_pdf_folder_selection
        )

        file_panel = ctk.CTkFrame(lists_frame, fg_color="transparent")
        file_panel.pack(side="left", fill="both", expand=True)
        self.pdf_file_count_label = ctk.CTkLabel(
            file_panel,
            text="PDF 파일 (0)",
            anchor="w",
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.pdf_file_count_label.pack(fill="x", pady=(0, 4))
        list_frame = ctk.CTkFrame(file_panel, fg_color="transparent")
        list_frame.pack(fill="both", expand=True)
        self.pdf_listbox = Listbox(
            list_frame,
            height=5,
            selectmode=EXTENDED,
            exportselection=False,
            bg="#0B111A",
            fg="#D6E2F2",
            selectbackground=COLOR_ACCENT,
            selectforeground="#FFFFFF",
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_ACCENT,
            relief="flat",
            activestyle="none",
            font=("Malgun Gothic", 10),
        )
        list_scroll = ctk.CTkScrollbar(
            list_frame,
            command=self.pdf_listbox.yview,
            button_color="#3B4A61",
            button_hover_color=COLOR_ACCENT,
        )
        self.pdf_listbox.configure(yscrollcommand=list_scroll.set)
        self.pdf_listbox.pack(side="left", fill="both", expand=True)
        list_scroll.pack(side="right", fill="y", padx=(6, 0))
        self.pdf_listbox.bind("<<ListboxSelect>>", self.on_pdf_selection)

        file_actions = ctk.CTkFrame(file_card, fg_color="transparent")
        file_actions.pack(fill="x", padx=12, pady=(0, 11))
        refresh_button = self._secondary_button(
            file_actions, "새로고침", self.refresh_pdf_list
        )
        refresh_button.configure(width=76)
        refresh_button.pack(side="left")
        open_folder_button = self._secondary_button(
            file_actions, "폴더 열기", self.open_current_pdf_folder
        )
        open_folder_button.configure(width=84)
        open_folder_button.pack(side="left", padx=6)
        merge_button = self._secondary_button(
            file_actions, "PDF 합치기", self.open_merge_dialog
        )
        merge_button.configure(width=100)
        merge_button.pack(side="left")
        self.text_button = self._secondary_button(
            file_actions, "텍스트 추출", self.extract_selected_pdf_text
        )
        self.text_button.configure(width=100)
        self.text_button.pack(side="left", padx=(6, 0))

    def _build_pdf_panel(self, parent):
        viewer_card = self._card(parent)
        viewer_card.pack(fill="both", expand=True)
        viewer_toolbar = ctk.CTkFrame(viewer_card, fg_color="transparent")
        viewer_toolbar.pack(fill="x", padx=12, pady=(9, 7))
        self.preview_title_label = ctk.CTkLabel(
            viewer_toolbar,
            text="선택된 PDF 없음",
            text_color="#C9D7EA",
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        )
        self.preview_title_label.pack(side="left", fill="x", expand=True)
        zoom_out = self._secondary_button(
            viewer_toolbar, "−", lambda: self.change_viewer_zoom(0.8)
        )
        zoom_out.configure(width=40)
        zoom_out.pack(side="left", padx=(6, 3))
        zoom_in = self._secondary_button(
            viewer_toolbar, "+", lambda: self.change_viewer_zoom(1.25)
        )
        zoom_in.configure(width=40)
        zoom_in.pack(side="left", padx=3)
        fit_button = self._secondary_button(
            viewer_toolbar, "높이 맞춤", self.fit_viewer_height
        )
        fit_button.configure(width=78)
        fit_button.pack(side="left", padx=(3, 0))

        canvas_frame = ctk.CTkFrame(
            viewer_card,
            fg_color="#080B10",
            corner_radius=8,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        canvas_frame.pack(fill="both", expand=True, padx=12, pady=(0, 7))
        self.viewer_canvas = Canvas(
            canvas_frame,
            bg="#080B10",
            highlightthickness=0,
            bd=0,
        )
        self.viewer_vscroll = ctk.CTkScrollbar(
            canvas_frame,
            orientation="vertical",
            command=self.viewer_canvas.yview,
            button_color="#3B4A61",
            button_hover_color=COLOR_ACCENT,
        )
        self.viewer_hscroll = ctk.CTkScrollbar(
            canvas_frame,
            orientation="horizontal",
            command=self.viewer_canvas.xview,
            button_color="#3B4A61",
            button_hover_color=COLOR_ACCENT,
        )
        self.viewer_canvas.configure(
            yscrollcommand=self.viewer_vscroll.set,
            xscrollcommand=self.viewer_hscroll.set,
        )
        self.viewer_canvas.grid(row=0, column=0, sticky="nsew")
        self.viewer_vscroll.grid(row=0, column=1, sticky="ns")
        self.viewer_hscroll.grid(row=1, column=0, sticky="ew")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)
        self._draw_viewer_placeholder(self.viewer_empty_message)
        self.viewer_canvas.bind("<Configure>", self.on_viewer_resize)
        self.viewer_canvas.bind("<MouseWheel>", self.on_viewer_mousewheel)
        self.viewer_canvas.bind("<Control-MouseWheel>", self.on_viewer_ctrl_mousewheel)

        navigation = ctk.CTkFrame(viewer_card, fg_color="transparent")
        navigation.pack(fill="x", padx=12, pady=(0, 11))
        previous_button = self._secondary_button(
            navigation, "◀ 이전", self.previous_pdf_page
        )
        previous_button.configure(width=80)
        previous_button.pack(side="left")
        self.preview_page_label = ctk.CTkLabel(
            navigation,
            text="0 / 0",
            text_color=COLOR_MUTED,
            width=80,
        )
        self.preview_page_label.pack(side="left", expand=True)
        next_button = self._secondary_button(
            navigation, "다음 ▶", self.next_pdf_page
        )
        next_button.configure(width=80)
        next_button.pack(side="right")

    def maximize_window(self):
        try:
            self.root.state("zoomed")
        except Exception:
            self.root.geometry(
                f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0"
            )

    def toggle_fullscreen(self, _event=None):
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)

    def exit_fullscreen(self, _event=None):
        if self.fullscreen:
            self.fullscreen = False
            self.root.attributes("-fullscreen", False)
            self.maximize_window()

    def _card(self, parent):
        return ctk.CTkFrame(
            parent,
            corner_radius=12,
            fg_color=COLOR_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
        )

    def _section_title(self, parent, title: str, subtitle: str):
        title_row = ctk.CTkFrame(parent, fg_color="transparent")
        title_row.pack(fill="x", padx=12, pady=(9, 2))
        ctk.CTkLabel(
            title_row,
            text=title,
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=COLOR_TEXT,
        ).pack(anchor="w")
        if subtitle:
            ctk.CTkLabel(
                title_row,
                text=subtitle,
                font=ctk.CTkFont(size=12),
                text_color=COLOR_MUTED,
            ).pack(anchor="w")

    def _secondary_button(self, parent, text: str, command):
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            height=30,
            corner_radius=8,
            fg_color="#202B3B",
            hover_color="#2A3A50",
            border_width=1,
            border_color="#34435B",
            text_color=COLOR_TEXT,
        )

    def load_file(self):
        filename = filedialog.askopenfilename(
            title="번호 목록 선택",
            filetypes=[("번호 목록", "*.txt *.csv *.xlsx"), ("모든 파일", "*.*")],
        )
        if not filename:
            return
        try:
            items = load_number_file(Path(filename))
        except Exception as exc:
            messagebox.showerror("불러오기 실패", str(exc))
            return
        lines = [
            f"{item.kind},{item.number}" if item.kind != "auto" else item.number
            for item in items
        ]
        self.number_text.delete("1.0", END)
        self.number_text.insert("1.0", "\n".join(lines))

    def choose_output(self):
        selected = filedialog.askdirectory(title="저장 폴더 선택")
        if selected:
            self.output_dir.set(selected)
            self.refresh_pdf_list(Path(selected))

    def selected_pdf_paths(self) -> list[Path]:
        return [
            self.pdf_files[index]
            for index in self.pdf_listbox.curselection()
            if 0 <= index < len(self.pdf_files)
        ]

    def refresh_pdf_list(
        self,
        root: Path | None = None,
        select_path: Path | None = None,
    ):
        self.pdf_root = Path(root or self.output_dir.get()).expanduser()
        self.pdf_folders = list_pdf_folders(self.pdf_root)
        folder_names: list[str] = []
        for folder in self.pdf_folders:
            try:
                name = str(folder.relative_to(self.pdf_root))
            except ValueError:
                name = folder.name
            folder_names.append(name)

        if not self.pdf_folders:
            self.current_pdf_folder = None
            self.pdf_folder_listbox.delete(0, END)
            self.pdf_folder_listbox.insert(END, "PDF 폴더 없음")
            self.pdf_folder_count_label.configure(text="문헌 폴더 (0)")
            self.pdf_files = []
            self._populate_pdf_file_list()
            self.clear_pdf_viewer("다운로드된 PDF가 없습니다.")
            return

        self.pdf_folder_listbox.delete(0, END)
        for folder_name in folder_names:
            self.pdf_folder_listbox.insert(END, folder_name)
        self.pdf_folder_count_label.configure(
            text=f"문헌 폴더 ({len(folder_names)})"
        )
        target_folder: Path | None = None
        if select_path is not None:
            candidate = Path(select_path).parent
            if candidate in self.pdf_folders:
                target_folder = candidate
        if target_folder is None and self.current_pdf_folder in self.pdf_folders:
            target_folder = self.current_pdf_folder
        if target_folder is None:
            target_folder = self.pdf_folders[0]

        self.current_pdf_folder = target_folder
        selected_index = self.pdf_folders.index(target_folder)
        self.pdf_folder_listbox.selection_clear(0, END)
        self.pdf_folder_listbox.selection_set(selected_index)
        self.pdf_folder_listbox.see(selected_index)
        self._load_current_folder_files(select_path)

    def on_pdf_folder_selection(self, _event=None):
        selection = self.pdf_folder_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        if not 0 <= index < len(self.pdf_folders):
            return
        self.current_pdf_folder = self.pdf_folders[index]
        self._load_current_folder_files()

    def open_current_pdf_folder(self):
        folder = self.current_pdf_folder
        if folder is None or not folder.is_dir():
            messagebox.showwarning("폴더 없음", "열 문헌 폴더를 먼저 선택해 주세요.")
            return
        try:
            resolved_root = self.pdf_root.resolve()
            resolved_folder = folder.resolve()
            if (
                resolved_folder != resolved_root
                and resolved_root not in resolved_folder.parents
            ):
                raise RuntimeError("저장 폴더 밖의 경로는 열 수 없습니다.")
            if os.name != "nt":
                raise RuntimeError("폴더 열기는 Windows에서 지원됩니다.")
            os.startfile(str(resolved_folder))
        except Exception as exc:
            messagebox.showerror("폴더 열기 실패", safe_error_text(exc))

    def _load_current_folder_files(self, select_path: Path | None = None):
        if self.current_pdf_folder is None:
            self.pdf_files = []
        else:
            self.pdf_files = list_folder_pdf_files(self.current_pdf_folder)
        self._populate_pdf_file_list()

        if not self.pdf_files:
            self.clear_pdf_viewer("이 문헌 폴더에는 PDF가 없습니다.")
            return

        selected_index = 0
        if select_path is not None:
            resolved_target = Path(select_path).resolve()
            for index, pdf_path in enumerate(self.pdf_files):
                if pdf_path.resolve() == resolved_target:
                    selected_index = index
                    break
        self.pdf_listbox.selection_clear(0, END)
        self.pdf_listbox.selection_set(selected_index)
        self.pdf_listbox.see(selected_index)
        self.load_pdf_viewer(self.pdf_files[selected_index], 0)

    def _populate_pdf_file_list(self):
        self.pdf_listbox.delete(0, END)
        for pdf_path in self.pdf_files:
            self.pdf_listbox.insert(END, pdf_path.name)
        self.pdf_file_count_label.configure(text=f"PDF 파일 ({len(self.pdf_files)})")

    def on_pdf_selection(self, _event=None):
        selected = self.selected_pdf_paths()
        if selected:
            self.load_pdf_viewer(selected[-1], 0)

    def load_pdf_viewer(self, pdf_path: Path, page_number: int):
        self.preview_path = pdf_path
        self.preview_page = max(page_number, 0)
        self.viewer_zoom = 1.0
        self.viewer_fit_height = True
        self.preview_title_label.configure(text=pdf_path.name)
        self.render_current_pdf_page()

    def _draw_viewer_background(self):
        if self.viewer_background_source is None:
            return
        width = max(self.viewer_canvas.winfo_width(), 320)
        height = max(self.viewer_canvas.winfo_height(), 320)
        background = ImageOps.fit(
            self.viewer_background_source,
            (width, height),
            method=Image.Resampling.LANCZOS,
            centering=(0.62, 0.5),
        )
        self.viewer_background_photo = ImageTk.PhotoImage(background)
        self.viewer_canvas.create_image(
            0,
            0,
            image=self.viewer_background_photo,
            anchor="nw",
            tags="viewer_background",
        )

    def _draw_viewer_placeholder(self, message: str):
        self.viewer_canvas.delete("all")
        self._draw_viewer_background()
        self.viewer_canvas.create_text(
            30,
            30,
            anchor="nw",
            width=max(self.viewer_canvas.winfo_width() - 60, 200),
            fill="#C4D4EA",
            text=message,
            tags="viewer_message",
            font=("Malgun Gothic", 12, "bold"),
        )

    def render_current_pdf_page(self):
        if self.preview_path is None:
            return
        self.viewer_render_generation += 1
        generation = self.viewer_render_generation
        pdf_path = self.preview_path
        page_number = self.preview_page
        width = max(self.viewer_canvas.winfo_width() - 34, 320)
        height = max(self.viewer_canvas.winfo_height() - 34, 320)
        zoom = self.viewer_zoom
        fit_height = self.viewer_fit_height
        self.viewer_canvas.delete("all")
        self._draw_viewer_background()
        self.viewer_canvas.create_text(
            width // 2,
            40,
            anchor="n",
            fill=COLOR_MUTED,
            text="PDF 페이지를 불러오는 중입니다…",
            font=("Malgun Gothic", 11),
        )

        def worker():
            with self.viewer_render_lock:
                if generation != self.viewer_render_generation:
                    return
                try:
                    result = render_pdf_page(
                        pdf_path,
                        page_number,
                        max_width=width,
                        max_height=height,
                        zoom_factor=zoom,
                        fit_height=fit_height,
                    )
                    self.root.after(
                        0,
                        lambda: self._apply_viewer_render(
                            generation, pdf_path, result, None
                        ),
                    )
                except Exception as exc:
                    self.root.after(
                        0,
                        lambda: self._apply_viewer_render(
                            generation, pdf_path, None, str(exc)
                        ),
                    )

        threading.Thread(target=worker, daemon=True).start()

    def _apply_viewer_render(self, generation, pdf_path, result, error):
        if generation != self.viewer_render_generation or pdf_path != self.preview_path:
            return
        self.viewer_canvas.delete("all")
        self._draw_viewer_background()
        if error or result is None:
            self.preview_page_count = 0
            self.preview_page_label.configure(text="0 / 0")
            self.viewer_canvas.create_text(
                30,
                30,
                anchor="nw",
                width=max(self.viewer_canvas.winfo_width() - 60, 200),
                fill="#FFB8BF",
                text=f"PDF를 표시하지 못했습니다.\n{error}",
                font=("Malgun Gothic", 11),
            )
            return
        image, page_count, actual_page = result
        self.preview_page = actual_page
        self.preview_page_count = page_count
        self.viewer_photo = ImageTk.PhotoImage(image)
        canvas_width = max(self.viewer_canvas.winfo_width(), 1)
        canvas_height = max(self.viewer_canvas.winfo_height(), 1)
        x = max(canvas_width // 2, image.width // 2 + 12)
        y = max(12, (canvas_height - image.height) // 2)
        self.viewer_canvas.create_image(
            x,
            y,
            image=self.viewer_photo,
            anchor="n",
            tags="pdf_page",
        )
        self.viewer_canvas.configure(
            scrollregion=(
                0,
                0,
                max(canvas_width, image.width + 24),
                max(canvas_height, image.height + 24),
            )
        )
        self.viewer_canvas.xview_moveto(0)
        self.viewer_canvas.yview_moveto(0)
        zoom_percent = round(self.viewer_zoom * 100)
        self.preview_page_label.configure(
            text=f"{actual_page + 1} / {page_count}   ·   {zoom_percent}%"
        )

    def clear_pdf_viewer(self, message: str):
        self.preview_path = None
        self.preview_page = 0
        self.preview_page_count = 0
        self.viewer_photo = None
        self.viewer_render_generation += 1
        self.viewer_empty_message = message
        self.preview_title_label.configure(text="선택된 PDF 없음")
        self.preview_page_label.configure(text="0 / 0")
        self._draw_viewer_placeholder(message)

    def on_viewer_resize(self, _event=None):
        if self.viewer_resize_job is not None:
            self.root.after_cancel(self.viewer_resize_job)
        self.viewer_resize_job = self.root.after(250, self._refresh_viewer_after_resize)

    def _refresh_viewer_after_resize(self):
        self.viewer_resize_job = None
        if self.preview_path is None:
            self._draw_viewer_placeholder(self.viewer_empty_message)
        elif self.viewer_fit_height:
            self.render_current_pdf_page()

    def on_viewer_mousewheel(self, event):
        self.viewer_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def on_viewer_ctrl_mousewheel(self, event):
        self.change_viewer_zoom(1.15 if event.delta > 0 else 1 / 1.15)
        return "break"

    def change_viewer_zoom(self, factor: float):
        if self.preview_path is None:
            return
        self.viewer_fit_height = True
        self.viewer_zoom = min(max(self.viewer_zoom * factor, 0.4), 4.0)
        self.render_current_pdf_page()

    def fit_viewer_height(self):
        if self.preview_path is None:
            return
        self.viewer_fit_height = True
        self.viewer_zoom = 1.0
        self.render_current_pdf_page()

    def previous_pdf_page(self):
        if self.preview_path is not None and self.preview_page > 0:
            self.preview_page -= 1
            self.render_current_pdf_page()

    def next_pdf_page(self):
        if (
            self.preview_path is not None
            and self.preview_page + 1 < self.preview_page_count
        ):
            self.preview_page += 1
            self.render_current_pdf_page()

    def open_merge_dialog(self):
        initial_folder = self.current_pdf_folder or self.pdf_root
        window = ctk.CTkToplevel(self.root)
        window.title("PDF 합치기 · 순서 지정")
        window.geometry("760x610")
        window.minsize(680, 520)
        window.configure(fg_color=COLOR_BG)
        window.transient(self.root)
        window.grab_set()

        selected_files: list[Path] = []
        selected_on_main = self.selected_pdf_paths()
        if len(selected_on_main) >= 2:
            selected_files.extend(selected_on_main)

        card = self._card(window)
        card.pack(fill="both", expand=True, padx=14, pady=14)
        self._section_title(
            card,
            "병합할 PDF와 순서",
            "파일 선택 후 위·아래 버튼으로 최종 PDF 페이지 순서를 정합니다.",
        )
        merge_list = Listbox(
            card,
            selectmode="browse",
            exportselection=False,
            bg="#0B111A",
            fg="#D6E2F2",
            selectbackground=COLOR_ACCENT,
            selectforeground="#FFFFFF",
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            relief="flat",
            font=("Malgun Gothic", 10),
        )
        merge_list.pack(fill="both", expand=True, padx=12, pady=(5, 8))

        def refresh_merge_list():
            merge_list.delete(0, END)
            for index, path in enumerate(selected_files, 1):
                merge_list.insert(END, f"{index:02}. {path.name}")

        def choose_files():
            names = filedialog.askopenfilenames(
                parent=window,
                title="병합할 PDF 선택",
                initialdir=str(initial_folder),
                filetypes=[("PDF 파일", "*.pdf")],
            )
            for name in names:
                path = Path(name)
                if path not in selected_files:
                    selected_files.append(path)
            refresh_merge_list()

        def move_selected(direction: int):
            indices = list(merge_list.curselection())
            if not indices:
                return
            index = indices[0]
            target = index + direction
            if not 0 <= target < len(selected_files):
                return
            selected_files[index], selected_files[target] = (
                selected_files[target],
                selected_files[index],
            )
            refresh_merge_list()
            merge_list.selection_set(target)

        def remove_selected():
            for index in reversed(merge_list.curselection()):
                selected_files.pop(index)
            refresh_merge_list()

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill="x", padx=12, pady=(0, 10))
        self._secondary_button(buttons, "파일 선택", choose_files).pack(side="left")
        self._secondary_button(buttons, "▲ 위로", lambda: move_selected(-1)).pack(
            side="left", padx=(7, 0)
        )
        self._secondary_button(buttons, "▼ 아래로", lambda: move_selected(1)).pack(
            side="left", padx=(7, 0)
        )
        self._secondary_button(buttons, "선택 제거", remove_selected).pack(
            side="left", padx=(7, 0)
        )

        output_row = ctk.CTkFrame(card, fg_color="transparent")
        output_row.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(output_row, text="생성 파일명", text_color=COLOR_TEXT).pack(side="left")
        default_stem = (self.current_pdf_folder or self.pdf_root).name or "병합문서"
        output_name = StringVar(value=f"{default_stem}_병합문서.pdf")
        output_entry = ctk.CTkEntry(
            output_row,
            textvariable=output_name,
            height=34,
            corner_radius=8,
            border_color=COLOR_BORDER,
            fg_color=COLOR_CARD_ALT,
        )
        output_entry.pack(side="left", fill="x", expand=True, padx=10)
        create_button = self._secondary_button(output_row, "병합 생성", lambda: None)
        create_button.configure(width=96)
        create_button.pack(side="right")

        def start_merge():
            if len(selected_files) < 2:
                messagebox.showwarning("PDF 선택", "병합할 PDF를 두 개 이상 선택해 주세요.", parent=window)
                return
            raw_name = output_name.get().strip()
            if not raw_name:
                messagebox.showwarning("파일명", "생성 파일명을 입력해 주세요.", parent=window)
                return
            if not raw_name.lower().endswith(".pdf"):
                raw_name += ".pdf"
            filename = safe_filename(raw_name, limit=120)
            destination = initial_folder / filename
            if destination in selected_files:
                messagebox.showwarning("파일명", "원본과 다른 생성 파일명을 입력해 주세요.", parent=window)
                return
            create_button.configure(state="disabled", text="병합 중…")

            def worker():
                try:
                    page_count = merge_pdfs(list(selected_files), destination)
                    self.root.after(0, lambda: finish_merge(page_count, destination, None))
                except Exception as exc:
                    self.root.after(0, lambda: finish_merge(0, destination, str(exc)))

            threading.Thread(target=worker, daemon=True).start()

        def finish_merge(page_count: int, destination: Path, error: str | None):
            if error:
                create_button.configure(state="normal", text="병합 생성")
                messagebox.showerror("PDF 병합 실패", error, parent=window)
                return
            self.log(f"PDF 병합 완료: {destination.name} ({page_count}페이지)")
            window.destroy()
            self.refresh_pdf_list(self.pdf_root, destination)
            messagebox.showinfo(
                "PDF 병합 완료",
                f"{len(selected_files)}개 PDF를 {page_count}페이지로 병합했습니다.",
                parent=self.root,
            )

        create_button.configure(command=start_merge)
        refresh_merge_list()

    def extract_selected_pdf_text(self):
        selected = self.selected_pdf_paths()
        if not selected:
            messagebox.showwarning(
                "PDF 선택",
                "텍스트를 추출할 PDF를 하나 이상 선택해 주세요.",
            )
            return
        self.status_label.configure(
            text="텍스트 추출 중", fg_color="#19335E", text_color="#AFCBFF"
        )
        self.text_button.configure(state="disabled")

        def worker():
            try:
                extracted_text = extract_pdf_text(selected)
                self.root.after(
                    0,
                    lambda: self._finish_text_extraction(extracted_text, len(selected), None),
                )
            except Exception as exc:
                self.root.after(
                    0,
                    lambda: self._finish_text_extraction("", len(selected), str(exc)),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _finish_text_extraction(self, text: str, file_count: int, error: str | None):
        self.status_label.configure(text="준비", fg_color="#202B3B", text_color="#AFCBFF")
        self.text_button.configure(state="normal")
        if error:
            messagebox.showerror("텍스트 추출 실패", error)
            return
        self.show_extracted_text(text, file_count)

    def show_extracted_text(self, extracted_text: str, file_count: int):
        window = ctk.CTkToplevel(self.root)
        window.title(f"PDF 텍스트 추출 - {file_count}개 파일")
        window.geometry("820x650")
        window.minsize(650, 480)
        window.configure(fg_color=COLOR_BG)
        window.transient(self.root)

        text_box = ctk.CTkTextbox(
            window,
            corner_radius=9,
            border_width=1,
            border_color=COLOR_BORDER,
            fg_color="#0B111A",
            text_color=COLOR_TEXT,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
        )
        text_box.pack(fill="both", expand=True, padx=12, pady=(12, 8))
        text_box.insert("1.0", extracted_text)

        actions = ctk.CTkFrame(window, fg_color="transparent")
        actions.pack(fill="x", padx=12, pady=(0, 12))

        def copy_all():
            self.root.clipboard_clear()
            self.root.clipboard_append(text_box.get("1.0", END).rstrip())
            self.root.update()
            messagebox.showinfo("복사 완료", "추출한 텍스트를 클립보드에 복사했습니다.")

        def save_text():
            destination = filedialog.asksaveasfilename(
                title="추출 텍스트 저장",
                initialdir=str(self.pdf_root),
                initialfile="PDF_추출텍스트.txt",
                defaultextension=".txt",
                filetypes=[("텍스트 파일", "*.txt")],
            )
            if destination:
                Path(destination).write_text(
                    text_box.get("1.0", END).rstrip(),
                    encoding="utf-8-sig",
                )

        copy_button = self._secondary_button(actions, "전체 복사", copy_all)
        copy_button.configure(width=100)
        copy_button.pack(side="left")
        save_button = self._secondary_button(actions, "TXT 저장", save_text)
        save_button.configure(width=100)
        save_button.pack(side="left", padx=7)
        close_button = self._secondary_button(actions, "닫기", window.destroy)
        close_button.configure(width=80)
        close_button.pack(side="right")

    def close_app(self):
        self.cancel_event.set()
        self.root.destroy()

    def request_cancel(self):
        if not self.running:
            return
        self.cancel_event.set()
        self.stop_button.configure(state="disabled")
        self.status_label.configure(
            text="중지 요청", fg_color="#775616", text_color="#FFF0BD"
        )
        self.log("사용자가 중지를 요청했습니다. 현재 문서 처리가 끝나면 중지합니다.")

    def open_settings(self):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.focus_force()
            return

        window = ctk.CTkToplevel(self.root)
        self.settings_window = window
        window.title("KIPRIS 설정")
        window.geometry("480x330")
        window.resizable(False, False)
        window.configure(fg_color=COLOR_BG)
        window.transient(self.root)
        window.grab_set()
        window.protocol("WM_DELETE_WINDOW", window.destroy)

        card = self._card(window)
        card.pack(fill="both", expand=True, padx=12, pady=12)
        self._section_title(
            card,
            "REST AccessKey",
            "KIPRISPlus 키를 Windows 자격 증명 저장소에 저장합니다.",
        )
        self.settings_api_key = StringVar(value=self.api_key.get())
        entry = ctk.CTkEntry(
            card,
            textvariable=self.settings_api_key,
            show="●",
            height=36,
            corner_radius=8,
            border_color=COLOR_BORDER,
            fg_color=COLOR_CARD_ALT,
        )
        entry.pack(fill="x", padx=12, pady=(7, 12))
        entry.focus_set()

        ctk.CTkCheckBox(
            card,
            text="회사 보안 프록시의 HTTPS 인증서 오류 임시 허용",
            variable=self.allow_insecure_tls,
            corner_radius=5,
            border_width=2,
            fg_color="#B26A21",
            hover_color="#8E5217",
            text_color=COLOR_TEXT,
        ).pack(anchor="w", padx=12, pady=(0, 5))
        ctk.CTkLabel(
            card,
            text="기본값은 안전한 인증서 검증입니다. KIPRIS 접속이 인증서 오류로\n"
            "실패하고 사내 보안 담당자의 안내가 있을 때만 활성화하세요.",
            justify="left",
            anchor="w",
            text_color="#E2B77A",
            font=ctk.CTkFont(size=10),
        ).pack(fill="x", padx=12, pady=(0, 10))

        button_row = ctk.CTkFrame(card, fg_color="transparent")
        button_row.pack(fill="x", padx=12, pady=(0, 12))
        save_button = self._secondary_button(
            button_row, "키 저장", self.save_settings_api_key
        )
        save_button.configure(width=82)
        save_button.pack(side="left")
        delete_button = self._secondary_button(
            button_row, "저장된 키 삭제", self.clear_saved_api_key
        )
        delete_button.configure(width=118)
        delete_button.pack(side="left", padx=7)
        close_button = self._secondary_button(button_row, "닫기", window.destroy)
        close_button.configure(width=64)
        close_button.pack(side="right")

    def save_settings_api_key(self):
        if self.settings_api_key is None:
            return
        api_key = self.settings_api_key.get().strip()
        if not api_key:
            messagebox.showwarning("키 없음", "REST AccessKey를 입력해 주세요.")
            return
        try:
            save_api_key(api_key)
        except Exception as exc:
            messagebox.showerror("저장 실패", f"REST AccessKey를 저장하지 못했습니다.\n{exc}")
            return
        self.api_key.set(api_key)
        messagebox.showinfo("저장 완료", "REST AccessKey를 안전하게 저장했습니다.")

    def clear_saved_api_key(self):
        try:
            delete_saved_api_key()
        except Exception as exc:
            messagebox.showerror("삭제 실패", f"저장된 API Key를 삭제하지 못했습니다.\n{exc}")
            return
        self.api_key.set("")
        if self.settings_api_key is not None:
            self.settings_api_key.set("")
        messagebox.showinfo("삭제 완료", "Windows에 저장된 API Key를 삭제했습니다.")

    def log(self, message: str):
        try:
            self.messages.put_nowait(message)
        except queue.Full:
            try:
                self.messages.get_nowait()
            except queue.Empty:
                pass
            self.messages.put_nowait(message)

    def _drain_messages(self):
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert(END, message + "\n")
            self.log_text.see(END)
            self.log_text.configure(state="disabled")
        while True:
            try:
                completed, total = self.progress_updates.get_nowait()
            except queue.Empty:
                break
            self._update_progress_display(completed, total)
        self.root.after(150, self._drain_messages)

    def _update_progress_display(self, completed: int, total: int):
        ratio = min(max(completed / total, 0.0), 1.0) if total else 0.0
        if not self.running:
            self.progress_bar.set(ratio)
        self.progress_label.configure(
            text=f"{completed} / {total}  ({round(ratio * 100)}%)"
        )

    def _start_running_indicators(self):
        self.progress_bar.stop()
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.set(0)
        self.progress_bar.start()
        self.status_blink_on = False
        self._blink_running_status()

    def _blink_running_status(self):
        if not self.running or self.cancel_event.is_set():
            self.status_blink_job = None
            return
        self.status_blink_on = not self.status_blink_on
        self.status_label.configure(
            text="● 진행 중" if self.status_blink_on else "진행 중",
            fg_color="#245AA3" if self.status_blink_on else "#19335E",
            text_color="#FFFFFF" if self.status_blink_on else "#AFCBFF",
        )
        self.status_blink_job = self.root.after(500, self._blink_running_status)

    def _stop_running_indicators(self):
        if self.status_blink_job is not None:
            try:
                self.root.after_cancel(self.status_blink_job)
            except Exception:
                pass
            self.status_blink_job = None
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")

    def _finish_run(
        self,
        success: int,
        files: int,
        failure_count: int,
        output_dir: Path,
        _open_folder: bool,
        completed_items: int,
        cancelled: bool,
    ):
        self.running = False
        self._stop_running_indicators()
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        while True:
            try:
                self.progress_updates.get_nowait()
            except queue.Empty:
                break
        self._update_progress_display(
            completed_items if cancelled else self.total_items,
            self.total_items,
        )
        self.current_pdf_folder = None
        self.refresh_pdf_list(output_dir)

        summary = (
            f"{'사용자 요청으로 처리를 중지했습니다.' if cancelled else '처리가 완료되었습니다.'}\n\n"
            f"성공 문헌: {success}건\n"
            f"저장 파일: {files}개\n"
            f"미저장 문헌: {failure_count}건\n\n"
            f"저장 위치: {output_dir}\n\n"
            "왼쪽 문헌 폴더·PDF 목록에서 파일을 선택해 확인할 수 있습니다."
        )
        if cancelled:
            self.status_label.configure(
                text="중지됨", fg_color="#775616", text_color="#FFF0BD"
            )
            dialog = messagebox.showwarning
            dialog_title = "다운로드 중지"
        elif files > 0 and failure_count == 0:
            self.status_label.configure(
                text="다운로드 완료", fg_color="#176B45", text_color="#D8FFEA"
            )
            dialog = messagebox.showinfo
            dialog_title = "다운로드 정상 완료"
        elif files > 0:
            self.status_label.configure(
                text="일부 실패", fg_color="#775616", text_color="#FFF0BD"
            )
            dialog = messagebox.showwarning
            dialog_title = "다운로드 일부 완료"
        else:
            self.status_label.configure(
                text="다운로드 실패", fg_color="#7A2830", text_color="#FFD9DD"
            )
            dialog = messagebox.showerror
            dialog_title = "다운로드 실패"

        self.root.update_idletasks()
        dialog(dialog_title, summary)

    def start(self):
        if self.running:
            return
        items = parse_text_items(self.number_text.get("1.0", END))
        if not items:
            messagebox.showwarning(
                "번호 없음",
                "국내 출원번호·공개공보 번호 또는 외국공보 번호를 입력해 주세요.",
            )
            return
        settings = (
            self.want_publication.get(),
            self.want_all_admin.get(),
            self.google_fallback.get(),
            self.open_folder_on_finish.get(),
            self.allow_insecure_tls.get(),
            self.api_key.get().strip(),
        )
        if not any(settings[:2]):
            messagebox.showwarning("서류 선택 없음", "다운로드할 서류를 하나 이상 선택해 주세요.")
            return
        if settings[-1]:
            try:
                save_api_key(settings[-1])
            except Exception as exc:
                messagebox.showwarning(
                    "API Key 저장 실패",
                    f"다운로드는 계속할 수 있지만 다음 실행 때 키를 다시 입력해야 합니다.\n{exc}",
                )
        output_dir = Path(self.output_dir.get()).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        self.running = True
        self.cancel_event.clear()
        self.total_items = len(items)
        self._update_progress_display(0, len(items))
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._start_running_indicators()
        threading.Thread(
            target=self._run,
            args=(items, output_dir, settings),
            daemon=True,
        ).start()

    def _run(
        self,
        items: list[NumberItem],
        output_dir: Path,
        settings: tuple[bool, bool, bool, bool, bool, str],
    ):
        (
            want_publication,
            want_all_admin,
            google_fallback,
            open_folder_on_finish,
            allow_insecure_tls,
            api_key,
        ) = settings
        success = 0
        files = 0
        failures: list[tuple[str, str]] = []
        admin_downloader = None
        completed_items = 0
        try:
            self.log(f"총 {len(items)}건을 시작합니다.")
            if want_publication and not api_key:
                self.log("API Key가 없습니다. 국가코드가 포함된 외국공보만 Google Patents로 시도합니다.")
            api_client = KiprisApiClient(
                api_key,
                google_fallback,
                self.log,
                allow_insecure_tls=allow_insecure_tls,
                cancel_event=self.cancel_event,
            )
            if want_all_admin and any(item.is_domestic for item in items):
                self.log(
                    "행정서류는 KIPRISPlus API를 먼저 사용하고, "
                    "API 미지원 서류만 KIPRIS 화면으로 보완합니다."
                )

            for index, item in enumerate(items, 1):
                if self.cancel_event.is_set():
                    self.log("중지 요청을 확인하여 남은 문헌 처리를 종료합니다.")
                    break
                self.log(f"[{index}/{len(items)}] {item.number}")
                count = 0
                item_errors: list[str] = []
                if want_publication:
                    try:
                        result = api_client.download_publication(item, output_dir)
                        count += 1
                        self.log(f"  저장: {result.destination.name} [{result.source}]")
                    except Exception as exc:
                        safe_error = safe_error_text(exc)
                        item_errors.append(f"공개·등록공보: {safe_error}")
                        self.log(f"  공개·등록공보 실패: {safe_error}")

                if want_all_admin:
                    if not item.is_domestic:
                        self.log("  외국 문헌의 출원서·심사서류는 지원하지 않아 건너뜁니다.")
                    else:
                        admin_count = 0
                        api_unsupported = 1
                        api_failures: list[tuple[str, str]] = []
                        api_error: Exception | None = None
                        if api_key:
                            try:
                                (
                                    api_count,
                                    _,
                                    api_unsupported,
                                    api_failures,
                                ) = api_client.download_admin_documents_api(
                                    item, output_dir
                                )
                                admin_count += api_count
                                self.log(
                                    f"  행정서류 API 저장 {api_count}개, "
                                    f"브라우저 보완 대상 {api_unsupported + len(api_failures)}개"
                                )
                            except Exception as exc:
                                api_error = exc
                                self.log(
                                    "  행정서류 목록/PDF API 실패, 브라우저로 보완: "
                                    f"{safe_error_text(exc)}"
                                )
                        else:
                            self.log("  API Key가 없어 행정서류를 KIPRIS 화면으로 처리합니다.")

                        need_browser = (
                            not api_key
                            or api_error is not None
                            or api_unsupported > 0
                            or bool(api_failures)
                        )
                        browser_count = 0
                        browser_error: Exception | None = None
                        if need_browser and admin_downloader is None:
                            self.log(
                                "KIPRIS 통합행정정보 브라우저를 시작합니다. "
                                "실행 방식: 숨김"
                            )
                            try:
                                admin_downloader = KiprisAdminDownloader(
                                    output_dir,
                                    self.log,
                                    headless=True,
                                    allow_insecure_tls=allow_insecure_tls,
                                    cancel_event=self.cancel_event,
                                )
                                admin_downloader.__enter__()
                            except Exception as exc:
                                browser_error = exc
                                self.log(
                                    "  KIPRIS 브라우저 시작 실패: "
                                    f"{safe_error_text(exc)}"
                                )

                        if need_browser and admin_downloader is not None and browser_error is None:
                            try:
                                browser_count, _ = admin_downloader.download_admin_item(item)
                            except Exception as exc:
                                browser_error = exc
                                self.log(
                                    "  숨김 모드 행정서류 처리 실패: "
                                    f"{safe_error_text(exc)}"
                                )

                        if (
                            need_browser
                            and browser_count == 0
                            and not self.cancel_event.is_set()
                        ):
                            self.log(
                                "  원문 저장이 없어 Chrome 숨김 방식으로 "
                                "자동 재시도합니다."
                            )
                            if admin_downloader is not None:
                                try:
                                    admin_downloader.__exit__(None, None, None)
                                except Exception:
                                    pass
                            admin_downloader = KiprisAdminDownloader(
                                output_dir,
                                self.log,
                                headless=True,
                                allow_insecure_tls=allow_insecure_tls,
                                cancel_event=self.cancel_event,
                            )
                            try:
                                admin_downloader.__enter__()
                                browser_count, _ = admin_downloader.download_admin_item(item)
                                browser_error = None
                            except Exception as exc:
                                browser_error = exc
                                self.log(
                                    "  숨김 재시도 행정서류 처리 실패: "
                                    f"{safe_error_text(exc)}"
                                )

                        admin_count += browser_count
                        count += admin_count
                        if need_browser and browser_count == 0 and browser_error is not None:
                            item_errors.append(
                                "행정서류 일부 또는 전체: "
                                f"{safe_error_text(browser_error)}"
                            )

                files += count
                if count:
                    success += 1
                    self.log(f"  완료: {count}개 파일")
                if item_errors:
                    failures.append((item.number, " / ".join(item_errors)))
                elif not count:
                    failures.append((item.number, "저장 가능한 문서가 없습니다."))
                self.progress_updates.put((index, len(items)))
                completed_items = index
                time.sleep(DEFAULT_DELAY_SECONDS)
        except Exception as exc:
            safe_error = safe_error_text(exc)
            failures.append(("프로그램", safe_error))
            self.log(f"중단: {safe_error}")
        finally:
            if admin_downloader is not None:
                try:
                    admin_downloader.__exit__(None, None, None)
                except Exception as exc:
                    self.log(f"브라우저 종료 경고: {safe_error_text(exc)}")
            report_path = output_dir / "download_report.csv"
            write_csv_atomic(
                report_path,
                ["번호", "실패 또는 미저장 사유"],
                failures,
            )
            self.log(f"종료: 성공 {success}건, 저장 {files}개, 미저장 {len(failures)}건")
            self.log(f"저장 위치: {output_dir}")
            failure_count = len(failures)
            self.root.after(
                0,
                lambda: self._finish_run(
                    success,
                    files,
                    failure_count,
                    output_dir,
                    open_folder_on_finish,
                    completed_items,
                    self.cancel_event.is_set(),
                ),
            )


def main():
    global ctk, Image, ImageOps, ImageTk
    global extract_pdf_text, list_folder_pdf_files, list_pdf_folders
    global merge_pdfs, render_pdf_page
    global BooleanVar, Canvas, END, EXTENDED, Listbox, StringVar
    global filedialog, messagebox
    import customtkinter as desktop_ctk
    from tkinter import (
        BooleanVar as DesktopBooleanVar,
        Canvas as DesktopCanvas,
        END as DESKTOP_END,
        EXTENDED as DESKTOP_EXTENDED,
        Listbox as DesktopListbox,
        StringVar as DesktopStringVar,
        filedialog as desktop_filedialog,
        messagebox as desktop_messagebox,
    )
    from PIL import Image as DesktopImage
    from PIL import ImageOps as DesktopImageOps
    from PIL import ImageTk as DesktopImageTk
    from pdf_tools import (
        extract_pdf_text as desktop_extract_pdf_text,
        list_folder_pdf_files as desktop_list_folder_pdf_files,
        list_pdf_folders as desktop_list_pdf_folders,
        merge_pdfs as desktop_merge_pdfs,
        render_pdf_page as desktop_render_pdf_page,
    )

    ctk = desktop_ctk
    BooleanVar = DesktopBooleanVar
    Canvas = DesktopCanvas
    END = DESKTOP_END
    EXTENDED = DESKTOP_EXTENDED
    Listbox = DesktopListbox
    StringVar = DesktopStringVar
    filedialog = desktop_filedialog
    messagebox = desktop_messagebox
    Image = DesktopImage
    ImageOps = DesktopImageOps
    ImageTk = DesktopImageTk
    extract_pdf_text = desktop_extract_pdf_text
    list_folder_pdf_files = desktop_list_folder_pdf_files
    list_pdf_folders = desktop_list_pdf_folders
    merge_pdfs = desktop_merge_pdfs
    render_pdf_page = desktop_render_pdf_page
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
