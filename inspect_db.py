import duckdb

conn = duckdb.connect('metadata.db', read_only=True)

# Check primary keys for AP_INVOICES_ALL
print("=== PRIMARY KEYS FOR AP_INVOICES_ALL ===")
result = conn.execute("""
    SELECT pk.primary_key_id, pk.pk_sequence, c.column_name
    FROM cached_primary_keys pk
    JOIN cached_tables t ON pk.table_id = t.table_id
    JOIN cached_columns c ON pk.column_id = c.column_id AND pk.table_id = c.table_id
    WHERE t.table_name = 'AP_INVOICES_ALL'
    ORDER BY pk.primary_key_id, pk.pk_sequence
""").fetchall()
for r in result:
    print(f"  pk_id={r[0]} seq={r[1]} col={r[2]}")

# Check embedding dimensions
print("\n=== REMARK_EMBEDDINGS STRUCTURE ===")
result = conn.execute("""
    SELECT source, COUNT(*) as cnt
    FROM remark_embeddings
    GROUP BY source
""").fetchall()
for r in result:
    print(f"  source={r[0]:15s} count={r[1]:>10,}")

# Check embedding vector length
print("\n=== EMBEDDING VECTOR SAMPLE ===")
result = conn.execute("""
    SELECT id, source, table_name, column_name, remark_text, typeof(embedding), length(embedding::VARCHAR)
    FROM remark_embeddings
    WHERE table_name = 'AP_INVOICES_ALL' AND source = 'table'
    LIMIT 1
""").fetchall()
for r in result:
    print(f"  id={r[0]} source={r[1]} table={r[2]} col={r[3]}")
    print(f"  remark_text={r[4][:100]}...")
    print(f"  embedding type={r[5]} length={r[6]}")

# Check data type of embedding column
print("\n=== EMBEDDING COLUMN TYPE ===")
result = conn.execute("""
    SELECT column_name, data_type 
    FROM information_schema.columns 
    WHERE table_name = 'remark_embeddings'
""").fetchall()
for r in result:
    print(f"  {r[0]:20s} {r[1]}")

# How many unique tables have embeddings?
print("\n=== EMBEDDING COVERAGE ===")
result = conn.execute("""
    SELECT COUNT(DISTINCT table_name) as tables_with_embeddings
    FROM remark_embeddings
    WHERE source = 'table'
""").fetchone()
print(f"  Tables with embeddings: {result[0]}")

result = conn.execute("""
    SELECT COUNT(DISTINCT table_name) as tables_with_col_embeddings
    FROM remark_embeddings
    WHERE source = 'column'
""").fetchone()
print(f"  Tables with column embeddings: {result[0]}")

# Sample column types distribution
print("\n=== COLUMN TYPE DISTRIBUTION ===")
result = conn.execute("""
    SELECT column_type, COUNT(*) as cnt
    FROM cached_columns
    GROUP BY column_type
    ORDER BY cnt DESC
""").fetchall()
for r in result:
    print(f"  type={r[0]:5s} count={r[1]:>10,}")

# Check table_type distribution  
print("\n=== TABLE TYPE DISTRIBUTION ===")
result = conn.execute("""
    SELECT table_type, COUNT(*) as cnt
    FROM cached_tables
    GROUP BY table_type
    ORDER BY cnt DESC
""").fetchall()
for r in result:
    print(f"  type={r[0]:5s} count={r[1]:>10,}")

# Check for supplier-related tables
print("\n=== SUPPLIER-RELATED TABLES ===")
result = conn.execute("""
    SELECT table_name, app_short_name, description
    FROM cached_tables
    WHERE table_name LIKE '%SUPPLIER%' OR table_name LIKE '%POZ_SUP%' OR table_name LIKE '%HZ_PART%'
    LIMIT 15
""").fetchall()
for r in result:
    desc = str(r[2])[:80] if r[2] else ''
    print(f"  {r[0]:45s} {r[1]:5s} {desc}")

conn.close()
