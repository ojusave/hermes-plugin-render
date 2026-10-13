"""Three real Hermes tasks, each checked in a fresh Render Sandbox.

All input data is synthetic. The trusted checkers are uploaded only after the
agent finishes, to a separate sandbox holding the downloaded output files.
"""

CSV_INPUT = """order_id,region,status,quantity,unit_price,customer
o1,West,paid,2,19.95,"A, Inc"
o2,East,paid,1,10.10,Bob
o3,West,cancelled,4,9.00,Carol
o4,East,paid,3,0.10,Dana
o2,East,paid,1,10.10,Bob
o5,Québec,paid,1,7.25,Émile
o6,East,refunded,1,5.00,Fred
o7,West,paid,1,0.00,Gina
"""

CSV_CHECKER = r"""
import json, subprocess, tempfile
from pathlib import Path
expected = {"paid_orders": 5, "revenue_cents": 5755,
            "regions": {"East": 1040, "West": 3990, "Québec": 725}}
assert json.loads(Path("summary.json").read_text()) == expected
checks = ["downloaded_summary", "quoted_csv_and_unicode", "paid_status_only", "deduplicated_ids", "exact_cents"]
with tempfile.TemporaryDirectory() as d:
    p = Path(d)
    for label, csv, want in [
        ("empty_input", "order_id,region,status,quantity,unit_price,customer\n",
         {"paid_orders": 0, "revenue_cents": 0, "regions": {}}),
        ("new_input", "order_id,region,status,quantity,unit_price,customer\n"
         "z1,North,paid,3,0.10,A\nz2,South,paid,2,1.05,B\n"
         "z1,North,paid,3,0.10,A\nz3,North,cancelled,8,5.00,C\n",
         {"paid_orders": 2, "revenue_cents": 240, "regions": {"North": 30, "South": 210}}),
    ]:
        (p / "input.csv").write_text(csv)
        subprocess.run(["python3", "analyze.py", str(p / "input.csv"), str(p / "out.json")], check=True, timeout=20)
        assert json.loads((p / "out.json").read_text()) == want, label
        checks.append(label)
print(json.dumps({"checks": checks, "summary": expected}))
"""

PACKAGE_FILES = {
    "inventory/__init__.py": "",
    "inventory/normalize.py": "def normalize_sku(value):\n    return value.lower()\n",
    "inventory/summary.py": (
        "from .normalize import normalize_sku\n\n"
        "def summarize(rows):\n"
        "    result = {}\n"
        "    for row in rows:\n"
        "        result[normalize_sku(row['sku'])] = int(row['quantity'])\n"
        "    return result\n"
    ),
    "inventory/__main__.py": (
        "import csv, json, sys\nfrom .summary import summarize\n"
        "with open(sys.argv[1], newline='') as source:\n"
        "    print(json.dumps(summarize(csv.DictReader(source)), sort_keys=True))\n"
    ),
    "stock.csv": "sku,quantity\n apple ,2\nAPPLE,3\npear,4\nPEAR,-1\n",
    "test_inventory.py": (
        "import json, subprocess, unittest\n"
        "from inventory.normalize import normalize_sku\n"
        "from inventory.summary import summarize\n\n"
        "class InventoryTests(unittest.TestCase):\n"
        "    def test_normalize(self): self.assertEqual(normalize_sku(' apple '), 'APPLE')\n"
        "    def test_duplicates(self): self.assertEqual(summarize([{'sku':'X','quantity':'2'}, {'sku':'X','quantity':'3'}]), {'X':5})\n"
        "    def test_returns(self): self.assertEqual(summarize([{'sku':'P','quantity':'4'}, {'sku':'p','quantity':'-1'}]), {'P':3})\n"
        "    def test_empty(self): self.assertEqual(summarize([]), {})\n"
        "    def test_cli(self):\n"
        "        out = subprocess.check_output(['python3', '-m', 'inventory', 'stock.csv'], text=True)\n"
        "        self.assertEqual(json.loads(out), {'APPLE':5, 'PEAR':3})\n"
    ),
}
PACKAGE_CHECKER = r"""
import copy, json, subprocess
from inventory.normalize import normalize_sku
from inventory.summary import summarize
subprocess.run(["python3", "-m", "unittest", "-v", "test_inventory"], check=True, timeout=20)
assert normalize_sku("  café  ") == "CAFÉ"
rows = [{"sku":" a ", "quantity":"8"}, {"sku":"A", "quantity":"-8"}, {"sku":" B", "quantity":"0"}]
original = copy.deepcopy(rows)
assert summarize(rows) == {"A": 0, "B": 0}
assert rows == original
print(json.dumps({"checks": ["five_original_tests", "unicode_sku", "zero_net_stock", "input_not_mutated"], "original_test_count": 5, "additional_check_count": 3}))
"""

DB_INPUT = (
    "\n".join(
        [
            '{"event_id":"e1","user_id":"Ada","kind":"purchase","amount_cents":1200}',
            '{"event_id":"e2","user_id":"Bo","kind":"purchase","amount_cents":500}',
            '{"event_id":"e3","user_id":"Ada","kind":"refund","amount_cents":200}',
            '{"event_id":"e2","user_id":"Bo","kind":"purchase","amount_cents":500}',
            '{"event_id":"e4","user_id":"Zoë","kind":"purchase","amount_cents":75}',
            '{"event_id":"e5","user_id":"Bo","kind":"refund","amount_cents":125}',
        ]
    )
    + "\n"
)
DB_CHECKER = r"""
import json, sqlite3, subprocess, tempfile
from pathlib import Path
expected = {"event_count": 5, "net_cents": 1450, "users": {"Ada": 1000, "Bo": 375, "Zoë": 75}}
def inspect(db, report, want):
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(events)")}
        assert set(columns) == {"event_id", "user_id", "kind", "amount_cents"}
        assert columns["event_id"][5] == 1
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        users = dict(conn.execute("SELECT user_id, SUM(CASE WHEN kind='purchase' THEN amount_cents ELSE -amount_cents END) FROM events GROUP BY user_id"))
        assert count == want["event_count"] and users == want["users"]
    assert json.loads(Path(report).read_text()) == want
inspect("events.sqlite", "report.json", expected)
# Verify the downloaded program can rebuild its result and can run twice safely.
with tempfile.TemporaryDirectory() as d:
    p = Path(d)
    for _ in range(2):
        subprocess.run(["python3", "importer.py", "events.jsonl", str(p / "fresh.sqlite"), str(p / "report.json")], check=True, timeout=20)
    inspect(p / "fresh.sqlite", p / "report.json", expected)
    (p / "new.jsonl").write_text('{"event_id":"e6","user_id":"Zoë","kind":"purchase","amount_cents":25}\n')
    subprocess.run(["python3", "importer.py", str(p / "new.jsonl"), str(p / "fresh.sqlite"), str(p / "report.json")], check=True, timeout=20)
    inspect(p / "fresh.sqlite", p / "report.json", {"event_count": 6, "net_cents": 1475, "users": {"Ada": 1000, "Bo": 375, "Zoë": 100}})
print(json.dumps({"checks": ["downloaded_database_integrity", "primary_key", "exact_aggregates", "downloaded_report", "rebuild_from_source", "repeat_import_no_duplicates", "incremental_new_event"], "summary": expected}))
"""

SCENARIOS = [
    {
        "name": "csv_analysis",
        "inputs": {"orders.csv": CSV_INPUT},
        "outputs": ["analyze.py", "summary.json"],
        "checker": CSV_CHECKER,
        "prompt": (
            "Read /workspace/orders.csv and write analyze.py, runnable as "
            "python3 analyze.py INPUT_CSV OUTPUT_JSON. Deduplicate by order_id keeping the first "
            "occurrence; include only paid orders. Compute quantity * unit_price as exact integer "
            "cents. Output exactly {paid_orders: integer, revenue_cents: integer, regions: "
            "{region: revenue_cents}}. Handle CSV quoting, Unicode and header-only input. "
            "Use Python's standard library. Run it on orders.csv to create summary.json. "
            "Do not change orders.csv. Inspect and explain the actual result."
        ),
    },
    {
        "name": "multifile_repair",
        "inputs": PACKAGE_FILES,
        "outputs": ["inventory/normalize.py", "inventory/summary.py"],
        "checker": PACKAGE_CHECKER,
        "baseline": "python3 -m unittest -v test_inventory",
        "prompt": (
            "Repair the inventory package. SKU keys must be stripped of surrounding whitespace "
            "and uppercased. Quantities for repeated normalized keys must be added, including "
            "negative returns and zero totals. Preserve the input rows. Read the files, reproduce "
            "the failure using python3 -m unittest -v test_inventory, change only "
            "inventory/normalize.py and inventory/summary.py, and rerun all original tests. "
            "Also exercise python3 -m inventory stock.csv. Do not change tests or other inputs. "
            "Explain the changes and actual results."
        ),
    },
    {
        "name": "sqlite_workflow",
        "inputs": {"events.jsonl": DB_INPUT},
        "outputs": ["importer.py", "events.sqlite", "report.json"],
        "checker": DB_CHECKER,
        "prompt": (
            "Create importer.py using only the Python standard library. Run it as "
            "python3 importer.py INPUT_JSONL DATABASE_FILE REPORT_JSON. Import events.jsonl "
            "into a SQLite events table with exactly event_id TEXT PRIMARY KEY, user_id TEXT, "
            "kind TEXT, amount_cents INTEGER. Duplicate IDs are ignored, keeping the first "
            "record; repeated imports must leave existing records intact. Commit and close "
            "the database before exiting. Report over all stored records as exactly "
            "{event_count: integer, net_cents: integer, users: {user_id: net_cents}}. "
            "Purchases add cents and refunds subtract them. Create events.sqlite and report.json "
            "from events.jsonl, run the importer twice and verify unchanged counts. "
            "Do not edit events.jsonl. Explain the actual result."
        ),
    },
]
