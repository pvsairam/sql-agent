"""
Oracle Fusion Metadata REST API
================================
Searches a DuckDB metadata.db and returns relevant
Oracle Fusion table/column/join metadata for SQL generation.

Endpoints:
    POST /resolve-schema   — Find relevant tables, columns, joins for a requirement
    POST /validate-sql     — Validate generated SQL against known metadata
    GET  /health           — Health check
"""

import os
import re
import logging
import functools
from collections import defaultdict

import duckdb
import sqlparse
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_KEY = os.getenv("API_KEY", "change-me-to-a-strong-random-key")
METADATA_DB_PATH = os.getenv("METADATA_DB_PATH", "metadata.db")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 5000))

# Search tuning
MAX_TABLES = 15             # Maximum tables to return (increased from 10)
MAX_COLUMNS_PER_TABLE = 50  # Maximum columns per table (0 = all)
DESCRIPTION_TRUNCATE = 200  # Truncate descriptions to this length

# Common Oracle Fusion synonyms for better search
SYNONYMS = {
    "vendor": ["supplier", "poz_suppliers", "vendor"],
    "supplier": ["vendor", "poz_suppliers", "supplier"],
    "employee": ["worker", "person", "per_all_people", "employee"],
    "worker": ["employee", "person", "per_all_people", "worker"],
    "po": ["purchase_order", "po_headers", "po_lines", "procurement"],
    "purchase order": ["po", "po_headers", "po_lines", "procurement"],
    "gl": ["general_ledger", "gl_je_headers", "gl_je_lines", "gl_balances"],
    "general ledger": ["gl", "gl_je_headers", "gl_je_lines", "gl_balances"],
    "ar": ["receivable", "ra_customer", "ar_payment"],
    "receivable": ["ar", "ra_customer", "ar_payment"],
    "ap": ["payable", "ap_invoices", "ap_payment"],
    "payable": ["ap", "ap_invoices", "ap_payment"],
    "journal": ["gl_je_headers", "gl_je_lines", "journal_entry"],
    "invoice": ["ap_invoices", "invoice", "ra_customer_trx"],
    "payment": ["ap_payment", "ar_payment", "iby_payment", "payment"],
    "customer": ["hz_parties", "hz_cust_accounts", "ra_customer", "customer"],
    "party": ["hz_parties", "hz_party_sites", "party"],
    "item": ["ego_item", "mtl_system_items", "inv_item", "item"],
    "inventory": ["inv_", "mtl_", "inventory"],
    "requisition": ["por_requisition", "requisition"],
    "receipt": ["rcv_shipment", "rcv_transactions", "receipt"],
    "asset": ["fa_additions", "fa_books", "asset"],
    "project": ["pjf_projects", "pjf_tasks", "project"],
    "budget": ["gl_budgets", "fun_budget", "budget"],
    "salary": ["cmp_salary", "pay_", "salary", "compensation"],
    "compensation": ["cmp_salary", "pay_", "salary", "compensation"],
    "department": ["per_departments", "hr_organization", "department"],
    "organization": ["hr_organization", "hr_all_organization", "organization"],
    "tax": ["zx_", "tax"],
    "bank": ["ce_bank", "iby_ext_bank", "bank"],
    "ledger": ["gl_ledgers", "gl_sets_of_books", "ledger"],
    "cost": ["cst_", "cost"],
    "expense": ["expense_report", "ap_expense", "expense"],
}

# Stop words to remove from search queries
STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "need", "must",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
    "they", "them", "their", "this", "that", "these", "those", "what",
    "which", "who", "whom", "how", "when", "where", "why",
    "all", "each", "every", "both", "few", "more", "most", "some", "any",
    "no", "not", "only", "same", "so", "than", "too", "very",
    "create", "write", "generate", "show", "get", "find", "list", "give",
    "display", "return", "fetch", "retrieve", "extract", "provide", "query",
    "select", "table", "column", "field", "data", "record", "row",
    "sql", "oracle", "fusion", "cloud", "erp",
}

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# Database connection (read-only, reused)
# ---------------------------------------------------------------------------
_db_conn = None


def get_db():
    """Get a read-only DuckDB connection (singleton per process)."""
    global _db_conn
    if _db_conn is None:
        _db_conn = duckdb.connect(METADATA_DB_PATH, read_only=True)
    return _db_conn


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
def require_api_key(f):
    """Decorator to enforce API key authentication."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if key != API_KEY:
            return jsonify({"error": "Unauthorized. Provide a valid X-API-Key header."}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def extract_keywords(query: str) -> list[str]:
    """Extract meaningful keywords from a natural language query."""
    tokens = re.split(r"[^a-zA-Z0-9_]+", query.lower())
    keywords = [t for t in tokens if t and t not in STOP_WORDS and len(t) > 1]
    expanded = set(keywords)
    for kw in keywords:
        if kw in SYNONYMS:
            expanded.update(SYNONYMS[kw])
    return list(expanded)


def search_tables(db, keywords: list[str]) -> list[dict]:
    """
    Search cached_tables for tables matching keywords.
    Uses a two-phase approach:
      Phase 1: Search by table name and description (primary discovery)
      Phase 2: Use column matches ONLY to boost already-found tables

    This prevents generic column keywords like 'amount', 'date', 'status'
    from pulling in thousands of irrelevant tables.
    """
    if not keywords:
        return []

    ATTRIBUTE_WORDS = {
        "amount", "date", "status", "name", "number", "code", "type",
        "description", "id", "flag", "value", "total", "quantity", "price",
        "rate", "percent", "percentage", "count", "line", "header",
        "creation", "update", "last", "first", "start", "end",
    }
    entity_keywords = [kw for kw in keywords if kw not in ATTRIBUTE_WORDS]

    # If ALL keywords are attributes, use them all for table search anyway
    search_keywords = entity_keywords if entity_keywords else keywords

    # ---------------------------------------------------------------
    # Phase 1: Search by table name, description, app_short_name
    # ---------------------------------------------------------------
    conditions = []
    params = []
    for kw in search_keywords:
        like_pat = f"%{kw}%"
        conditions.append(
            "(LOWER(t.table_name) LIKE ? OR LOWER(t.description) LIKE ? "
            "OR LOWER(t.app_short_name) LIKE ? OR LOWER(t.user_table_name) LIKE ?)"
        )
        params.extend([like_pat, like_pat, like_pat, like_pat])

    where = " OR ".join(conditions)
    sql = f"""
        SELECT t.table_id, t.table_name, t.app_short_name, t.user_table_name,
               t.table_type, t.description, t.module_key, t.status
        FROM cached_tables t
        WHERE ({where})
          AND t.status = 'Active'
        LIMIT 1000
    """
    rows = db.execute(sql, params).fetchall()
    logger.info(f"Phase 1 search found {len(rows)} candidate tables for keywords: {search_keywords}")

    # Score each table
    scored = []
    for row in rows:
        table_id, table_name, app_short, user_name, ttype, desc, mod_key, status = row
        score = 0
        table_name_lower = (table_name or "").lower()
        desc_lower = (desc or "").lower()
        user_name_lower = (user_name or "").lower()

        # Count how many ENTITY keywords match (relevance indicator)
        entity_matches = 0
        for kw in search_keywords:
            # Table name exact substring match (highest weight)
            if kw in table_name_lower:
                score += 15
                entity_matches += 1
            # User-friendly name match
            if kw in user_name_lower:
                score += 8
                entity_matches += 1
            # Description match
            if kw in desc_lower:
                score += 3

        # Bonus for matching MULTIPLE entity keywords (cross-concept relevance)
        if entity_matches >= 2:
            score += entity_matches * 5

        # Prefer base tables (T) over views (V)
        if ttype == "T":
            score += 2

        # Boost canonical Oracle base tables — _ALL suffix tables are always
        # the real production tables across every Oracle Fusion pillar
        if table_name_lower.endswith("_all"):
            score += 20

        # Prefer non-audit tables (tables ending in bare underscore are audit variants)
        if not table_name_lower.endswith("_"):
            score += 2

        # Penalize staging, temp, backup, interface, and variant tables
        # Penalty is -50 so these always drop below 0 and get filtered out by the score > 0 check
        TEMP_SUFFIXES = [
            "_gt", "_int", "_stage", "_stg", "_tmp", "_temp", "_bkp",
            "_log", "_purge", "_arch", "_hist", "_s_gt", "_interface",
            "_staging", "_eff", "_ucm", "_pii_", "_denied_parties_",
        ]
        for suffix in TEMP_SUFFIXES:
            if table_name_lower.endswith(suffix):
                score -= 50
                break

        # Penalize internal/system tables
        if any(prefix in table_name_lower for prefix in ["msc_bm_", "msc_hvgop_"]):
            score -= 5

        if score > 0:
            scored.append({
                "table_id": table_id,
                "name": table_name,
                "appShortName": app_short or "",
                "userTableName": user_name or "",
                "tableType": ttype or "T",
                "description": (desc or "")[:DESCRIPTION_TRUNCATE],
                "moduleKey": mod_key or "",
                "score": score,
            })

    # ---------------------------------------------------------------
    # Phase 2: Column-based boosting (boost already-found tables ONLY)
    # Use ALL keywords (including attributes) for column matching
    # ---------------------------------------------------------------
    if scored and keywords:
        existing_ids = {s["table_id"] for s in scored}
        existing_id_list = list(existing_ids)

        placeholders = ",".join(["?"] * len(existing_id_list))
        col_conditions = []
        col_params = list(existing_id_list)
        for kw in keywords:
            like_pat = f"%{kw}%"
            col_conditions.append("LOWER(c.column_name) LIKE ?")
            col_params.append(like_pat)

        col_where = " OR ".join(col_conditions)
        col_sql = f"""
            SELECT c.table_id, COUNT(*) as col_hits
            FROM cached_columns c
            WHERE c.table_id IN ({placeholders})
              AND ({col_where})
            GROUP BY c.table_id
        """
        col_rows = db.execute(col_sql, col_params).fetchall()

        col_boost_map = {r[0]: r[1] for r in col_rows}
        for s in scored:
            boost = col_boost_map.get(s["table_id"], 0)
            s["score"] += boost * 2

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:MAX_TABLES]
    logger.info(f"Returning top {len(top)} tables: {[t['name'] for t in top]}")
    return top


def fetch_columns(db, table_id: int, table_name: str) -> list[dict]:
    """Fetch columns for a given table, with PK info."""
    pk_rows = db.execute(
        "SELECT column_id FROM cached_primary_keys WHERE table_id = ?",
        [table_id]
    ).fetchall()
    pk_col_ids = {r[0] for r in pk_rows}

    limit_clause = f"LIMIT {MAX_COLUMNS_PER_TABLE}" if MAX_COLUMNS_PER_TABLE > 0 else ""
    col_rows = db.execute(f"""
        SELECT column_id, column_name, user_column_name, column_type,
               width, nullable, description, column_sequence
        FROM cached_columns
        WHERE table_id = ?
        ORDER BY column_sequence
        {limit_clause}
    """, [table_id]).fetchall()

    columns = []
    for cr in col_rows:
        columns.append({
            "name": cr[1],
            "userColumnName": cr[2] or cr[1],
            "dataType": cr[3] or "V",
            "width": cr[4] or 0,
            "nullable": cr[5] or "Y",
            "description": (cr[6] or "")[:DESCRIPTION_TRUNCATE],
            "isPK": cr[0] in pk_col_ids,
        })
    return columns


def infer_joins(db, tables: list[dict]) -> list[dict]:
    """
    Infer join relationships between matched tables by finding shared
    column names (especially those ending in _ID).
    """
    if len(tables) < 2:
        return []

    table_ids = [t["table_id"] for t in tables]
    table_id_to_name = {t["table_id"]: t["name"] for t in tables}

    placeholders_t = ",".join(["?"] * len(table_ids))

    id_cols = db.execute(f"""
        SELECT table_id, column_name, column_id
        FROM cached_columns
        WHERE table_id IN ({placeholders_t})
          AND (LOWER(column_name) LIKE '%\\_id' ESCAPE '\\'
               OR LOWER(column_name) LIKE '%\\_code' ESCAPE '\\')
        ORDER BY table_id, column_name
    """, table_ids).fetchall()

    col_to_tables = defaultdict(list)
    for tid, cname, cid in id_cols:
        col_to_tables[cname.upper()].append((tid, cid))

    pk_rows = db.execute(f"""
        SELECT table_id, column_id FROM cached_primary_keys
        WHERE table_id IN ({placeholders_t})
    """, table_ids).fetchall()
    pk_set = {(r[0], r[1]) for r in pk_rows}

    joins = []
    seen_pairs = set()

    for col_name, table_entries in col_to_tables.items():
        if len(table_entries) < 2:
            continue

        for i in range(len(table_entries)):
            for j in range(i + 1, len(table_entries)):
                tid_a, cid_a = table_entries[i]
                tid_b, cid_b = table_entries[j]

                if tid_a == tid_b:
                    continue

                pair_key = (min(tid_a, tid_b), max(tid_a, tid_b), col_name)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                a_is_pk = (tid_a, cid_a) in pk_set
                b_is_pk = (tid_b, cid_b) in pk_set

                if a_is_pk and not b_is_pk:
                    confidence = "high"
                    left_tid, right_tid = tid_b, tid_a
                elif b_is_pk and not a_is_pk:
                    confidence = "high"
                    left_tid, right_tid = tid_a, tid_b
                elif a_is_pk and b_is_pk:
                    confidence = "medium"
                    left_tid, right_tid = tid_a, tid_b
                else:
                    confidence = "medium"
                    left_tid, right_tid = tid_a, tid_b

                joins.append({
                    "leftTable": table_id_to_name[left_tid],
                    "leftColumn": col_name,
                    "rightTable": table_id_to_name[right_tid],
                    "rightColumn": col_name,
                    "joinType": "INNER JOIN",
                    "confidence": confidence,
                })

    joins.sort(key=lambda x: 0 if x["confidence"] == "high" else 1)
    return joins


def validate_sql_against_metadata(db, sql_text: str, allowed_tables: list[str]) -> dict:
    """
    Parse SQL and validate that all tables exist in metadata.
    When allowedTables is empty, validates only that tables exist in metadata.db.
    When allowedTables is provided, also checks tables were in the schema context.
    """
    errors = []
    tables_used = set()
    allowed_upper = {t.upper() for t in allowed_tables}

    try:
        parsed = sqlparse.parse(sql_text)
    except Exception as e:
        return {"valid": False, "errors": [f"SQL parse error: {str(e)}"], "tablesUsed": []}

    if not parsed:
        return {"valid": False, "errors": ["Empty SQL"], "tablesUsed": []}

    # Extract table names from SQL (covers FROM and JOIN patterns)
    table_pattern = r'(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)'
    matches = re.findall(table_pattern, sql_text, re.IGNORECASE)

    for tname in matches:
        tname_upper = tname.upper()
        tables_used.add(tname_upper)

        if tname_upper not in allowed_upper:
            # Check if it exists in metadata at all
            exists = db.execute(
                "SELECT COUNT(*) FROM cached_tables WHERE UPPER(table_name) = ?",
                [tname_upper]
            ).fetchone()[0]

            if exists:
                # If no allowedTables whitelist was provided, a table existing
                # in metadata is GOOD — the LLM used a real Oracle Fusion table.
                # Only flag it when a whitelist was explicitly provided.
                if allowed_tables:
                    errors.append(
                        f"Table '{tname_upper}' exists in metadata but was not in the "
                        f"provided schema context. It may be relevant — consider adding it."
                    )
                # else: table exists in metadata, no whitelist = valid, do nothing
            else:
                # Table does not exist at all — this is a hallucinated table name
                errors.append(
                    f"Table '{tname_upper}' does NOT exist in Oracle Fusion metadata. "
                    f"This is a hallucinated table name."
                )

    # Validate columns only when a whitelist is provided
    if allowed_tables:
        col_pattern = r'(?:SELECT|WHERE|AND|OR|ON|BY|,)\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)'
        col_matches = re.findall(col_pattern, sql_text, re.IGNORECASE)

        sql_keywords = {
            "SELECT", "FROM", "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "OUTER",
            "CROSS", "ON", "AND", "OR", "NOT", "IN", "EXISTS", "BETWEEN", "LIKE",
            "IS", "NULL", "AS", "ORDER", "BY", "GROUP", "HAVING", "UNION", "ALL",
            "DISTINCT", "CASE", "WHEN", "THEN", "ELSE", "END", "COUNT", "SUM",
            "AVG", "MIN", "MAX", "NVL", "NVL2", "DECODE", "TRIM", "UPPER", "LOWER",
            "TO_CHAR", "TO_DATE", "TO_NUMBER", "SYSDATE", "TRUNC", "ROUND", "ASC",
            "DESC", "FETCH", "FIRST", "NEXT", "ROWS", "ONLY", "OFFSET", "LIMIT",
            "COALESCE", "CAST", "SUBSTR", "LENGTH", "REPLACE", "INSTR", "CONCAT",
            "ROWNUM", "ROWID", "DUAL", "WITH", "RECURSIVE", "INSERT", "UPDATE",
            "DELETE", "VALUES", "SET", "INTO",
        }

        for col in col_matches:
            if col.upper() in sql_keywords:
                continue
            if col.upper() in tables_used:
                continue
            for tname in allowed_upper:
                tid_row = db.execute(
                    "SELECT table_id FROM cached_tables WHERE UPPER(table_name) = ?",
                    [tname]
                ).fetchone()
                if tid_row:
                    col_exists = db.execute(
                        "SELECT COUNT(*) FROM cached_columns WHERE table_id = ? AND UPPER(column_name) = ?",
                        [tid_row[0], col.upper()]
                    ).fetchone()[0]
                    if col_exists > 0:
                        break

    logger.info(f"Validation result: valid={len(errors) == 0}, tables={list(tables_used)}, errors={errors}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "tablesUsed": list(tables_used),
        "columnsValidated": True,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    """Health check — no auth required."""
    try:
        db = get_db()
        table_count = db.execute("SELECT COUNT(*) FROM cached_tables").fetchone()[0]
        col_count = db.execute("SELECT COUNT(*) FROM cached_columns").fetchone()[0]
        return jsonify({
            "status": "ok",
            "tablesCount": table_count,
            "columnsCount": col_count,
        })
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/resolve-schema", methods=["POST"])
@require_api_key
def resolve_schema():
    """
    Accept a natural language query and return relevant Oracle Fusion
    tables, columns, and inferred join hints.
    """
    body = request.get_json(force=True, silent=True)
    if not body or "query" not in body:
        return jsonify({"error": "Request body must include 'query' field."}), 400

    query = body["query"].strip()
    if not query:
        return jsonify({"error": "'query' must not be empty."}), 400

    logger.info(f"resolve-schema called with query: {query}")

    try:
        db = get_db()

        keywords = extract_keywords(query)
        if not keywords:
            return jsonify({
                "tables": [],
                "joins": [],
                "meta": {"tablesReturned": 0, "searchTerms": [], "searchMethod": "keyword"},
            })

        matched_tables = search_tables(db, keywords)
        if not matched_tables:
            return jsonify({
                "tables": [],
                "joins": [],
                "meta": {"tablesReturned": 0, "searchTerms": keywords, "searchMethod": "keyword"},
            })

        for tbl in matched_tables:
            tbl["columns"] = fetch_columns(db, tbl["table_id"], tbl["name"])

        joins = infer_joins(db, matched_tables)

        for tbl in matched_tables:
            del tbl["table_id"]
            del tbl["score"]

        return jsonify({
            "tables": matched_tables,
            "joins": joins,
            "meta": {
                "tablesReturned": len(matched_tables),
                "searchTerms": keywords,
                "searchMethod": "keyword",
            },
        })

    except Exception as e:
        logger.error(f"resolve-schema error: {str(e)}")
        return jsonify({"error": f"Internal error: {str(e)}"}), 500


@app.route("/validate-sql", methods=["POST"])
@require_api_key
def validate_sql():
    """
    Validate a generated SQL query against the metadata.
    Checks that all referenced tables exist in metadata.db.
    """
    body = request.get_json(force=True, silent=True)
    if not body or "sql" not in body:
        return jsonify({"error": "Request body must include 'sql' field."}), 400

    sql_text = body["sql"].strip()
    sql_text = sql_text.replace('\\_', '_').replace('\\', '')
    allowed_tables = body.get("allowedTables", [])

    if not sql_text:
        return jsonify({"error": "'sql' must not be empty."}), 400

    logger.info(f"validate-sql called, allowedTables count: {len(allowed_tables)}")

    try:
        db = get_db()
        result = validate_sql_against_metadata(db, sql_text, allowed_tables)
        return jsonify(result)
    except Exception as e:
        logger.error(f"validate-sql error: {str(e)}")
        return jsonify({"error": f"Validation error: {str(e)}"}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info(f"Starting Oracle Fusion Metadata API on {HOST}:{PORT}")
    logger.info(f"Database: {METADATA_DB_PATH}")
    app.run(host=HOST, port=PORT, debug=False)
