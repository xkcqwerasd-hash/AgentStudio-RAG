"""
Automated Verification Script for Phase 4 Acceptance Criteria (Backend & API layer)
"""
import io
import json
import zipfile
import requests

BASE_URL = "http://127.0.0.1:8000/api/v1"

def create_valid_docx(content="Phase 4 DOCX test content"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body><w:p><w:r><w:t>{content}</w:t></w:r></w:p></w:body>'
            '</w:document>'
        )
        zf.writestr("word/document.xml", doc_xml)
    return buf.getvalue()

def create_bomb_docx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w:document><w:body/></w:document>")
        for i in range(1005):
            zf.writestr(f"word/dummy_{i}.xml", "dummy")
    return buf.getvalue()

def run_tests():
    results = {}
    
    # 1. Test multi-file upload where one succeeds and one fails parsing
    print("--- Test 1: Multi-file upload with partial parse failure ---")
    conv1 = requests.post(f"{BASE_URL}/conversations", json={"title": "P4-Test-PartialFailure", "mode": "workflow"}).json()
    conv1_id = conv1["id"]
    
    valid_txt = ("file1.txt", b"Valid TXT document content for Phase 4 test.", "text/plain")
    bomb_docx = ("bomb.docx", create_bomb_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    
    files = [("files", valid_txt), ("files", bomb_docx)]
    res = requests.post(f"{BASE_URL}/conversations/{conv1_id}/attachments", files=files)
    assert res.status_code == 201, f"Expected 201, got {res.status_code}: {res.text}"
    att_data = res.json()
    assert len(att_data) == 2, f"Expected 2 attachments, got {len(att_data)}"
    
    status_map = {a["original_filename"]: a["parse_status"] for a in att_data}
    print(f"Attachment statuses: {status_map}")
    assert status_map["file1.txt"] == "COMPLETED", "file1.txt should be COMPLETED"
    assert status_map["bomb.docx"] == "FAILED", "bomb.docx should be FAILED"
    
    # Verify both are preserved in list
    list_res = requests.get(f"{BASE_URL}/conversations/{conv1_id}/attachments").json()
    assert len(list_res) == 2
    results["item1_multi_upload_partial_failure"] = "PASS"
    print("Test 1 PASS: Both files preserved, successful file is COMPLETED, failed file is FAILED.")

    # 4. Test cross-conversation isolation
    print("\n--- Test 4: Cross-conversation attachment isolation ---")
    conv2 = requests.post(f"{BASE_URL}/conversations", json={"title": "P4-Test-Isolation-Conv2", "mode": "workflow"}).json()
    conv2_id = conv2["id"]
    
    txt2 = ("conv2_doc.txt", b"Exclusive content for Conv2", "text/plain")
    res2 = requests.post(f"{BASE_URL}/conversations/{conv2_id}/attachments", files=[("files", txt2)])
    assert res2.status_code == 201
    
    atts1 = requests.get(f"{BASE_URL}/conversations/{conv1_id}/attachments").json()
    atts2 = requests.get(f"{BASE_URL}/conversations/{conv2_id}/attachments").json()
    
    names1 = [a["original_filename"] for a in atts1]
    names2 = [a["original_filename"] for a in atts2]
    print(f"Conv1 attachments: {names1}")
    print(f"Conv2 attachments: {names2}")
    assert "conv2_doc.txt" not in names1
    assert "file1.txt" not in names2
    assert "bomb.docx" not in names2
    results["item4_cross_conv_isolation"] = "PASS"
    print("Test 4 PASS: Attachments strictly isolated between conversations.")

    # 5. Test reload / persistence of attachments and status
    print("\n--- Test 5: Reload persistence of attachments and parse status ---")
    reloaded_atts = requests.get(f"{BASE_URL}/conversations/{conv1_id}/attachments").json()
    assert len(reloaded_atts) == 2
    for a in reloaded_atts:
        if a["original_filename"] == "file1.txt":
            assert a["parse_status"] == "COMPLETED"
            assert a["token_count"] > 0
        elif a["original_filename"] == "bomb.docx":
            assert a["parse_status"] == "FAILED"
            assert a["error_message"] is not None
    results["item5_reload_persistence"] = "PASS"
    print("Test 5 PASS: Attachments, parse status, token count, and error messages persisted.")

    # 6. Test file bounds and limit interceptions
    print("\n--- Test 6: Unsupported format, oversize, and 10 attachment limit ---")
    # 6a. Disallowed extension
    exe_file = ("evil.exe", b"binary content", "application/octet-stream")
    res_exe = requests.post(f"{BASE_URL}/conversations/{conv1_id}/attachments", files=[("files", exe_file)])
    assert res_exe.status_code == 400
    print(f"6a disallowed extension rejected: {res_exe.json()['detail']}")
    
    # 6b. Oversized file (>20MB)
    big_file = ("huge.txt", b"x" * (21 * 1024 * 1024), "text/plain")
    res_big = requests.post(f"{BASE_URL}/conversations/{conv1_id}/attachments", files=[("files", big_file)])
    assert res_big.status_code == 400
    print(f"6b oversized file rejected: {res_big.json()['detail']}")

    # 6c. Attachment count limit (10)
    conv3 = requests.post(f"{BASE_URL}/conversations", json={"title": "P4-Test-10-Limit", "mode": "workflow"}).json()
    conv3_id = conv3["id"]
    # Upload 9 files
    nine_files = [(f"files", (f"doc_{i}.txt", f"Content {i}".encode(), "text/plain")) for i in range(9)]
    res_9 = requests.post(f"{BASE_URL}/conversations/{conv3_id}/attachments", files=nine_files)
    assert res_9.status_code == 201
    assert len(res_9.json()) == 9
    
    # Now upload 2 more (9 + 2 = 11 > 10)
    two_files = [("files", ("doc_10.txt", b"10", "text/plain")), ("files", ("doc_11.txt", b"11", "text/plain"))]
    res_overflow = requests.post(f"{BASE_URL}/conversations/{conv3_id}/attachments", files=two_files)
    assert res_overflow.status_code == 400
    assert "超过上限限制" in res_overflow.json()["detail"]
    print(f"6c 10-file overflow intercepted: {res_overflow.json()['detail']}")
    results["item6_file_interceptions"] = "PASS"

    # 7. Test attachment detail & deletion
    print("\n--- Test 7: Attachment preview detail and deletion ---")
    file1_id = [a["id"] for a in att_data if a["original_filename"] == "file1.txt"][0]
    detail_res = requests.get(f"{BASE_URL}/conversations/{conv1_id}/attachments/{file1_id}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert "Valid TXT document content" in detail["extracted_text"]
    print(f"Preview extracted text length: {len(detail['extracted_text'])}, pages: {len(detail['parsed_pages'])}")
    
    # Delete file1
    del_res = requests.delete(f"{BASE_URL}/conversations/{conv1_id}/attachments/{file1_id}")
    assert del_res.status_code in (200, 204)
    remaining = requests.get(f"{BASE_URL}/conversations/{conv1_id}/attachments").json()
    assert len(remaining) == 1
    assert remaining[0]["original_filename"] == "bomb.docx"
    print("Test 7 PASS: Detail retrieved accurately and deletion works.")
    results["item7_preview_and_delete"] = "PASS"

    # Cleanup test conversations
    for cid in [conv1_id, conv2_id, conv3_id]:
        requests.delete(f"{BASE_URL}/conversations/{cid}")
    print("\nAll automated API tests completed successfully! Cleaned up test conversations.")
    return results

if __name__ == "__main__":
    run_tests()
