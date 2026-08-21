from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api_client import MAX_DOWNLOAD_BYTES, KiprisApiClient
from core import (
    MAX_INPUT_FILE_BYTES,
    NumberItem,
    load_number_file,
    parse_text_items,
    safe_error_text,
    safe_filename,
)
from runtime_support import (
    delete_saved_api_key,
    load_saved_api_key,
    save_api_key,
    write_csv_atomic,
)


APP_VERSION = "6.13"
DEFAULT_DELAY_SECONDS = 0.35
MAX_EXTERNAL_PDF_FILES = 100
MAX_EXTERNAL_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
SESSION_TOKEN = secrets.token_urlsafe(32)
LOG_LIMIT = 500
CONFIG_FILENAME = "web_config.json"
APP_SERVER: uvicorn.Server | None = None


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


WEB_DIR = resource_path("web")
ASSET_DIR = resource_path("assets")


def config_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / ".kipris_document_hub"
    return base / "KIPRISDocumentHub"


def default_output_dir() -> Path:
    return Path.home() / "Downloads" / "KIPRIS"


def load_config() -> dict[str, Any]:
    path = config_dir() / CONFIG_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


def save_config(values: dict[str, Any]) -> None:
    folder = config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / CONFIG_FILENAME
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".kipris_config_",
            suffix=".json",
            dir=folder,
            delete=False,
            mode="w",
            encoding="utf-8",
        ) as stream:
            temporary = Path(stream.name)
            json.dump(values, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def clean_log_message(message: Any) -> str:
    text = str(message).replace("\x00", "").strip()
    text = re.sub(
        r"(?i)(accessKey|api[_-]?key|authorization)=([^&\s]+)",
        r"\1=[보호됨]",
        text,
    )
    text = re.sub(r"(?im)^\s*-\s*cookie:.*$", "", text)
    return text[:8000]


class JobState:
    def __init__(self) -> None:
        config = load_config()
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.running = False
        self.status = "준비"
        self.completed = 0
        self.total = 0
        self.success = 0
        self.files = 0
        self.failures = 0
        self.preview_folder = ""
        self.preview_file = ""
        self.logs: deque[str] = deque(maxlen=LOG_LIMIT)
        self.external_files: dict[str, Path] = {}
        self.output_dir = Path(
            config.get("output_dir") or default_output_dir()
        ).expanduser()
        self.allow_insecure_tls = bool(config.get("allow_insecure_tls", False))
        self.updated_at = time.time()

    def log(self, message: Any) -> None:
        cleaned = clean_log_message(message)
        if not cleaned:
            return
        with self.lock:
            self.logs.append(cleaned)
            self.updated_at = time.time()

    def begin(self, total: int, output_dir: Path) -> None:
        with self.lock:
            self.cancel_event.clear()
            self.running = True
            self.status = "진행 중"
            self.completed = 0
            self.total = total
            self.success = 0
            self.files = 0
            self.failures = 0
            self.preview_folder = ""
            self.preview_file = ""
            self.logs.clear()
            self.output_dir = output_dir
            self.updated_at = time.time()

    def set_preview_pdf(self, path: Path, output_dir: Path) -> None:
        try:
            resolved = path.resolve()
            root = output_dir.resolve()
            relative = resolved.relative_to(root)
        except (OSError, ValueError):
            return
        if len(relative.parts) != 2 or not resolved.is_file():
            return
        with self.lock:
            self.preview_folder = relative.parts[0]
            self.preview_file = relative.parts[1]
            self.updated_at = time.time()

    def progress(self, completed: int) -> None:
        with self.lock:
            self.completed = completed
            self.updated_at = time.time()

    def finish(self, success: int, files: int, failures: int) -> None:
        with self.lock:
            self.running = False
            self.success = success
            self.files = files
            self.failures = failures
            if self.cancel_event.is_set():
                self.status = "중지됨"
            elif files and not failures:
                self.status = "다운로드 완료"
            elif files:
                self.status = "일부 실패"
            else:
                self.status = "다운로드 실패"
            self.updated_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            percent = int(self.completed / self.total * 100) if self.total else 0
            return {
                "running": self.running,
                "status": self.status,
                "completed": self.completed,
                "total": self.total,
                "percent": percent,
                "success": self.success,
                "files": self.files,
                "failures": self.failures,
                "preview_folder": self.preview_folder,
                "preview_file": self.preview_file,
                "logs": list(self.logs),
                "output_dir": str(self.output_dir),
                "updated_at": self.updated_at,
            }


STATE = JobState()


class DownloadRequest(BaseModel):
    numbers: str = Field(min_length=1, max_length=200_000)
    output_dir: str = Field(min_length=1, max_length=1000)
    want_publication: bool = True
    want_admin: bool = True
    google_patents: bool = True
    allow_insecure_tls: bool = False


class SettingsRequest(BaseModel):
    api_key: str = Field(default="", max_length=1000)
    output_dir: str = Field(default="", max_length=1000)
    allow_insecure_tls: bool = False


class FolderRequest(BaseModel):
    folder: str = Field(default="", max_length=260)


class FileOperationRequest(BaseModel):
    folder: str = Field(min_length=1, max_length=260)
    files: list[str] = Field(min_length=1, max_length=100)


class MergeRequest(FileOperationRequest):
    output_name: str = Field(min_length=1, max_length=200)


class ExternalExtractRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=MAX_EXTERNAL_PDF_FILES)


def require_token(request: Request, allow_query: bool = False) -> None:
    supplied = request.headers.get("x-kipris-token", "")
    if allow_query and not supplied:
        supplied = request.query_params.get("token", "")
    if not supplied or not hmac.compare_digest(supplied, SESSION_TOKEN):
        raise HTTPException(status_code=403, detail="로컬 세션 인증에 실패했습니다.")


def normalize_output_dir(raw: str) -> Path:
    value = (raw or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="저장 폴더를 입력해 주세요.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail="저장 폴더는 전체 경로로 입력해 주세요.")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"저장 폴더를 만들 수 없습니다: {safe_error_text(exc)}",
        ) from exc
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="저장 폴더가 디렉터리가 아닙니다.")
    return path.resolve()


def safe_child(root: Path, *parts: str) -> Path:
    if any(not part or part in {".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="잘못된 파일 경로입니다.")
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="허용되지 않은 파일 경로입니다.") from exc
    return candidate


def move_to_recycle_bin(path: Path) -> None:
    """Move one local Windows file or folder to the Recycle Bin."""
    if os.name != "nt":
        raise RuntimeError("휴지통 삭제는 Windows에서만 지원됩니다.")
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = (
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        )

    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = str(path) + "\0\0"
    operation.pTo = None
    operation.fFlags = 0x0040 | 0x0010 | 0x0400  # ALLOWUNDO, NOCONFIRMATION, NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise RuntimeError(f"Windows 휴지통 이동에 실패했습니다(코드 {result}).")


def current_root() -> Path:
    with STATE.lock:
        root = STATE.output_dir
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def list_folder_pdf_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(
        (path for path in folder.glob("*.pdf") if path.is_file()),
        key=lambda path: path.name.lower(),
    )


def list_pdf_folders(root: Path) -> list[Path]:
    if not root.exists():
        return []
    folders = {
        path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")
    }
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


def selected_pdf_paths(
    folder: str, files: list[str], validate: bool = True
) -> tuple[Path, list[Path]]:
    root = current_root()
    folder_path = safe_child(root, folder)
    if not folder_path.is_dir():
        raise HTTPException(status_code=404, detail="문헌 폴더를 찾을 수 없습니다.")
    paths: list[Path] = []
    seen: set[Path] = set()
    for filename in files:
        if Path(filename).name != filename or not filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="잘못된 PDF 파일명입니다.")
        path = safe_child(folder_path, filename)
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"파일을 찾을 수 없습니다: {filename}")
        if path not in seen:
            if validate:
                from pdf_tools import validate_pdf_for_processing

                validate_pdf_for_processing(path)
            paths.append(path)
            seen.add(path)
    return folder_path, paths


def run_download_job(
    items: list[NumberItem],
    output_dir: Path,
    want_publication: bool,
    want_admin: bool,
    google_patents: bool,
    allow_insecure_tls: bool,
) -> None:
    api_key = load_saved_api_key()
    success = 0
    files = 0
    failures: list[tuple[str, str]] = []
    admin_downloader: Any = None
    admin_downloader_class: Any = None
    completed = 0

    def remember_folder_pdf(folder_name: str) -> None:
        folder = output_dir / folder_name
        candidates = list_folder_pdf_files(folder)
        if candidates:
            STATE.set_preview_pdf(
                max(candidates, key=lambda path: path.stat().st_mtime),
                output_dir,
            )

    try:
        STATE.log(f"총 {len(items)}건을 시작합니다.")
        if want_publication and not api_key:
            STATE.log("API Key가 없습니다. 외국공보만 Google Patents로 시도합니다.")
        api_client = KiprisApiClient(
            api_key,
            google_patents,
            STATE.log,
            allow_insecure_tls=allow_insecure_tls,
            cancel_event=STATE.cancel_event,
        )
        if want_admin and any(item.is_domestic for item in items):
            STATE.log("행정서류용 숨김 브라우저 엔진을 준비합니다.")
            from app import KiprisAdminDownloader

            admin_downloader_class = KiprisAdminDownloader
            STATE.log(
                "행정서류는 KIPRISPlus API를 먼저 사용하고 API 미지원 서류만 "
                "숨김 Chrome으로 보완합니다."
            )

        for index, item in enumerate(items, 1):
            if STATE.cancel_event.is_set():
                STATE.log("중지 요청을 확인하여 남은 문헌 처리를 종료합니다.")
                break
            STATE.log(f"[{index}/{len(items)}] {item.number}")
            count = 0
            item_errors: list[str] = []
            if want_publication:
                try:
                    result = api_client.download_publication(item, output_dir)
                    count += 1
                    STATE.set_preview_pdf(result.destination, output_dir)
                    STATE.log(f"  저장: {result.destination.name} [{result.source}]")
                except Exception as exc:
                    error = safe_error_text(exc)
                    item_errors.append(f"공개·등록공보: {error}")
                    STATE.log(f"  공개·등록공보 실패: {error}")

            if want_admin:
                if not item.is_domestic:
                    STATE.log("  외국 문헌의 출원서·심사서류는 지원하지 않습니다.")
                else:
                    admin_count = 0
                    api_unsupported = 1
                    api_failures: list[tuple[str, str]] = []
                    api_error: Exception | None = None
                    if api_key:
                        try:
                            api_count, api_application, api_unsupported, api_failures = (
                                api_client.download_admin_documents_api(item, output_dir)
                            )
                            admin_count += api_count
                            if api_count:
                                remember_folder_pdf(api_application)
                            STATE.log(
                                f"  행정서류 API 저장 {api_count}개, 브라우저 보완 대상 "
                                f"{api_unsupported + len(api_failures)}개"
                            )
                        except Exception as exc:
                            api_error = exc
                            STATE.log(
                                "  행정서류 API 실패, 숨김 Chrome으로 보완: "
                                f"{safe_error_text(exc)}"
                            )
                    else:
                        STATE.log("  API Key가 없어 행정서류를 KIPRIS 화면으로 처리합니다.")

                    need_browser = (
                        not api_key
                        or api_error is not None
                        or api_unsupported > 0
                        or bool(api_failures)
                    )
                    browser_count = 0
                    browser_error: Exception | None = None
                    if need_browser and admin_downloader is None:
                        STATE.log("KIPRIS 통합행정정보 브라우저를 숨김으로 시작합니다.")
                        try:
                            admin_downloader = admin_downloader_class(
                                output_dir,
                                STATE.log,
                                headless=True,
                                allow_insecure_tls=allow_insecure_tls,
                                cancel_event=STATE.cancel_event,
                            )
                            admin_downloader.__enter__()
                        except Exception as exc:
                            browser_error = exc
                            admin_downloader = None
                            STATE.log(f"  Chrome 시작 실패: {safe_error_text(exc)}")

                    if need_browser and admin_downloader is not None:
                        try:
                            browser_count, browser_application = admin_downloader.download_admin_item(item)
                            if browser_count:
                                remember_folder_pdf(browser_application)
                        except Exception as exc:
                            browser_error = exc
                            STATE.log(f"  숨김 행정서류 처리 실패: {safe_error_text(exc)}")

                    if (
                        need_browser
                        and browser_count == 0
                        and not STATE.cancel_event.is_set()
                    ):
                        STATE.log("  원문 저장이 없어 숨김 Chrome으로 한 번 재시도합니다.")
                        if admin_downloader is not None:
                            try:
                                admin_downloader.__exit__(None, None, None)
                            except Exception:
                                pass
                        admin_downloader = admin_downloader_class(
                            output_dir,
                            STATE.log,
                            headless=True,
                            allow_insecure_tls=allow_insecure_tls,
                            cancel_event=STATE.cancel_event,
                        )
                        try:
                            admin_downloader.__enter__()
                            browser_count, browser_application = admin_downloader.download_admin_item(item)
                            if browser_count:
                                remember_folder_pdf(browser_application)
                            browser_error = None
                        except Exception as exc:
                            browser_error = exc
                            STATE.log(f"  숨김 재시도 실패: {safe_error_text(exc)}")

                    admin_count += browser_count
                    count += admin_count
                    if need_browser and browser_count == 0 and browser_error is not None:
                        item_errors.append(
                            "행정서류 일부 또는 전체: " + safe_error_text(browser_error)
                        )

            files += count
            if count:
                success += 1
                STATE.log(f"  완료: {count}개 파일")
            if item_errors:
                failures.append((item.number, " / ".join(item_errors)))
            elif not count:
                failures.append((item.number, "저장 가능한 문서가 없습니다."))
            completed = index
            STATE.progress(completed)
            time.sleep(DEFAULT_DELAY_SECONDS)
    except Exception as exc:
        error = safe_error_text(exc)
        failures.append(("프로그램", error))
        STATE.log(f"중단: {error}")
    finally:
        if admin_downloader is not None:
            try:
                admin_downloader.__exit__(None, None, None)
            except Exception as exc:
                STATE.log(f"브라우저 종료 경고: {safe_error_text(exc)}")
        report_path = output_dir / "download_report.csv"
        write_csv_atomic(
            report_path,
            ["번호", "실패 또는 미저장 사유"],
            failures,
        )
        STATE.progress(completed if STATE.cancel_event.is_set() else len(items))
        STATE.log(
            f"종료: 성공 {success}건, 저장 {files}개, 미저장 {len(failures)}건"
        )
        STATE.log(f"저장 위치: {output_dir}")
        STATE.finish(success, files, len(failures))


app = FastAPI(
    title="KIPRIS Document Hub",
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/assets", StaticFiles(directory=str(ASSET_DIR)), name="assets")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-src 'self'; "
        "object-src 'self'; base-uri 'none'; form-action 'self'"
    )
    return response


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__SESSION_TOKEN__", SESSION_TOKEN)
    html = html.replace("__APP_VERSION__", APP_VERSION)
    return HTMLResponse(html)


@app.get("/app.css")
def app_css() -> FileResponse:
    return FileResponse(WEB_DIR / "app.css", media_type="text/css")


@app.get("/app.js")
def app_js() -> FileResponse:
    return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")


@app.get("/api/config")
def get_config(request: Request) -> dict[str, Any]:
    require_token(request)
    return {
        "version": APP_VERSION,
        "output_dir": str(current_root()),
        "has_api_key": bool(load_saved_api_key()),
        "api_key_from_environment": bool(os.environ.get("KIPRIS_API_KEY", "").strip()),
        "allow_insecure_tls": STATE.allow_insecure_tls,
    }


@app.post("/api/config")
def set_config(payload: SettingsRequest, request: Request) -> dict[str, Any]:
    require_token(request)
    if STATE.running:
        raise HTTPException(status_code=409, detail="다운로드 중에는 설정을 바꿀 수 없습니다.")
    if payload.api_key.strip():
        try:
            save_api_key(payload.api_key.strip())
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    "AccessKey를 Windows 자격 증명에 저장하지 못했습니다: "
                    f"{safe_error_text(exc)}"
                ),
            ) from exc
    output = normalize_output_dir(payload.output_dir or str(STATE.output_dir))
    with STATE.lock:
        STATE.output_dir = output
        STATE.allow_insecure_tls = payload.allow_insecure_tls
    save_config(
        {
            "output_dir": str(output),
            "allow_insecure_tls": payload.allow_insecure_tls,
        }
    )
    return {"ok": True, "has_api_key": bool(load_saved_api_key())}


@app.delete("/api/config/key")
def delete_key(request: Request) -> dict[str, bool]:
    require_token(request)
    if os.environ.get("KIPRIS_API_KEY", "").strip():
        raise HTTPException(
            status_code=409,
            detail="환경변수 KIPRIS_API_KEY가 설정되어 있어 프로그램에서 삭제할 수 없습니다.",
        )
    delete_saved_api_key()
    return {"ok": True}


@app.post("/api/choose-output")
def choose_output(request: Request) -> dict[str, str]:
    require_token(request)
    if STATE.running:
        raise HTTPException(status_code=409, detail="다운로드 중에는 폴더를 바꿀 수 없습니다.")
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(initialdir=str(current_root()))
        root.destroy()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"폴더 선택창을 열 수 없습니다: {safe_error_text(exc)}",
        ) from exc
    return {"path": selected or str(current_root())}


@app.post("/api/choose-external-pdfs")
def choose_external_pdfs(request: Request) -> dict[str, Any]:
    require_token(request)
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        selected = filedialog.askopenfilenames(
            title="텍스트를 추출할 PDF 선택",
            filetypes=(("PDF 파일", "*.pdf"), ("모든 파일", "*.*")),
        )
        root.destroy()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"PDF 선택창을 열 수 없습니다: {safe_error_text(exc)}",
        ) from exc

    if not selected:
        return {"count": 0, "files": []}
    if len(selected) > MAX_EXTERNAL_PDF_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"외부 PDF는 한 번에 최대 {MAX_EXTERNAL_PDF_FILES}개까지 선택할 수 있습니다.",
        )

    registered: dict[str, Path] = {}
    response_files: list[dict[str, Any]] = []
    total_bytes = 0
    for raw_path in selected:
        path = Path(raw_path).resolve()
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise HTTPException(status_code=400, detail=f"PDF 파일이 아닙니다: {path.name}")
        size = path.stat().st_size
        if size <= 0 or size > MAX_DOWNLOAD_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"PDF 크기가 허용 범위를 벗어났습니다: {path.name}",
            )
        total_bytes += size
        if total_bytes > MAX_EXTERNAL_TOTAL_BYTES:
            raise HTTPException(
                status_code=400,
                detail="선택한 외부 PDF의 전체 크기가 2GB를 초과했습니다.",
            )
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise HTTPException(status_code=400, detail=f"PDF 형식이 아닙니다: {path.name}")
        identifier = secrets.token_urlsafe(18)
        registered[identifier] = path
        response_files.append({"id": identifier, "name": path.name, "size": size})

    with STATE.lock:
        STATE.external_files = registered
    return {"count": len(response_files), "files": response_files}


@app.post("/api/input-file")
async def input_file(request: Request) -> dict[str, Any]:
    require_token(request)
    suffix = request.headers.get("x-file-suffix", "").strip().lower()
    if suffix not in {".txt", ".csv", ".xlsx"}:
        raise HTTPException(status_code=400, detail="TXT, CSV 또는 XLSX 파일만 지원합니다.")
    content = await request.body()
    if not content or len(content) > MAX_INPUT_FILE_BYTES:
        raise HTTPException(status_code=400, detail="입력 파일 크기가 허용 범위를 벗어났습니다.")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        items = load_number_file(temporary)
        lines = []
        for item in items:
            if item.kind == "application":
                lines.append(f"출원번호,{item.number}")
            elif item.kind == "publication":
                lines.append(f"공개번호,{item.number}")
            elif item.kind == "registration":
                lines.append(f"등록번호,{item.number}")
            else:
                lines.append(item.number)
        return {"count": len(items), "numbers": "\n".join(lines)}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@app.post("/api/download")
def start_download(payload: DownloadRequest, request: Request) -> dict[str, Any]:
    require_token(request)
    with STATE.lock:
        if STATE.running:
            raise HTTPException(status_code=409, detail="이미 다운로드가 진행 중입니다.")
    if not payload.want_publication and not payload.want_admin:
        raise HTTPException(status_code=400, detail="다운로드 대상을 하나 이상 선택해 주세요.")
    try:
        items = parse_text_items(payload.numbers)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=safe_error_text(exc)) from exc
    if not items:
        raise HTTPException(status_code=400, detail="문헌번호를 입력해 주세요.")
    output = normalize_output_dir(payload.output_dir)
    STATE.begin(len(items), output)
    with STATE.lock:
        STATE.allow_insecure_tls = payload.allow_insecure_tls
    save_config(
        {
            "output_dir": str(output),
            "allow_insecure_tls": payload.allow_insecure_tls,
        }
    )
    thread = threading.Thread(
        target=run_download_job,
        args=(
            items,
            output,
            payload.want_publication,
            payload.want_admin,
            payload.google_patents,
            payload.allow_insecure_tls,
        ),
        daemon=True,
        name="kipris-download-worker",
    )
    thread.start()
    return {"ok": True, "total": len(items)}


@app.post("/api/cancel")
def cancel_download(request: Request) -> dict[str, bool]:
    require_token(request)
    STATE.cancel_event.set()
    STATE.log("사용자가 중지를 요청했습니다. 현재 문서 처리 후 종료합니다.")
    return {"ok": True}


@app.get("/api/status")
def status(request: Request) -> dict[str, Any]:
    require_token(request)
    return STATE.snapshot()


@app.get("/api/folders")
def folders(request: Request) -> dict[str, Any]:
    require_token(request)
    root = current_root()
    values = []
    for folder in list_pdf_folders(root):
        try:
            folder.relative_to(root)
        except ValueError:
            continue
        values.append(
            {
                "name": folder.name,
                "pdf_count": len(list_folder_pdf_files(folder)),
            }
        )
    return {"root": str(root), "folders": values}


@app.get("/api/files")
def files(folder: str, request: Request) -> dict[str, Any]:
    require_token(request)
    root = current_root()
    folder_path = safe_child(root, folder)
    if not folder_path.is_dir():
        raise HTTPException(status_code=404, detail="문헌 폴더를 찾을 수 없습니다.")
    values = []
    for path in list_folder_pdf_files(folder_path):
        values.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "modified": path.stat().st_mtime,
            }
        )
    return {"folder": folder, "files": values}


@app.get("/api/pdf")
def pdf(folder: str, file: str, request: Request) -> FileResponse:
    require_token(request, allow_query=True)
    _, paths = selected_pdf_paths(folder, [file])
    return FileResponse(
        paths[0],
        media_type="application/pdf",
        filename=paths[0].name,
        content_disposition_type="inline",
    )


@app.post("/api/open-folder")
def open_folder(payload: FolderRequest, request: Request) -> dict[str, bool]:
    require_token(request)
    root = current_root()
    folder = safe_child(root, payload.folder) if payload.folder else root
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail="폴더를 찾을 수 없습니다.")
    if os.name != "nt":
        raise HTTPException(status_code=501, detail="폴더 열기는 Windows에서 지원됩니다.")
    os.startfile(str(folder))  # type: ignore[attr-defined]
    return {"ok": True}


@app.post("/api/delete-files")
def delete_files(payload: FileOperationRequest, request: Request) -> dict[str, Any]:
    require_token(request)
    with STATE.lock:
        if STATE.running:
            raise HTTPException(status_code=409, detail="다운로드 중에는 파일을 삭제할 수 없습니다.")
    _, paths = selected_pdf_paths(payload.folder, payload.files, validate=False)
    deleted: list[str] = []
    failures: list[str] = []
    for path in paths:
        try:
            move_to_recycle_bin(path)
            deleted.append(path.name)
        except Exception as exc:
            failures.append(f"{path.name}: {safe_error_text(exc)}")
    return {"ok": not failures, "deleted": deleted, "failures": failures}


@app.post("/api/delete-folder")
def delete_folder(payload: FolderRequest, request: Request) -> dict[str, Any]:
    require_token(request)
    with STATE.lock:
        if STATE.running:
            raise HTTPException(status_code=409, detail="다운로드 중에는 폴더를 삭제할 수 없습니다.")
    if not payload.folder:
        raise HTTPException(status_code=400, detail="삭제할 문헌 폴더를 선택해 주세요.")
    root = current_root()
    folder = safe_child(root, payload.folder)
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail="문헌 폴더를 찾을 수 없습니다.")
    try:
        move_to_recycle_bin(folder)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=safe_error_text(exc)) from exc
    return {"ok": True, "folder": payload.folder}


@app.post("/api/merge")
def merge(payload: MergeRequest, request: Request) -> dict[str, Any]:
    require_token(request)
    from pdf_tools import merge_pdfs

    folder, paths = selected_pdf_paths(payload.folder, payload.files)
    if len(paths) < 2:
        raise HTTPException(status_code=400, detail="PDF를 두 개 이상 선택해 주세요.")
    output_name = safe_filename(payload.output_name.strip())
    if not output_name.lower().endswith(".pdf"):
        output_name += ".pdf"
    if output_name in {".pdf", "_.pdf"}:
        raise HTTPException(status_code=400, detail="생성 파일명을 입력해 주세요.")
    destination = safe_child(folder, output_name)
    try:
        pages = merge_pdfs(paths, destination)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=safe_error_text(exc)) from exc
    return {"ok": True, "file": destination.name, "pages": pages}


@app.post("/api/extract")
def extract(payload: FileOperationRequest, request: Request) -> dict[str, Any]:
    require_token(request)
    from pdf_tools import extract_pdf_text

    _, paths = selected_pdf_paths(payload.folder, payload.files)
    try:
        text = extract_pdf_text(paths)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=safe_error_text(exc)) from exc
    return {"ok": True, "text": text, "characters": len(text)}


@app.post("/api/extract-external")
def extract_external(payload: ExternalExtractRequest, request: Request) -> dict[str, Any]:
    require_token(request)
    with STATE.lock:
        registered = dict(STATE.external_files)
    paths: list[Path] = []
    seen: set[str] = set()
    for identifier in payload.ids:
        if identifier in seen:
            continue
        seen.add(identifier)
        path = registered.get(identifier)
        if path is None:
            raise HTTPException(status_code=403, detail="외부 PDF 선택 정보가 만료되었습니다.")
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"파일을 찾을 수 없습니다: {path.name}")
        paths.append(path)
    from pdf_tools import extract_pdf_text

    try:
        text = extract_pdf_text(paths)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=safe_error_text(exc)) from exc
    return {
        "ok": True,
        "text": text,
        "characters": len(text),
        "files": [path.name for path in paths],
    }


@app.post("/api/shutdown")
def shutdown(request: Request) -> dict[str, bool]:
    require_token(request)
    if STATE.running:
        raise HTTPException(status_code=409, detail="다운로드를 중지한 뒤 종료해 주세요.")
    if APP_SERVER is not None:
        APP_SERVER.should_exit = True
    return {"ok": True}


def find_available_port() -> int:
    for port in range(8765, 8796):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("KIPRIS 웹 화면에 사용할 로컬 포트를 찾지 못했습니다.")


def find_chrome() -> Path | None:
    candidates = []
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(variable, "").strip()
        if base:
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def open_ui(port: int) -> None:
    url = f"http://127.0.0.1:{port}/"
    chrome = find_chrome()
    try:
        if chrome:
            subprocess.Popen(
                [str(chrome), "--new-window", "--start-maximized", url],
                close_fds=True,
            )
        else:
            webbrowser.open(url, new=1)
    except Exception:
        webbrowser.open(url, new=1)


def wait_and_open_ui(port: int) -> None:
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                open_ui(port)
                return
        except OSError:
            time.sleep(0.1)


def main() -> None:
    global APP_SERVER
    port = find_available_port()
    debug = "--debug" in sys.argv
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info" if debug else "error",
        access_log=False,
        use_colors=False,
        server_header=False,
    )
    APP_SERVER = uvicorn.Server(config)
    if debug:
        print(f"KIPRIS Document Hub preview: http://127.0.0.1:{port}/")
    threading.Thread(
        target=wait_and_open_ui,
        args=(port,),
        daemon=True,
        name="kipris-ui-launcher",
    ).start()
    APP_SERVER.run()


if __name__ == "__main__":
    main()
