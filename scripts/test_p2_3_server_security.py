import urllib.request
import urllib.error
import json
import time

BASE = "http://localhost:8080/api/v1"

def req(url, method="GET", data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    b = json.dumps(data).encode("utf-8") if data else None
    r = urllib.request.Request(f"{BASE}{url}", data=b, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=15) as resp:
        content = resp.read().decode("utf-8")
        return json.loads(content) if content.strip() else {}

def main():
    print("=== AgentStudio-RAG Server Mode Security & IDOR Verification ===")
    ts = int(time.time())

    # 1. Register User A
    uA = f"user_a_{ts}"
    req("/auth/register", "POST", {"username": uA, "password": "OldPassword123!"})
    tokA = req("/auth/login", "POST", {"username": uA, "password": "OldPassword123!"})["access_token"]
    print("[1] User A registered and logged in successfully.")

    # 2. Change User A password
    req("/auth/password", "POST", {"old_password": "OldPassword123!", "new_password": "NewPassword123!"}, token=tokA)
    print("[2] User A changed password to NewPassword123!.")

    # 3. Verify old password fails
    try:
        req("/auth/login", "POST", {"username": uA, "password": "OldPassword123!"})
        raise AssertionError("Old password should have been rejected!")
    except urllib.error.HTTPError as e:
        assert e.code == 401
        print("[3] Old password rejected with HTTP 401 as expected.")

    # 4. Verify new password succeeds
    tokA_new = req("/auth/login", "POST", {"username": uA, "password": "NewPassword123!"})["access_token"]
    print("[4] New password login succeeded.")

    # 5. User A creates private conversation & KB
    convA = req("/conversations", "POST", {"title": "User A Confidential Chat"}, token=tokA_new)
    kbA = req("/knowledge-bases", "POST", {"name": "User A Confidential KB"}, token=tokA_new)
    conv_id = convA["id"]
    kb_id = kbA["id"]
    print(f"[5] User A created private resources: conv={conv_id}, kb={kb_id}")

    # 6. Register User B
    uB = f"user_b_{ts}"
    req("/auth/register", "POST", {"username": uB, "password": "Password123!"})
    tokB = req("/auth/login", "POST", {"username": uB, "password": "Password123!"})["access_token"]
    print(f"[6] User B registered and logged in ({uB}).")

    # 7. User B attempts IDOR on User A resources
    try:
        req(f"/conversations/{conv_id}", "GET", token=tokB)
        raise AssertionError("User B was able to access User A conversation!")
    except urllib.error.HTTPError as e:
        assert e.code in (403, 404), f"Unexpected HTTP status: {e.code}"
        print(f"[7a] IDOR Defended: User B denied access to User A conversation (HTTP {e.code}).")

    try:
        req(f"/knowledge-bases/{kb_id}", "GET", token=tokB)
        raise AssertionError("User B was able to access User A knowledge base!")
    except urllib.error.HTTPError as e:
        assert e.code in (403, 404), f"Unexpected HTTP status: {e.code}"
        print(f"[7b] IDOR Defended: User B denied access to User A knowledge base (HTTP {e.code}).")

    # Clean up
    req(f"/conversations/{conv_id}", "DELETE", token=tokA_new)
    req(f"/knowledge-bases/{kb_id}", "DELETE", token=tokA_new)
    print("[8] Private test resources cleaned up.")

    print("\n[VERDICT: PASS] Multi-tenant password lifecycle & IDOR defense verified successfully.")

if __name__ == "__main__":
    main()
