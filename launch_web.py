from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


APP_NAME = "KIPRIS Document Hub"


def log_path() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / ".kipris_document_hub"
    folder = base / "KIPRISDocumentHub"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "startup.log"


def append_log(message: str) -> Path:
    path = log_path()
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}]\n{message.rstrip()}\n")
    return path


def show_error(message: str, path: Path) -> None:
    detail = (
        f"{message}\n\n"
        "오류 원인은 아래 startup.log 파일에서 확인할 수 있습니다.\n\n"
        f"시작 로그: {path}"
    )
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, detail, APP_NAME, 0x10)
    except Exception:
        pass


def resource_path(relative: str) -> Path:
    """Return bundled resource path for both source and PyInstaller one-file builds."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def check_installation(full: bool = False) -> None:
    import fastapi  # noqa: F401
    import uvicorn  # noqa: F401

    if full:
        import keyring  # noqa: F401
        import openpyxl  # noqa: F401
        import playwright  # noqa: F401
        import pymupdf  # noqa: F401

    # In a PyInstaller --onefile executable, bundled data is extracted under
    # sys._MEIPASS rather than beside launch_web.py.  Use the same resource
    # resolution rule as web_app.py so startup validation works after packaging.
    required = (
        resource_path("web/index.html"),
        resource_path("web/app.css"),
        resource_path("web/app.js"),
        resource_path("assets/rocket_background.png"),
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("필수 프로그램 파일이 없습니다:\n" + "\n".join(missing))

    import web_app

    if not web_app.WEB_DIR.is_dir() or not web_app.ASSET_DIR.is_dir():
        raise RuntimeError("웹 화면 또는 이미지 폴더를 불러오지 못했습니다.")


def main() -> int:
    try:
        diagnostic = "--check" in sys.argv or "--debug" in sys.argv
        check_installation(full=diagnostic)
        if "--check" in sys.argv:
            print("KIPRIS Document Hub startup check passed.")
            return 0
        append_log("프로그램 시작 요청")
        import web_app

        web_app.main()
        append_log("프로그램 정상 종료")
        return 0
    except KeyboardInterrupt:
        append_log("미리보기 또는 프로그램을 사용자가 종료함")
        return 0
    except BaseException:
        detail = traceback.format_exc()
        path = append_log(detail)
        show_error("프로그램을 시작하지 못했습니다.", path)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
