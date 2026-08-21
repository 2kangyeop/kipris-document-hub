from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


EXPECTED = {
    "playwright": "1.62.0",
    "fastapi": "0.115.12",
    "uvicorn": "0.34.2",
    "openpyxl": "3.1.5",
    "keyring": "25.7.0",
    "PyMuPDF": "1.28.2",
}


def main() -> int:
    failures: list[str] = []
    for package, expected in EXPECTED.items():
        try:
            installed = version(package)
        except PackageNotFoundError:
            failures.append(f"{package}: missing")
            continue
        if installed != expected:
            failures.append(f"{package}: expected {expected}, installed {installed}")
    if failures:
        print("Environment verification failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("Environment verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
