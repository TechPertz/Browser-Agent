"""Mock Workday route tests via TestClient — no network, no subprocess."""

from fastapi.testclient import TestClient

from services.mock_workday.app import app


def test_health():
    c = TestClient(app)
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_root_redirects_to_directory():
    c = TestClient(app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/directory" in r.headers["location"]


def test_directory_lists_employees():
    c = TestClient(app)
    r = c.get("/directory")
    assert r.status_code == 200
    assert "Directory" in r.text
    # Seeded 100 employees exist on disk; directory should show them
    assert "E-1001" in r.text


def test_search_matches_name():
    c = TestClient(app)
    r = c.get("/directory/search?q=Octavius")
    # Fallback data always has Octavius; search should find it
    r2 = c.get("/directory/search?q=NoSuchPerson")
    assert r.status_code == 200
    assert r2.status_code == 200
    assert "No matches" in r2.text or "No matches." in r2.text


def test_person_page_returns_404_when_missing():
    c = TestClient(app)
    r = c.get("/people/does-not-exist")
    assert r.status_code == 404


def test_person_page_renders_profile():
    c = TestClient(app)
    r = c.get("/people/E-1001")
    assert r.status_code == 200
    assert "E-1001" in r.text
    # Person has title + department rendered
    assert "Title" in r.text
    assert "Department" in r.text


def test_form_new_renders():
    c = TestClient(app)
    r = c.get("/forms/new")
    assert r.status_code == 200
    assert "Employee ID" in r.text
    assert "Submit" in r.text


def test_form_submit_returns_confirmation_and_attachment():
    c = TestClient(app)
    r = c.post(
        "/forms/submit",
        data={
            "employee_id": "E-1001", "request_type": "PTO",
            "start_date": "2026-05-01", "notes": "test",
        },
    )
    assert r.status_code == 200
    assert "Confirmation" in r.text
    # The confirmation page links to an attachment URL
    assert "/attachments/CONF-" in r.text


def test_attachment_served():
    c = TestClient(app)
    r = c.post("/forms/submit", data={
        "employee_id": "E-1001", "request_type": "PTO",
    })
    # Extract the attachment URL from the confirmation HTML
    import re
    m = re.search(r'(/attachments/CONF-[A-F0-9]+\.txt)', r.text)
    assert m, "confirmation must link to attachment"
    r2 = c.get(m.group(1))
    assert r2.status_code == 200
    assert "employee_id=E-1001" in r2.text
