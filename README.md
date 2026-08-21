# KIPRIS Document Hub

<p align="center">
  <img src="assets/kipris_document_hub_banner.jpg" alt="KIPRIS Document Hub v6.13 - 특허 심사문서 자동 수집 및 PDF 검토" width="100%">
</p>

<p align="center">
  <strong>국내·외 특허공보와 KIPRIS 통합행정정보 문서를 한 화면에서 자동 수집·검토하는 Windows 도구</strong>
</p>

<p align="center">
  <a href="https://github.com/2kangyeop/kipris-document-hub/releases/tag/v6.13.0"><strong>⬇️ KIPRIS Document Hub v6.13.0 다운로드</strong></a>
  ·
  <a href="docs/KIPRIS_Document_Hub_Manual.pdf"><strong>📘 설치 및 사용설명서</strong></a>
  ·
  <a href="https://github.com/2kangyeop/kipris-document-hub/releases"><strong>📦 모든 Releases</strong></a>
</p>

---

## 가장 쉬운 설치 방법

### 1. EXE 다운로드

위의 **KIPRIS Document Hub v6.13.0 다운로드**를 눌러 Release 페이지에서

`KIPRIS_Document_Hub_v6.13.0.exe`

를 내려받습니다.

### 2. 실행

다운로드한 EXE를 더블클릭합니다.

- Python 설치 불필요
- 소스코드 설치 불필요
- `pip install` 불필요
- Google Chrome 또는 Microsoft Edge 필요

처음 실행할 때 Windows SmartScreen 경고가 나타날 수 있습니다. 이는 코드서명되지 않은 신규 EXE에서 흔히 발생합니다.

### 3. KIPRISPlus REST AccessKey 등록

프로그램의 **다운로드 설정 → 설정 → REST AccessKey 입력 → 키 저장** 순서로 등록합니다.

AccessKey는 Windows 자격 증명 저장소에 보관되며, 배포 파일이나 일반 설정 파일에 저장되지 않습니다.

---

## 주요 기능

- 여러 문헌번호를 직접 입력하거나 TXT·CSV·XLSX에서 불러와 최대 500건 일괄 처리
- 국내 공개·등록공보 자동 수집
- 국내 출원의 출원서, 명세서등 보정서, 의견서, 의견제출통지서, 거절·등록결정서 등 주요 통합행정정보 서류 자동 수집
- US·JP·CN·EP·WO 등 외국 공개·등록공보 지원
- Google Patents 우선 다운로드 후 KIPRISPlus로 보완
- 다운로드된 PDF를 프로그램 화면에서 바로 열람
- 선택 PDF 병합 및 파일명 지정
- PDF 텍스트 추출 및 복사
- 외부 PDF 최대 100개 텍스트 추출
- 선택 PDF 또는 문헌 폴더를 Windows 휴지통으로 안전하게 이동
- 다운로드 실패 내역과 전체 처리 결과를 CSV로 기록

---

## 지원 범위

| 구분 | 지원 내용 |
|---|---|
| 국내 공보 | 공개공보·등록공보 |
| 국내 행정서류 | 출원서, 명세서등 보정서, 의견서, 의견제출통지서, 거절결정서, 등록결정서 등 원문 링크가 제공되는 문서 |
| 외국 공보 | US·JP·CN·EP·WO 등 공개·등록공보 |
| PDF 검토 | 내장 뷰어, 병합, 텍스트 추출 |

> `[명세서등 보정]보정서`는 지원하지만 `[출원서등 보정]보정서`는 의도적으로 제외합니다. 외국 출원의 출원서·심사서류는 지원하지 않습니다.

---

## KIPRISPlus 준비

KIPRISPlus에서 필요한 데이터 상품을 신청하고 승인받은 뒤 REST AccessKey를 사용합니다.

권장 신청 항목:

- 특허·실용 공개·등록공보
- 특허·실용 행정처리 이력
- 의견제출통지서
- 거절결정서
- 해외특허 — 외국공보 사용 시

프로그램에는 KIPRISPlus 아이디와 비밀번호를 입력하지 않습니다.

---

## 배포판 정보

현재 배포 버전: **v6.13.0**

GitHub Actions가 Windows 환경에서 자동으로 테스트와 PyInstaller 빌드를 수행하고 Release 파일을 생성합니다.

배포 파일에는 다음이 포함됩니다.

- `KIPRIS_Document_Hub_v6.13.0.exe`
- 사용설명서 PDF
- `SHA256SUMS.txt`

현재 자동 빌드에서는 전체 단위 테스트가 통과한 후에만 EXE와 Release가 생성됩니다.

---

## 보안 및 안정성

- UI는 `127.0.0.1` 로컬 서버에서만 실행
- 실행마다 임의 세션 토큰 생성
- REST AccessKey는 Windows 자격 증명 저장소 사용
- 브라우저 화면으로 AccessKey를 전달하지 않음
- 외부 다운로드는 허용된 특허 원문 서버와 HTTPS를 우선 사용
- 임시 파일 검증 후 최종 파일로 교체하여 중단 시 기존 정상 파일 보호
- ZIP·이미지 묶음 원문은 파일 수·해제 용량·압축률을 검사 후 처리

---

## 자세한 설명서

설치, AccessKey 준비, 번호 입력 형식, 문서 다운로드, PDF 병합·텍스트 추출, 문제 해결은 아래 설명서를 참고하세요.

**[KIPRIS Document Hub 설치 및 사용설명서 열기](docs/KIPRIS_Document_Hub_Manual.pdf)**

---

## 개발·배포

개발 또는 진단 목적의 소스 실행 방법과 Release 절차는 다음 문서를 참고하세요.

- [RELEASE.md](RELEASE.md)
- [SECURITY.md](SECURITY.md)

일반 사용자에게는 소스 ZIP보다 **Releases의 단독 실행형 EXE** 사용을 권장합니다.
