import sys
sys.stdout.reconfigure(encoding="utf-8")

import time
import requests
import auth

BASE_URL = "http://localhost:8000"


def test_full_system():
    print("==========================================================")
    print("   RUNNING PRODUCTION SECURITY & INTEGRATION TEST SUITE   ")
    print("==========================================================")

    # 1. Test 3-Stage Password Security Unit Logic
    print("\n[1/8] Testing 3-Stage Security Hashing Pipeline (SHA256 -> Salt -> Bcrypt)...")
    raw_password = "SuperStrongUserPassword2026!@#$%^&*()"
    salt_hex, bcrypt_hash = auth.hash_password_3stage(raw_password)

    assert len(salt_hex) == 32, "Salt hex length should be 32 chars (16 bytes)"
    assert bcrypt_hash.startswith("$2b$12$"), f"Hash does not match Bcrypt format with rounds=12: {bcrypt_hash}"
    assert auth.verify_password(raw_password, salt_hex, bcrypt_hash), "3-Stage password verification failed for correct password!"
    assert not auth.verify_password("WrongPassword123", salt_hex, bcrypt_hash), "3-Stage password verification accepted wrong password!"
    print(" -> SUCCESS: 3-Stage Pipeline (SHA-256 + Salt + Bcrypt rounds=12) validated.")

    # 2. Test Security Headers
    print("\n[2/8] Testing OWASP HTTP Security Headers...")
    r_root = requests.get(f"{BASE_URL}/")
    assert r_root.status_code == 200
    assert r_root.headers.get("X-Content-Type-Options") == "nosniff", "Missing X-Content-Type-Options: nosniff"
    assert r_root.headers.get("X-Frame-Options") == "DENY", "Missing X-Frame-Options: DENY"
    assert "default-src" in r_root.headers.get("Content-Security-Policy", ""), "Missing CSP header"
    print(" -> SUCCESS: OWASP security headers (CSP, X-Frame-Options, nosniff) present.")

    # 3. Test Registration & SQLite Storage
    test_user = f"sqlite_user_{int(time.time())}"
    test_pass = "ComplexPassw0rd!"

    print(f"\n[3/8] Testing User Registration ('{test_user}')...")
    r = requests.post(f"{BASE_URL}/api/register", json={"username": test_user, "password": test_pass})
    assert r.status_code == 200, f"Registration failed: {r.text}"
    reg_data = r.json()
    assert reg_data["status"] == "success"
    token = reg_data["token"]
    print(" -> SUCCESS: Registration passed, persistent SQLite session issued.")

    # Verify SQLite schema & user record
    with auth.get_db_connection() as conn:
        row = conn.execute("SELECT username, hash FROM users WHERE username = ?", (test_user,)).fetchone()
        assert row is not None, "User record not found in SQLite database!"
        assert row["hash"].startswith("$2b$12$"), "Hash not stored in Bcrypt 12 format!"
        print(f" -> SUCCESS: User stored in SQLite DB with 3-Stage hash: {row['hash'][:25]}...")

    # 4. Test Login & Invalid Login Security
    print("\n[4/8] Testing Security & Login...")
    r_bad = requests.post(f"{BASE_URL}/api/login", json={"username": test_user, "password": "WrongPassword"})
    assert r_bad.status_code == 401, "Security failure: Invalid password was accepted!"
    print(" -> SUCCESS: Invalid password properly rejected with HTTP 401.")

    r_login = requests.post(f"{BASE_URL}/api/login", json={"username": test_user, "password": test_pass})
    assert r_login.status_code == 200, f"Login failed: {r_login.text}"
    login_token = r_login.json()["token"]

    r_me = requests.get(f"{BASE_URL}/api/me", headers={"Authorization": f"Bearer {login_token}"})
    assert r_me.json()["username"] == test_user
    print(" -> SUCCESS: Login and Bearer Token verification via SQLite passed.")

    # 5. Test Plan Generation with Cascade
    print("\n[5/8] Testing Plan Generation with Ping Probe Cascade...")
    payload = {
        "height": 195,
        "weight": 90,
        "position": "SG",
        "goal": ["🚀 Прыжок и вертикальный взрыв (Vertical Jump)", "⚡ Дриблинг и контроль мяча (Dribble & Ball Handling)"],
        "days_per_week": 4,
        "injuries": "Нет",
        "model": "claude-opus-5"
    }

    headers = {"Authorization": f"Bearer {login_token}"}
    r_gen = requests.post(f"{BASE_URL}/generate_plan", json=payload, headers=headers)
    assert r_gen.status_code == 200, f"Generate plan request failed: {r_gen.text}"
    task_id = r_gen.json()["task_id"]
    print(f" -> Task {task_id[:8]} started. Polling for completion...")

    start_time = time.time()
    task_success = False
    result_data = None

    for _ in range(40):
        time.sleep(3)
        r_status = requests.get(f"{BASE_URL}/plan_status/{task_id}", headers=headers)
        st = r_status.json()
        status_val = st.get("status")
        if status_val == "success":
            task_success = True
            result_data = st
            break
        elif status_val == "error":
            raise RuntimeError(f"Plan generation task error: {st.get('message')}")
        print(f"   Waiting... ({time.time()-start_time:.0f}s)")

    assert task_success, "Plan generation timed out after 120 seconds"
    print(f" -> SUCCESS: Plan generated by {result_data.get('source')} in {time.time()-start_time:.1f}s!")

    # 6. Test User History Synchronization in SQLite
    print("\n[6/8] Testing User History Persistence in SQLite...")
    r_hist = requests.get(f"{BASE_URL}/api/history", headers=headers)
    assert r_hist.status_code == 200
    history_items = r_hist.json().get("history", [])
    assert len(history_items) > 0, "Generated plan was not stored in user history!"
    item_id = history_items[0]["id"]
    print(f" -> SUCCESS: User history retrieved ({len(history_items)} items found in SQLite).")

    # 7. Test History Item Deletion
    print("\n[7/8] Testing History Deletion Security...")
    r_del = requests.delete(f"{BASE_URL}/api/history/{item_id}", headers=headers)
    assert r_del.status_code == 200
    r_hist_after = requests.get(f"{BASE_URL}/api/history", headers=headers)
    assert len(r_hist_after.json().get("history", [])) == len(history_items) - 1
    print(" -> SUCCESS: History item deleted successfully from SQLite.")

    # 8. Test Logout & Session Revocation
    print("\n[8/8] Testing Logout & Session Revocation...")
    r_logout = requests.post(f"{BASE_URL}/api/logout", headers=headers)
    assert r_logout.status_code == 200, f"Logout failed: {r_logout.text}"
    r_me_after = requests.get(f"{BASE_URL}/api/me", headers=headers)
    assert r_me_after.json().get("authenticated") is False, "Session token still valid after logout!"
    print(" -> SUCCESS: Logout revoked the session, token no longer authenticates.")

    print("\n==========================================================")
    print("   ALL TESTS PASSED SUCCESSFULLY! PROJECT IS PERFECT!     ")
    print("==========================================================")


if __name__ == "__main__":
    test_full_system()

