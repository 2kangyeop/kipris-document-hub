# Security policy

## Supported version

Security fixes are applied to the latest GitHub Release only. Users should download the newest executable from this repository's Releases page.

## Credential handling

- Do not commit a KIPRISPlus REST AccessKey to this repository, an issue, a log, or a screenshot.
- The application receives the AccessKey in its local settings screen and stores it through Windows Credential Manager.
- Release builds are created by GitHub Actions from the committed source. No AccessKey is embedded in the executable.
- Downloaded patent documents remain on the user's PC. The local web server listens only on `127.0.0.1`.

## Verifying a download

Every Release contains `SHA256SUMS.txt`. In PowerShell, compare it with:

```powershell
Get-FileHash .\KIPRIS_Document_Hub_v6.13.0.exe -Algorithm SHA256
```

## Reporting a vulnerability

Do not post credentials, personal information, confidential patent documents, or a proof of concept containing them in a public issue. Report only a minimized reproduction with secrets removed to the repository maintainer through an approved internal contact channel.

