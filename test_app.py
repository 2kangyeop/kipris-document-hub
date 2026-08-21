import base64
import tempfile
import unittest
import xml.etree.ElementTree as ET
import struct
import zipfile
from pathlib import Path

import pymupdf

from app import (
    RENDERED_FALLBACK_MARKER,
    KiprisAdminDownloader,
    _document_payload_suffix,
    extract_viewer_page_count,
    is_rendered_fallback_pdf,
)
from api_client import (
    AdminDocument,
    KiprisApiClient,
    _admin_pdf_service,
    _extract_google_pdf_url,
    _find_text,
    _parse_admin_documents,
    _redact_secrets,
    _normalize_download_url,
    _validate_remote_url,
)
from core import (
    AMENDMENT_OPINION_PATTERNS,
    NumberItem,
    blob_url_from_iframe_src,
    canonical_document_label,
    document_pdf_filename,
    is_downloadable_admin_control,
    is_excluded_admin_document_title,
    kipris_document_candidate_url,
    load_number_file,
    normalize_identifier,
    normalize_number,
    parse_open_document_call,
    parse_text_items,
    safe_error_text,
    safe_filename,
)
from pdf_tools import (
    convert_document_payload_to_pdf,
    extract_pdf_text,
    list_folder_pdf_files,
    list_pdf_files,
    list_pdf_folders,
    merge_pdfs,
    render_pdf_page,
)


class ParserTests(unittest.TestCase):
    def test_local_web_app_assets_are_packaged(self):
        root = Path(__file__).parent
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        backend = (root / "web_app.py").read_text(encoding="utf-8")
        self.assertIn("__SESSION_TOKEN__", html)
        self.assertIn("X-KIPRIS-Token", script)
        self.assertIn('host="127.0.0.1"', backend)
        self.assertIn("Content-Security-Policy", backend)
        self.assertNotIn("from app import (", backend)
        self.assertIn('/api/delete-files', backend)
        self.assertIn('/api/delete-folder', backend)
        self.assertIn('/api/choose-external-pdfs', backend)
        self.assertIn('/api/extract-external', backend)
        self.assertIn("SHFileOperationW", backend)
        self.assertIn("externalExtractButton", html)
        self.assertIn("clearNumbersButton", html)
        self.assertIn("문헌번호 입력란을 지웠습니다.", script)
        self.assertIn("$('numbers').value = '';", script)
        self.assertIn("preview_folder", backend)
        self.assertIn("preview_file", backend)
        self.assertIn("state.lastRunning = true", script)
        self.assertIn("await refreshFolders(result.preview_folder", script)
        self.assertIn("OPD 국제심사정보", html)
        self.assertIn("Espacenet 특허검색", html)
        self.assertIn("noopener noreferrer", html)
        installer = (root / "install_windows.bat").read_text(encoding="utf-8")
        self.assertIn("Required packages are already installed", installer)
        self.assertIn("access_log=False", backend)
        self.assertIn("use_colors=False", backend)
        self.assertNotIn("setInterval(pollStatus", script)
        self.assertIn("result.running ? 800 : 4000", script)
        self.assertIn("min-height: 0", (root / "web" / "app.css").read_text(encoding="utf-8"))
        css = (root / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".library-card { min-height: 260px", css)
        self.assertIn("font: 600 14.5px/1.15 Consolas", css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", css)
        self.assertTrue((root / "launch_web.py").is_file())
        self.assertTrue((root / "preview_windows.bat").is_file())

    def test_rocket_background_asset(self):
        asset = Path(__file__).with_name("assets") / "rocket_background.png"
        self.assertTrue(asset.is_file())
        data = asset.read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", data[16:24])
        self.assertGreater(width, height)
        self.assertGreaterEqual(width, 1600)

    def test_rendered_fallback_pdf_is_retried(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rendered.pdf"
            path.write_bytes(b"%PDF-1.4\n%%EOF\n" + RENDERED_FALLBACK_MARKER)
            self.assertTrue(is_rendered_fallback_pdf(path))
            path.write_bytes(b"%PDF-1.4\n%%EOF")
            self.assertFalse(is_rendered_fallback_pdf(path))

    def test_viewer_page_count_ignores_dates(self):
        self.assertEqual(extract_viewer_page_count("페이지 1 / 7"), 7)
        self.assertEqual(extract_viewer_page_count("작성일 2025/07/28"), 0)
        self.assertEqual(extract_viewer_page_count("", [None, "12"]), 12)

    def test_document_payload_signatures(self):
        self.assertEqual(_document_payload_suffix(b"%PDF-1.7"), ".pdf")
        self.assertEqual(_document_payload_suffix(b"II*\x00payload"), ".tiff")
        self.assertEqual(_document_payload_suffix(b"\x89PNG\r\n\x1a\n"), ".png")

    def test_truncated_popup_print_is_not_saved(self):
        class DummyLocator:
            def inner_text(self, timeout=0):
                return "의견서 문서 내용이 충분히 표시되어 인쇄할 수 있는 상태입니다."

            def count(self):
                return 0

        class DummyPage:
            def wait_for_load_state(self, *args, **kwargs):
                return None

            def wait_for_timeout(self, *_):
                return None

            def locator(self, selector):
                return DummyLocator()

            def emulate_media(self, **kwargs):
                return None

            def pdf(self, path, **kwargs):
                document = pymupdf.open()
                document.new_page()
                document.save(path)
                document.close()

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "opinion.pdf"
            downloader = KiprisAdminDownloader(Path(temp_dir), lambda _: None)
            with self.assertRaisesRegex(RuntimeError, "1/3페이지"):
                downloader._save_rendered_popup_pdf(
                    DummyPage(), destination, expected_pages=3
                )
            self.assertFalse(destination.exists())

    def test_normalize_number(self):
        self.assertEqual(normalize_number("10-2021-0171607"), "1020210171607")

    def test_parse_mixed_input(self):
        items = parse_text_items(
            "출원번호,10-2021-0171607\n"
            "공개번호\t10-2023-0076543\n"
            "US-2023-0123456-A1"
        )
        self.assertEqual(
            items,
            [
                NumberItem("1020210171607", "application"),
                NumberItem("1020230076543", "publication"),
                NumberItem("US20230123456A1", "auto"),
            ],
        )

    def test_parse_multiple_publication_numbers_and_a_suffix(self):
        items = parse_text_items(
            "공개 1020250012345A, 1020250067890A; US20230123456A1\n"
            "EP1731204A1\tWO2020123456A1"
        )
        self.assertEqual(
            items,
            [
                NumberItem("1020250012345", "publication"),
                NumberItem("1020250067890", "publication"),
                NumberItem("US20230123456A1", "publication"),
                NumberItem("EP1731204A1", "auto"),
                NumberItem("WO2020123456A1", "auto"),
            ],
        )
    def test_input_item_limit(self):
        rows = "\n".join(f"1020230{index:06d}" for index in range(501))
        with self.assertRaises(ValueError):
            parse_text_items(rows)

    def test_foreign_parts(self):
        item = NumberItem("EP1731204A1")
        self.assertEqual(item.foreign_parts, ("EP", "1731204", "A1"))
        self.assertFalse(item.is_domestic)
        self.assertTrue(NumberItem("KR1020230010262A").is_domestic)

    def test_normalize_foreign_identifier(self):
        self.assertEqual(normalize_identifier("WO 2020/123456 A1"), "WO2020123456A1")

    def test_parse_foreign_publication_and_registration_numbers(self):
        self.assertEqual(
            parse_text_items(
                "공개: JP 2002-087733 A\n"
                "등록: US 11 234 567 B2\n"
                "grant: CN-115123456-B"
            ),
            [
                NumberItem("JP2002087733A", "publication"),
                NumberItem("US11234567B2", "registration"),
                NumberItem("CN115123456B", "registration"),
            ],
        )

    def test_load_csv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "numbers.csv"
            path.write_text("구분,번호\n출원번호,10-2021-0171607\n", encoding="utf-8")
            self.assertEqual(load_number_file(path), [NumberItem("1020210171607", "application")])

    def test_blob_url(self):
        src = "/viewer.html?file=blob%3Ahttps%3A%2F%2Fwww.kipris.or.kr%2Fabcd%23sidebarView%3D0#page=1"
        self.assertEqual(blob_url_from_iframe_src(src), "blob:https://www.kipris.or.kr/abcd")

    def test_kipris_popup_resource_filter_excludes_tracker_and_html_shell(self):
        base = "https://www.kipris.or.kr/khome/detail/document.do"
        self.assertIsNone(
            kipris_document_candidate_url(
                "https://gptrk.logger.co.kr/tracker/image.gif", base
            )
        )
        self.assertIsNone(kipris_document_candidate_url(base, base))
        self.assertEqual(
            kipris_document_candidate_url(
                "/khome/original/downloadFile.do?id=123", base
            ),
            "https://www.kipris.or.kr/khome/original/downloadFile.do?id=123",
        )
        self.assertEqual(
            kipris_document_candidate_url("blob:https://www.kipris.or.kr/abcd", base),
            "blob:https://www.kipris.or.kr/abcd",
        )
        self.assertIsNone(
            kipris_document_candidate_url("blob:https://example.com/abcd", base)
        )

    def test_safe_filename(self):
        self.assertEqual(safe_filename('20260820_의견:제출/통지서?.pdf'), "20260820_의견_제출_통지서_.pdf")
        long_name = safe_filename("가" * 200 + ".pdf", limit=120)
        self.assertTrue(long_name.endswith(".pdf"))
        self.assertLessEqual(len(long_name), 120)

    def test_browser_error_secrets_are_redacted(self):
        error = (
            "GET https://www.kipris.or.kr/a?accessKey=top-secret\n"
            "  - cookie: JSESSIONID=session-secret; _TRK_UID=tracking-secret\n"
            "self-signed certificate in certificate chain"
        )
        cleaned = safe_error_text(error)
        self.assertNotIn("top-secret", cleaned)
        self.assertNotIn("session-secret", cleaned)
        self.assertNotIn("tracking-secret", cleaned)
        self.assertIn("Chrome 세션 내부 다운로드", cleaned)

    def test_standard_document_filenames(self):
        self.assertEqual(canonical_document_label("[특허출원]특허출원서"), "출원서")
        self.assertEqual(
            document_pdf_filename("1020230104168", "의견제출통지서"),
            "1020230104168_의견제출통지서.pdf",
        )
        self.assertEqual(
            document_pdf_filename("1020230104168", "[명세서등 보정]보정서", 2, 3),
            "1020230104168_보정서_2.pdf",
        )

    def test_remote_url_security(self):
        _validate_remote_url("https://www.kipris.or.kr/khome/detail/document.do")
        _validate_remote_url("https://patentimages.storage.googleapis.com/a.pdf")
        with self.assertRaises(RuntimeError):
            _validate_remote_url("http://www.kipris.or.kr/a.pdf")
        with self.assertRaises(RuntimeError):
            _validate_remote_url("https://127.0.0.1/a.pdf")
        self.assertNotIn(
            "secret-value",
            _redact_secrets("x?accessKey=secret-value&applicationNumber=1"),
        )

    def test_api_http_pdf_url_is_upgraded_only_for_allowed_hosts(self):
        self.assertEqual(
            _normalize_download_url("http://plus.kipris.or.kr/files/a.pdf"),
            "https://plus.kipris.or.kr/files/a.pdf",
        )
        with self.assertRaises(RuntimeError):
            _normalize_download_url("http://example.com/a.pdf")
        with self.assertRaises(RuntimeError):
            _normalize_download_url("https://example.com/a.pdf")

    def test_amendment_and_opinion_patterns(self):
        self.assertTrue(any(pattern in "명세서등 보정서" for pattern in AMENDMENT_OPINION_PATTERNS))
        self.assertTrue(any(pattern in "의견서" for pattern in AMENDMENT_OPINION_PATTERNS))
        self.assertFalse(
            any(pattern in "의견제출통지서" for pattern in AMENDMENT_OPINION_PATTERNS)
        )

    def test_application_form_amendment_is_excluded_only(self):
        self.assertTrue(is_excluded_admin_document_title("[출원서등 보정]보정서"))
        self.assertTrue(is_excluded_admin_document_title("[출원서 등 보정] 보정서"))
        self.assertFalse(is_excluded_admin_document_title("[명세서등 보정]보정서"))
        self.assertFalse(is_excluded_admin_document_title("의견제출통지서"))

    def test_admin_document_controls_accept_href_and_onclick(self):
        self.assertTrue(
            is_downloadable_admin_control(
                "최초의견제출통지서", "", "openDocument('123')", "", "2024.01.01"
            )
        )
        self.assertTrue(
            is_downloadable_admin_control(
                "보정서", "javascript:fnView('123')", "", "", "2024.01.02"
            )
        )
        self.assertFalse(
            is_downloadable_admin_control("접수번호", "#", "", "", "1020240000001")
        )

    def test_blue_admin_anchor_from_attached_screen_is_downloadable(self):
        self.assertTrue(
            is_downloadable_admin_control(
                "[특허출원]특허원서",
                "#",
                "",
                "",
                "[특허출원]특허원서 2023.08.09 수리",
                "a",
            )
        )
        self.assertTrue(
            is_downloadable_admin_control(
                "[거절이유 등 통지에 따른 의견]의견서·답변서·소명서",
                "javascript:void(0)",
                "fnOpen('123')",
                "",
                "2025.07.28 수리",
                "a",
            )
        )

    def test_parse_open_document_call_from_opinion_link(self):
        parsed = parse_open_document_call(
            "javascript:openDocument('1020230104168','112025085522698',"
            "'1013201', '');"
        )
        self.assertEqual(
            parsed,
            ("1020230104168", "112025085522698", "1013201", ""),
        )

    def test_parse_open_document_call_from_onclick(self):
        parsed = parse_open_document_call(
            "",
            'openDocument("1020230104168", "112025085522733", "1013101", "")',
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[1], "112025085522733")

    def test_xml_find_text_ignores_case_and_namespace(self):
        root = ET.fromstring(
            "<response xmlns='urn:test'><body><Item><ApplicationNumber>"
            "1020230000035</ApplicationNumber></Item></body></response>"
        )
        self.assertEqual(_find_text(root, "applicationNumber"), "1020230000035")

    def test_parse_admin_history_documents(self):
        root = ET.fromstring(
            """<response><body><items>
            <relateddocsonfileInfo>
              <applicationNumber>10-2023-0104168</applicationNumber>
              <documentNumber>952025050273774</documentNumber>
              <documentDate>20250527</documentDate>
              <documentTitle>의견제출통지서</documentTitle>
              <status>발송처리완료</status>
            </relateddocsonfileInfo>
            <relateddocsonfileInfo>
              <applicationNumber>1020230104168</applicationNumber>
              <documentNumber>112025085522733</documentNumber>
              <documentDate>20250728</documentDate>
              <documentTitle>[명세서등 보정]보정서</documentTitle>
              <status>보정승인간주</status>
            </relateddocsonfileInfo>
            </items></body></response>"""
        )
        documents = _parse_admin_documents(root)
        self.assertEqual(len(documents), 2)
        self.assertEqual(documents[0].application_number, "1020230104168")
        self.assertEqual(documents[0].document_number, "952025050273774")
        self.assertEqual(documents[0].title, "의견제출통지서")

    def test_admin_pdf_service_mapping(self):
        self.assertEqual(
            _admin_pdf_service("최후의견제출통지서"),
            "IntermediateDocumentOPService",
        )
        self.assertEqual(
            _admin_pdf_service("특허거절결정서"),
            "IntermediateDocumentREService",
        )
        self.assertEqual(_admin_pdf_service("[명세서등 보정]보정서"), "")

    def test_api_admin_list_does_not_schedule_application_form_amendment(self):
        client = KiprisApiClient("test-key", True, lambda _message: None)
        client._resolve_admin_application_number = lambda _item: "1020230104168"
        client._admin_document_list = lambda _number: [
            AdminDocument(
                application_number="1020230104168",
                document_number="112023000000001",
                document_date="20230101",
                title="[출원서등 보정]보정서",
                status="수리",
            ),
            AdminDocument(
                application_number="1020230104168",
                document_number="112023000000002",
                document_date="20230102",
                title="[명세서등 보정]보정서",
                status="수리",
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            saved, _, unsupported, failures = client.download_admin_documents_api(
                NumberItem("1020230104168", "application"), Path(temp_dir)
            )
        self.assertEqual(saved, 0)
        self.assertEqual(unsupported, 1)
        self.assertEqual(failures, [])

    def test_pdf_v2_uses_application_and_send_number(self):
        client = KiprisApiClient("test-key", False, lambda _: None)
        calls = []

        def fake_request_xml(url, params):
            calls.append((url, params))
            return ET.fromstring(
                "<response><body><item><filePath>https://example.test/a.pdf"
                "</filePath></item></body></response>"
            )

        client._request_xml = fake_request_xml
        document = AdminDocument(
            application_number="1020230104168",
            document_number="952025050273774",
            document_date="20250527",
            title="의견제출통지서",
            status="발송처리완료",
        )
        path = client._admin_pdf_path(document, "IntermediateDocumentOPService")
        self.assertEqual(path, "https://example.test/a.pdf")
        self.assertTrue(calls[0][0].endswith("/IntermediateDocumentOPService/pdfInfoV2"))
        self.assertEqual(calls[0][1]["applicationNumber"], "1020230104168")
        self.assertEqual(calls[0][1]["sendNumber"], "952025050273774")

    def test_publication_uses_standard_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KiprisApiClient("test-key", False, lambda _: None)
            saved = []
            client._domestic_pdf_path = lambda _: ("https://plus.kipris.or.kr/a.pdf", "old.pdf")
            client._save_pdf = lambda url, destination: saved.append((url, destination))
            result = client._download_domestic(
                NumberItem("1020230104168", "application"), Path(temp_dir)
            )
            self.assertEqual(result.destination.name, "1020230104168_공개공보.pdf")
            self.assertEqual(saved[0][1], result.destination)

    def test_foreign_publication_uses_google_first_and_kipris_as_backup(self):
        item = NumberItem("US20230123456A1")
        client = KiprisApiClient("test-key", True, lambda _: None)
        expected = object()
        calls = []

        def google_first(*_):
            calls.append("google")
            return expected

        def kipris_should_not_run(*_):
            calls.append("kipris")
            raise AssertionError("Google 성공 후 KIPRISPlus를 호출하면 안 됩니다.")

        client._download_google = google_first
        client._download_foreign_kipris = kipris_should_not_run
        self.assertIs(client.download_publication(item, Path(".")), expected)
        self.assertEqual(calls, ["google"])

        calls.clear()
        client._download_google = lambda *_: (_ for _ in ()).throw(
            RuntimeError("Google PDF 없음")
        )
        client._download_foreign_kipris = lambda *_: (calls.append("kipris"), expected)[1]
        self.assertIs(client.download_publication(item, Path(".")), expected)
        self.assertEqual(calls, ["kipris"])

    def test_google_pdf_url_attribute_order_and_direct_fallback(self):
        self.assertEqual(
            _extract_google_pdf_url(
                '<meta content="https://patentimages.storage.googleapis.com/a.pdf" '
                'name="citation_pdf_url">'
            ),
            "https://patentimages.storage.googleapis.com/a.pdf",
        )
        self.assertEqual(
            _extract_google_pdf_url(
                'window.pdf="https:\\/\\/patentimages.storage.googleapis.com\\/b.pdf"'
            ),
            "https://patentimages.storage.googleapis.com/b.pdf",
        )

    def test_japanese_publication_uses_kipris_literature_number_lookup(self):
        client = KiprisApiClient("test-key", True, lambda _: None)
        calls = []

        def request_xml(url, params):
            calls.append((url, params))
            return ET.fromstring(
                "<response><body><items><item>"
                "<ltrtno>200200087733A0</ltrtno>"
                "</item></items></body></response>"
            )

        client._request_xml = request_xml
        self.assertEqual(
            client._foreign_literature_number(NumberItem("JP2002087733A")),
            ("JP", "200200087733A0"),
        )
        self.assertEqual(calls[0][1]["openNumber"], "2002087733A")
        self.assertEqual(calls[0][1]["collectionValues"], "JP")

    def test_foreign_lookup_keeps_requested_grant_kind(self):
        client = KiprisApiClient("test-key", True, lambda _: None)
        calls = []

        def request_xml(_url, params):
            calls.append(params["openNumber"])
            ltrtno = (
                "000115123456A0"
                if params["openNumber"].endswith("B")
                else "000115123456B0"
            )
            return ET.fromstring(f"<response><ltrtno>{ltrtno}</ltrtno></response>")

        client._request_xml = request_xml
        self.assertEqual(
            client._foreign_literature_number(NumberItem("CN115123456B")),
            ("CN", "000115123456B0"),
        )
        self.assertEqual(calls, ["115123456B", "115123456"])

    def test_foreign_gazette_labels_publication_and_registration(self):
        client = KiprisApiClient("test-key", True, lambda _: None)
        self.assertEqual(
            client._foreign_document_label(NumberItem("US20230123456A1")),
            "공개공보",
        )
        self.assertEqual(
            client._foreign_document_label(NumberItem("US11234567B2")),
            "등록공보",
        )
        self.assertEqual(
            client._foreign_document_label(NumberItem("CN115123456B")),
            "등록공보",
        )

    def test_japanese_publication_literature_number_fallback(self):
        client = KiprisApiClient("test-key", True, lambda _: None)
        client._request_xml = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("lookup unavailable")
        )
        self.assertEqual(
            client._foreign_literature_number(NumberItem("JP2002087733A")),
            ("JP", "200200087733A0"),
        )

    def test_pdf_tools_list_preview_merge_and_extract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_path = root / "first.pdf"
            nested = root / "nested"
            nested.mkdir()
            empty_literature_folder = root / "1020250012345"
            empty_literature_folder.mkdir()
            second_path = nested / "second.pdf"
            for path, text in ((first_path, "Alpha"), (second_path, "Beta")):
                document = pymupdf.open()
                page = document.new_page()
                page.insert_text((72, 72), text)
                document.save(path)
                document.close()

            self.assertEqual(list_pdf_files(root), [first_path, second_path])
            self.assertEqual(
                set(list_pdf_folders(root)),
                {root, nested, empty_literature_folder},
            )
            self.assertEqual(list_folder_pdf_files(root), [first_path])
            image, page_count, page_number = render_pdf_page(first_path, 0, 240, 240)
            self.assertEqual(page_count, 1)
            self.assertEqual(page_number, 0)
            self.assertLessEqual(image.width, 240)
            self.assertLessEqual(image.height, 240)
            height_image, _, _ = render_pdf_page(
                first_path,
                0,
                max_width=1000,
                max_height=180,
                fit_height=True,
            )
            self.assertGreaterEqual(height_image.height, 175)
            self.assertLessEqual(height_image.height, 180)

            merged_path = root / "merged.pdf"
            self.assertEqual(merge_pdfs([first_path, second_path], merged_path), 2)
            with pymupdf.open(merged_path) as merged:
                self.assertEqual(merged.page_count, 2)
            extracted = extract_pdf_text([first_path, second_path])
            self.assertIn("Alpha", extracted)
            self.assertIn("Beta", extracted)

    def test_foreign_image_payload_and_zip_are_converted_to_pdf(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_payload = root / "image.payload"
            image_payload.write_bytes(png)
            image_pdf = root / "image.pdf"
            payload_type, pages = convert_document_payload_to_pdf(
                image_payload, image_pdf
            )
            self.assertEqual(payload_type, "PNG 원문")
            self.assertEqual(pages, 1)
            self.assertEqual(image_pdf.read_bytes()[:5], b"%PDF-")

            zip_payload = root / "pages.payload"
            with zipfile.ZipFile(zip_payload, "w") as archive:
                archive.writestr("page_2.png", png)
                archive.writestr("page_1.png", png)
            zip_pdf = root / "pages.pdf"
            payload_type, pages = convert_document_payload_to_pdf(
                zip_payload, zip_pdf
            )
            self.assertEqual(payload_type, "ZIP 이미지 원문")
            self.assertEqual(pages, 2)
            self.assertEqual(zip_pdf.read_bytes()[:5], b"%PDF-")


if __name__ == "__main__":
    unittest.main()
