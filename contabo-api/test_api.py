"""Quick local test of the API endpoints against metadata.db."""
import json
import sys
import os

# Point to the metadata.db in parent directory
os.environ["METADATA_DB_PATH"] = os.path.join(os.path.dirname(__file__), "..", "metadata.db")
os.environ["API_KEY"] = "test-key-123"

from app import app

client = app.test_client()
HEADERS = {"X-API-Key": "test-key-123", "Content-Type": "application/json"}


def test_health():
    print("=" * 60)
    print("TEST: GET /health")
    resp = client.get("/health")
    data = resp.get_json()
    print(f"  Status: {resp.status_code}")
    print(f"  Tables: {data.get('tablesCount', '?')}")
    print(f"  Columns: {data.get('columnsCount', '?')}")
    assert resp.status_code == 200
    assert data["tablesCount"] > 25000
    print("  PASSED ✓")


def test_auth():
    print("\n" + "=" * 60)
    print("TEST: Auth rejection (no key)")
    resp = client.post("/resolve-schema", json={"query": "test"})
    assert resp.status_code == 401
    print(f"  Status: {resp.status_code} — correctly rejected")
    print("  PASSED ✓")


def test_resolve_invoices():
    print("\n" + "=" * 60)
    print("TEST: POST /resolve-schema — supplier invoices")
    resp = client.post("/resolve-schema", headers=HEADERS, json={
        "query": "Show supplier invoice amount, invoice date, supplier name, and invoice status"
    })
    data = resp.get_json()
    print(f"  Status: {resp.status_code}")
    print(f"  Tables returned: {data['meta']['tablesReturned']}")
    print(f"  Search terms: {data['meta']['searchTerms']}")
    print(f"  Tables:")
    for t in data["tables"]:
        print(f"    {t['name']:45s} (cols: {len(t['columns']):>3}, app: {t['appShortName']})")
    print(f"  Joins: {len(data['joins'])}")
    for j in data["joins"]:
        print(f"    {j['leftTable']}.{j['leftColumn']} → {j['rightTable']}.{j['rightColumn']} ({j['confidence']})")

    # Verify key tables are found
    table_names = [t["name"] for t in data["tables"]]
    assert "AP_INVOICES_ALL" in table_names, "AP_INVOICES_ALL not found!"
    print("\n  AP_INVOICES_ALL found ✓")

    # Check if vendor/supplier table found
    has_supplier = any("SUPPLIER" in t or "POZ_SUP" in t or "VENDOR" in t for t in table_names)
    print(f"  Supplier-related table found: {has_supplier}")
    print("  PASSED ✓")


def test_resolve_employees():
    print("\n" + "=" * 60)
    print("TEST: POST /resolve-schema — employee salary")
    resp = client.post("/resolve-schema", headers=HEADERS, json={
        "query": "Get employee name, department, and salary information"
    })
    data = resp.get_json()
    print(f"  Status: {resp.status_code}")
    print(f"  Tables returned: {data['meta']['tablesReturned']}")
    for t in data["tables"][:5]:
        print(f"    {t['name']:45s} (cols: {len(t['columns']):>3}, app: {t['appShortName']})")
    print("  PASSED ✓")


def test_resolve_purchase_order():
    print("\n" + "=" * 60)
    print("TEST: POST /resolve-schema — purchase orders")
    resp = client.post("/resolve-schema", headers=HEADERS, json={
        "query": "List purchase order number, line amount, item description, and supplier"
    })
    data = resp.get_json()
    print(f"  Status: {resp.status_code}")
    print(f"  Tables returned: {data['meta']['tablesReturned']}")
    for t in data["tables"][:5]:
        print(f"    {t['name']:45s} (cols: {len(t['columns']):>3}, app: {t['appShortName']})")
    print("  PASSED ✓")


def test_validate_valid_sql():
    print("\n" + "=" * 60)
    print("TEST: POST /validate-sql — valid SQL")
    resp = client.post("/validate-sql", headers=HEADERS, json={
        "sql": "SELECT aia.invoice_num, aia.invoice_amount FROM ap_invoices_all aia",
        "allowedTables": ["AP_INVOICES_ALL", "POZ_SUPPLIERS"]
    })
    data = resp.get_json()
    print(f"  Status: {resp.status_code}")
    print(f"  Valid: {data['valid']}")
    print(f"  Tables used: {data['tablesUsed']}")
    print(f"  Errors: {data['errors']}")
    assert data["valid"] is True
    print("  PASSED ✓")


def test_validate_hallucinated_table():
    print("\n" + "=" * 60)
    print("TEST: POST /validate-sql — hallucinated table")
    resp = client.post("/validate-sql", headers=HEADERS, json={
        "sql": "SELECT x.name FROM fake_nonexistent_table x",
        "allowedTables": ["AP_INVOICES_ALL"]
    })
    data = resp.get_json()
    print(f"  Status: {resp.status_code}")
    print(f"  Valid: {data['valid']}")
    print(f"  Errors: {data['errors']}")
    assert data["valid"] is False
    print("  PASSED ✓")


if __name__ == "__main__":
    test_health()
    test_auth()
    test_resolve_invoices()
    test_resolve_employees()
    test_resolve_purchase_order()
    test_validate_valid_sql()
    test_validate_hallucinated_table()
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED ✓")
