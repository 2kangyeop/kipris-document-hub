# GitHub 배포 방법

## 사용자가 받는 파일

사용자는 GitHub의 **Releases**에서 `KIPRIS_Document_Hub_vX.Y.Z.exe`만 내려받아 실행합니다. Python, 소스코드, `pip install`, 압축 해제는 필요하지 않습니다. KIPRIS 통합행정정보 처리를 위해 Chrome 또는 Edge는 설치되어 있어야 합니다.

## 최초 저장소 게시

1. 이 폴더를 공개 GitHub 저장소 `kipris-document-hub`에 올립니다.
2. 저장소의 **Actions** 탭에서 `Build Windows release`를 선택합니다.
3. `Run workflow`에서 버전을 입력하고 `publish_release`를 선택하면 시험 Release를 만들 수 있습니다.
4. 정식 배포는 아래처럼 버전 태그를 푸시합니다.

```bash
git tag v6.13.0
git push origin v6.13.0
```

태그가 올라오면 GitHub Actions가 Windows에서 테스트와 PyInstaller 빌드를 수행하고 다음 파일을 Release에 첨부합니다.

- `KIPRIS_Document_Hub_v6.13.0.exe`
- `KIPRIS_Document_Hub_Manual_v6.13.0.pdf`
- `SHA256SUMS.txt`

## 배포 전 확인

- Actions의 41개 단위 테스트가 모두 통과했는지 확인합니다.
- Release EXE를 별도의 Windows PC 또는 Windows Sandbox에서 실행합니다.
- 문헌 1건으로 공개공보, 통합행정정보, PDF 뷰어를 확인합니다.
- `SHA256SUMS.txt`와 EXE의 SHA-256이 일치하는지 확인합니다.
- Windows SmartScreen은 서명되지 않은 신규 EXE에 경고할 수 있습니다. 조직 배포 전에는 신뢰할 수 있는 코드서명 인증서로 서명하는 것이 권장됩니다.

