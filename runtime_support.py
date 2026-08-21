from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path


CREDENTIAL_SERVICE = "KIPRIS Document Downloader"
CREDENTIAL_USERNAME = "REST AccessKey"


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


def _keyring_module():
    try:
        import keyring

        return keyring
    except ModuleNotFoundError:
        return None


def load_saved_api_key() -> str:
    environment_key = os.environ.get("KIPRIS_API_KEY", "").strip()
    if environment_key:
        return environment_key
    keyring = _keyring_module()
    if keyring is None:
        return ""
    try:
        return (
            keyring.get_password(CREDENTIAL_SERVICE, CREDENTIAL_USERNAME) or ""
        ).strip()
    except Exception:
        return ""


def save_api_key(api_key: str) -> None:
    keyring = _keyring_module()
    if keyring is None:
        raise RuntimeError("Windows 자격 증명 저장 모듈이 설치되지 않았습니다.")
    keyring.set_password(CREDENTIAL_SERVICE, CREDENTIAL_USERNAME, api_key)


def delete_saved_api_key() -> None:
    keyring = _keyring_module()
    if keyring is None:
        return
    try:
        keyring.delete_password(CREDENTIAL_SERVICE, CREDENTIAL_USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass
