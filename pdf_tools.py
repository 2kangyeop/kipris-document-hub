from __future__ import annotations

import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

import pymupdf


MAX_PDF_BYTES = 500 * 1024 * 1024
MAX_PDF_PAGES = 5000
MAX_MERGE_PAGES = 10000
MAX_EXTRACT_CHARACTERS = 10_000_000
MAX_RENDER_PIXELS = 16_000_000
MAX_ARCHIVE_MEMBERS = 5000
MAX_ARCHIVE_MEMBER_BYTES = 200 * 1024 * 1024


def _natural_name_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def _insert_payload_document(
    destination: pymupdf.Document,
    *,
    filename: str = "",
    path: Path | None = None,
    data: bytes | None = None,
) -> int:
    source = None
    converted = None
    try:
        if path is not None:
            source = pymupdf.open(path)
        else:
            source = pymupdf.open(stream=data or b"", filetype=Path(filename).suffix.lstrip("."))
        if source.is_pdf:
            destination.insert_pdf(source)
            return source.page_count
        converted_bytes = source.convert_to_pdf()
        converted = pymupdf.open(stream=converted_bytes, filetype="pdf")
        destination.insert_pdf(converted)
        return converted.page_count
    finally:
        if converted is not None:
            converted.close()
        if source is not None:
            source.close()


def convert_document_payload_to_pdf(source_path: Path, destination: Path) -> tuple[str, int]:
    """Convert official PDF, ZIP, TIFF or image payloads into one validated PDF."""
    source_path = Path(source_path)
    destination = Path(destination)
    payload_size = source_path.stat().st_size
    if payload_size <= 0 or payload_size > MAX_PDF_BYTES:
        raise RuntimeError("원문 응답 크기가 허용 범위를 벗어났습니다.")

    merged = pymupdf.open()
    payload_type = "문서"
    try:
        if zipfile.is_zipfile(source_path):
            payload_type = "ZIP 이미지 원문"
            with zipfile.ZipFile(source_path) as archive:
                members = [item for item in archive.infolist() if not item.is_dir()]
                if not members or len(members) > MAX_ARCHIVE_MEMBERS:
                    raise RuntimeError("ZIP 원문의 파일 개수가 안전 제한을 벗어났습니다.")
                if sum(item.file_size for item in members) > MAX_PDF_BYTES:
                    raise RuntimeError("ZIP 원문의 해제 용량이 500MB를 초과했습니다.")
                members.sort(key=lambda item: _natural_name_key(item.filename))
                inserted = 0
                for member in members:
                    if member.flag_bits & 0x1:
                        raise RuntimeError("암호화된 ZIP 원문은 처리할 수 없습니다.")
                    if member.file_size <= 0:
                        continue
                    if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                        raise RuntimeError("ZIP 원문의 개별 파일이 200MB를 초과했습니다.")
                    if (
                        member.file_size > 10 * 1024 * 1024
                        and member.compress_size > 0
                        and member.file_size > member.compress_size * 200
                    ):
                        raise RuntimeError("비정상적인 압축률의 ZIP 원문을 차단했습니다.")
                    suffix = Path(member.filename).suffix.lower()
                    if suffix not in {
                        ".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png",
                        ".jp2", ".jpx", ".bmp", ".gif",
                    }:
                        continue
                    data = archive.read(member)
                    try:
                        inserted += _insert_payload_document(
                            merged, filename=member.filename, data=data
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            f"ZIP 원문 내부 파일을 변환하지 못했습니다: {Path(member.filename).name}"
                        ) from exc
                if inserted == 0:
                    raise RuntimeError("ZIP 원문에 PDF 또는 지원 이미지가 없습니다.")
        else:
            with source_path.open("rb") as stream:
                signature = stream.read(16)
            if signature.startswith(b"%PDF-"):
                payload_type = "PDF 원문"
            elif signature.startswith((b"II*\x00", b"MM\x00*")):
                payload_type = "TIFF 원문"
            elif signature.startswith(b"\x89PNG"):
                payload_type = "PNG 원문"
            elif signature.startswith(b"\xff\xd8\xff"):
                payload_type = "JPEG 원문"
            elif signature[4:12] in {b"jP  \r\n\x87\n", b"ftypjp2 ", b"ftypjpx "}:
                payload_type = "JPEG2000 원문"
            else:
                raise RuntimeError(
                    "KIPRISPlus가 PDF·ZIP·TIFF·지원 이미지가 아닌 응답을 반환했습니다."
                )
            _insert_payload_document(merged, path=source_path)

        if merged.page_count <= 0 or merged.page_count > MAX_PDF_PAGES:
            raise RuntimeError("변환된 원문의 페이지 수가 안전 제한을 벗어났습니다.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        merged.save(destination, garbage=4, deflate=True)
    finally:
        merged.close()
    validate_pdf_for_processing(destination)
    with pymupdf.open(destination) as converted:
        page_count = converted.page_count
    return payload_type, page_count


def list_pdf_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        (path for path in root.rglob("*.pdf") if path.is_file()),
        key=lambda path: (str(path.parent).lower(), path.name.lower()),
    )


def list_pdf_folders(root: Path) -> list[Path]:
    if not root.exists():
        return []
    folders = {path.parent for path in root.rglob("*.pdf") if path.is_file()}
    folders.update(
        path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")
    )
    return sorted(
        folders,
        key=lambda folder: max(
            (
                path.stat().st_mtime
                for path in folder.iterdir()
                if path.is_file()
            ),
            default=folder.stat().st_mtime,
        ),
        reverse=True,
    )


def list_folder_pdf_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(
        (path for path in folder.glob("*.pdf") if path.is_file()),
        key=lambda path: path.name.lower(),
    )


def validate_pdf_for_processing(pdf_path: Path) -> tuple[int, int]:
    pdf_path = Path(pdf_path)
    size = pdf_path.stat().st_size
    if size <= 0 or size > MAX_PDF_BYTES:
        raise RuntimeError(
            f"PDF 크기가 허용 범위를 벗어났습니다: {size / 1024 / 1024:.1f} MB"
        )
    with pdf_path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise RuntimeError(f"PDF 형식이 아닙니다: {pdf_path.name}")
    with pymupdf.open(pdf_path) as document:
        page_count = document.page_count
    if page_count <= 0 or page_count > MAX_PDF_PAGES:
        raise RuntimeError(f"PDF 페이지 수가 허용 범위를 벗어났습니다: {page_count}페이지")
    return size, page_count


def render_pdf_page(
    pdf_path: Path,
    page_number: int,
    max_width: int = 500,
    max_height: int = 430,
    zoom_factor: float = 1.0,
    fit_width: bool = False,
    fit_height: bool = False,
) -> tuple[Image.Image, int, int]:
    from PIL import Image

    validate_pdf_for_processing(pdf_path)
    with pymupdf.open(pdf_path) as document:
        if document.page_count == 0:
            raise RuntimeError("페이지가 없는 PDF입니다.")
        page_number = min(max(page_number, 0), document.page_count - 1)
        page = document.load_page(page_number)
        rect = page.rect
        if fit_height:
            base_zoom = max_height / max(rect.height, 1)
        elif fit_width:
            base_zoom = max_width / max(rect.width, 1)
        else:
            base_zoom = min(
                max_width / max(rect.width, 1),
                max_height / max(rect.height, 1),
            )
        zoom = min(max(base_zoom * zoom_factor, 0.2), 5.0)
        pixels = rect.width * zoom * rect.height * zoom
        if pixels > MAX_RENDER_PIXELS:
            zoom *= (MAX_RENDER_PIXELS / pixels) ** 0.5
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(zoom, zoom),
            colorspace=pymupdf.csRGB,
            alpha=False,
        )
        image = Image.frombytes(
            "RGB",
            (pixmap.width, pixmap.height),
            pixmap.samples,
        )
        return image, document.page_count, page_number


def merge_pdfs(pdf_paths: Iterable[Path], destination: Path) -> int:
    sources = [Path(path) for path in pdf_paths]
    if len(sources) < 2:
        raise ValueError("합칠 PDF를 두 개 이상 선택해 주세요.")
    destination = Path(destination)
    destination_resolved = destination.resolve()
    if any(path.resolve() == destination_resolved for path in sources):
        raise ValueError("원본 PDF와 다른 이름으로 저장해 주세요.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    merged = pymupdf.open()
    try:
        total_pages = 0
        for source_path in sources:
            _, source_pages = validate_pdf_for_processing(source_path)
            total_pages += source_pages
            if total_pages > MAX_MERGE_PAGES:
                raise RuntimeError(
                    f"병합 페이지 제한({MAX_MERGE_PAGES:,}페이지)을 초과했습니다."
                )
            with pymupdf.open(source_path) as source:
                if source.page_count:
                    merged.insert_pdf(source)
        if merged.page_count == 0:
            raise RuntimeError("병합할 PDF 페이지가 없습니다.")
        with tempfile.NamedTemporaryFile(
            prefix=".kipris_merge_",
            suffix=".pdf",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        merged.save(temporary_path, garbage=4, deflate=True)
        validate_pdf_for_processing(temporary_path)
        os.replace(temporary_path, destination)
        temporary_path = None
        return merged.page_count
    finally:
        merged.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def extract_pdf_text(pdf_paths: Iterable[Path]) -> str:
    sections: list[str] = []
    character_count = 0
    page_count = 0
    for pdf_path in (Path(path) for path in pdf_paths):
        _, pages = validate_pdf_for_processing(pdf_path)
        page_count += pages
        if page_count > MAX_PDF_PAGES:
            raise RuntimeError(
                f"텍스트 추출 페이지 제한({MAX_PDF_PAGES:,}페이지)을 초과했습니다."
            )
        with pymupdf.open(pdf_path) as document:
            page_sections: list[str] = []
            for page_index, page in enumerate(document, 1):
                text = page.get_text("text").strip()
                if text:
                    character_count += len(text)
                    if character_count > MAX_EXTRACT_CHARACTERS:
                        raise RuntimeError(
                            "추출 텍스트가 안전 제한(1,000만 자)을 초과했습니다. "
                            "파일을 나누어 처리해 주세요."
                        )
                    page_sections.append(f"[페이지 {page_index}]\n{text}")
            content = "\n\n".join(page_sections)
            if not content:
                content = "[추출 가능한 내장 텍스트가 없습니다.]"
            sections.append(f"===== {pdf_path.name} =====\n{content}")
    return "\n\n".join(sections)
