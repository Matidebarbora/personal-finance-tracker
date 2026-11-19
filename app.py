import sqlite3
from datetime import date
from textual.app import App, ComposeResult
from textual.screen import ModalScreen, Screen
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Footer, DataTable, Input, Button, Select, Tree, Label, Rule, Static
from textual.theme import Theme
from textual import on
from rich.text import Text
from rich.cells import cell_len

# --- Theme (matches the Option B mockup) ---
WALLET_THEME = Theme(
    name="wallet-dark",
    primary="#4a9eff",
    secondary="#a06cd5",
    accent="#d4a843",
    foreground="#c8c8d4",
    background="#0a0a0f",
    surface="#0d0d1a",
    panel="#1a1a2e",
    success="#3ddc84",
    warning="#e5c07b",
    error="#e06c75",
    dark=True,
    variables={
        "block-cursor-foreground": "#0a0a0f",
        "block-cursor-background": "#4a9eff",
        "block-cursor-text-style": "bold",
        "footer-key-foreground": "#7070c0",
        "footer-description-foreground": "#66667e",
        "border": "#2a2a4a",
    },
)

# --- Helper Functions ---
def get_current_month():
    return date.today().strftime("%Y-%m")

def get_month_options():
    today = date.today()
    options = []
    for i in range(-6, 13): 
        m = (today.month - 1 + i) % 12 + 1
        y = today.year + (today.month - 1 + i) // 12
        period_str = f"{y}-{m:02d}"
        options.append((period_str, period_str))
    return options

# Credit-card statement cycle: the card closes on the 24th. A charge dated after the
# 24th belongs to next month's statement; on/before it belongs to this month's.
STATEMENT_CLOSE_DAY = 24

def period_for_date(date_str: str) -> str:
    """Statement-anchored period (YYYY-MM) for a YYYY-MM-DD date."""
    d = date.fromisoformat(date_str)
    if d.day > STATEMENT_CLOSE_DAY:
        y = d.year + (1 if d.month == 12 else 0)
        m = 1 if d.month == 12 else d.month + 1
        return f"{y}-{m:02d}"
    return f"{d.year}-{d.month:02d}"

def period_range_label(period: str) -> str:
    """Human label for a period's statement window, e.g. '25 May - 24 Jun'."""
    y, m = map(int, period.split("-"))
    end = date(y, m, STATEMENT_CLOSE_DAY)
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    start = date(py, pm, STATEMENT_CLOSE_DAY + 1)
    return f"{start.strftime('%d %b')} - {end.strftime('%d %b')}"

def node_base_text(prefix: str, name: str) -> str:
    return f"{prefix} {name}"

def format_node_label(base_text: str, depth: int, base_style: str = "", all_checked: bool = False, pad_to: int = 0) -> Text:
    # pad_to is the label-relative column where the tick should sit (depth already factored in by caller).
    pad_length = max(1, pad_to - cell_len(base_text))
    t = Text(base_text + (" " * pad_length), style=base_style)
    if all_checked:
        t.append("✓", style="bold green")
    return t

ACCOUNT_NAMES = {"CC": "Cuenta Corriente", "TCN": "TC Nacional", "TCI": "TC Internacional"}

# --- Option B palette (GnuCash/Quicken style) ---
ACCOUNT_STYLES = {"CC": "bold #d4a843", "TCN": "bold #a06cd5", "TCI": "bold #4ec9b0"}  # gold / purple / teal
INCOME_STYLE = "bold #3ddc84"   # green
EXPENSE_STYLE = "bold #e06c75"  # red
MIXED_STYLE = "bold #7090e0"    # blue (group with both income & expense items)
LEAF_STYLE = "#b0b0c8"          # soft gray
INCOME_AMOUNT = "#3ddc84"
EXPENSE_AMOUNT = "#e06c75"
MIXED_AMOUNT = "#7090e0"
UNPAID_STYLE = "#8a8a9a"        # muted but readable (stays legible on the selection highlight)
ROW_STYLE = "#c8c8d4"           # normal (paid) row text — set explicitly so the row cursor highlight never recolors it
OVERSPEND_STYLE = "#ff6b6b"
AVAILABLE_STYLE = "#3ddc84"
DIM_WHITE = "#a0a0a0"           # muted white for dashboard headings/labels (calmer than the accent colors)
DIM_WHITE_LOW = "#787880"       # a touch dimmer, for secondary bits like the (account) suffix
# Fixed colors for the dashboard's CURRENT BCC bar segments — reuses the same account
# colors used everywhere else, so the bar reads consistently with the rest of the app.
CC_UNPAID_COLOR = "#d4a843"   # gold, matches CC account color
TCN_COLOR = "#a06cd5"         # matches ACCOUNT_STYLES TCN
TCI_COLOR = "#4ec9b0"         # matches ACCOUNT_STYLES TCI
GROUP_BAR_COLOR = "#56d8e6"   # cyan, matches the month selector — used for every group's fill bar

def group_style(item_types) -> str:
    """Color a category group by the type of items it holds."""
    types = set(item_types)
    if types == {"Income"}:
        return INCOME_STYLE
    if types == {"Expense"}:
        return EXPENSE_STYLE
    return MIXED_STYLE

def compute_bcc_bar_segments(cc_unpaid: float, tcn: float, tci: float, funds: float):
    """Percentage widths for the dashboard's balance bar: the box represents 100% = funds
    (CURRENT BCC + still-unpaid CC income, i.e. everything REAL EXPECTED starts from), and
    cc_unpaid/tcn/tci are drawn as proportional segments inside it — a direct visualization
    of the REAL EXPECTED formula. Under capacity, the unused space is the 'leftover'
    (= REAL EXPECTED) segment. Over capacity (commitments exceed funds), segments are
    rescaled to fill exactly 100% — same never-overflow rule used elsewhere in this app —
    and there's no leftover segment; the caller shows the shortfall as text instead.
    Returns (pcts: list[float] len 3, leftover_pct, overspent)."""
    amounts = [max(0.0, cc_unpaid), max(0.0, tcn), max(0.0, tci)]
    total = sum(amounts)
    overspent = funds <= 0 or total > funds
    baseline = total if overspent else funds
    if baseline <= 0:
        return [0.0, 0.0, 0.0], 0.0, overspent
    pcts = [amt / baseline * 100 for amt in amounts]
    leftover_pct = 0.0 if overspent else max(0.0, 100 - sum(pcts))
    return pcts, leftover_pct, overspent

def get_group_summaries(period):
    """All (account, category) groups for a period, each with its actual (paid) and
    expected (paid+unpaid) totals. Independent of the tree's smart-flatten display —
    every category shows here, promoted-to-a-leaf or not."""
    categories = get_categories()
    txs = get_transactions(period)
    summaries = []
    for acc_key in ["CC", "TCN", "TCI"]:
        acc_categories = [c for c in categories if c[1] == acc_key]
        cat_groups = {}
        for c in acc_categories:
            cat_groups.setdefault(c[3], []).append(c)
        for cat_name in sorted(cat_groups.keys()):
            cat_txs = [tx for tx in txs if tx[2] == acc_key and tx[4] == cat_name]
            summaries.append({
                "account": acc_key,
                "category": cat_name,
                "actual": sum(tx[6] for tx in cat_txs if tx[7] == 'Paid'),
                "expected": sum(tx[6] for tx in cat_txs),
            })
    return summaries

# --- Database Setup & Operations ---
DB_NAME = "finances.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period TEXT,
                account TEXT,
                type TEXT,
                category TEXT,
                description TEXT,
                amount REAL,
                status TEXT DEFAULT 'Paid',
                note TEXT DEFAULT '',
                date TEXT DEFAULT '',
                installments_left INTEGER DEFAULT 0
            )
        ''')
        # Migrations
        cursor.execute("PRAGMA table_info(transactions)")
        cols = [col[1] for col in cursor.fetchall()]
        if 'period' not in cols:
            cursor.execute(f"ALTER TABLE transactions ADD COLUMN period TEXT DEFAULT '{get_current_month()}'")
        if 'status' not in cols:
            cursor.execute("ALTER TABLE transactions ADD COLUMN status TEXT DEFAULT 'Paid'")
        if 'note' not in cols:
            cursor.execute("ALTER TABLE transactions ADD COLUMN note TEXT DEFAULT ''")
        if 'checked' not in cols:
            cursor.execute("ALTER TABLE transactions ADD COLUMN checked INTEGER DEFAULT 0")
        if 'date' not in cols:
            cursor.execute("ALTER TABLE transactions ADD COLUMN date TEXT DEFAULT ''")
            # Backfill a synthetic date inside each row's existing period (the 15th maps
            # back to the same period under the statement rule, so nothing reshuffles).
            cursor.execute("UPDATE transactions SET date = period || '-15' WHERE date IS NULL OR date = ''")
        if 'installments_left' not in cols:
            cursor.execute("ALTER TABLE transactions ADD COLUMN installments_left INTEGER DEFAULT 0")
            
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account TEXT,
                type TEXT,
                category TEXT,
                description TEXT,
                is_fixed INTEGER DEFAULT 0
            )
        ''')
        cursor.execute("PRAGMA table_info(categories)")
        if 'is_fixed' not in [col[1] for col in cursor.fetchall()]:
            cursor.execute("ALTER TABLE categories ADD COLUMN is_fixed INTEGER DEFAULT 0")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        conn.commit()

# Transactions DB Calls
def get_transactions(period):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT id, period, account, type, category, description, amount, status, note, checked, date, installments_left FROM transactions WHERE period = ?", (period,)).fetchall()

def add_transaction(period, account, tx_type, category, description, amount, status="Paid", note="", tx_date="", installments_left=0):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT INTO transactions (period, account, type, category, description, amount, status, note, date, installments_left) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (period, account, tx_type, category, description, amount, status, note, tx_date, installments_left))

def update_transaction(tx_id, period, account, tx_type, category, description, amount, status, note, tx_date, installments_left):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE transactions SET period=?, account=?, type=?, category=?, description=?, amount=?, status=?, note=?, date=?, installments_left=? WHERE id=?",
                     (period, account, tx_type, category, description, amount, status, note, tx_date, installments_left, tx_id))

def delete_transaction(tx_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))

def delete_transactions(tx_ids):
    with sqlite3.connect(DB_NAME) as conn:
        conn.executemany("DELETE FROM transactions WHERE id = ?", [(tx_id,) for tx_id in tx_ids])

def toggle_transaction_status(tx_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE transactions SET status = CASE WHEN status='Paid' THEN 'Unpaid' ELSE 'Paid' END WHERE id=?", (tx_id,))

def toggle_transaction_checked(tx_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE transactions SET checked = CASE WHEN checked=1 THEN 0 ELSE 1 END WHERE id=?", (tx_id,))

def compute_balance_components(period, txs):
    """Shared balance math for a period: (tcn, tci, cc_egresos_unpaid, cc_income_unpaid).
    Used by both the main UI (calculate_balances) and the DashboardScreen, so the two never
    drift apart. tcn/tci are the FULL statement total (paid + unpaid) — REAL EXPECTED treats
    the whole card balance as a draw against current funds, not just what's still unpaid.
    cc_income_unpaid is income still marked Unpaid (e.g. a paycheck not yet received): REAL
    EXPECTED is the projected end-of-period scenario, so pending income counts, same as
    pending expenses do."""
    tcn = sum(tx[6] for tx in txs if tx[2] == 'TCN' and tx[3] == 'Expense')
    tci = sum(tx[6] for tx in txs if tx[2] == 'TCI' and tx[3] == 'Expense')
    cc_egresos_unpaid = sum(tx[6] for tx in txs if tx[2] == 'CC' and tx[3] == 'Expense' and tx[4] == 'Egresos' and tx[7] != 'Paid')
    cc_income_unpaid = sum(tx[6] for tx in txs if tx[2] == 'CC' and tx[3] == 'Income' and tx[7] != 'Paid')
    return tcn, tci, cc_egresos_unpaid, cc_income_unpaid

def compute_current_bcc(txs):
    """Actual settled checking balance: only PAID CC transactions count. Unlike REAL
    EXPECTED, this never includes the forward-looking ARRIENDO PRÓX. reserve — using
    REAL EXPECTED for the Saldo Mes Anterior handoff would double-subtract rent (once as
    next period's reserve, again as next period's own recorded Arriendo expense)."""
    cc_paid_incomes = sum(tx[6] for tx in txs if tx[2] == 'CC' and tx[3] == 'Income' and tx[7] == 'Paid')
    cc_paid_expenses = sum(tx[6] for tx in txs if tx[2] == 'CC' and tx[3] == 'Expense' and tx[7] == 'Paid')
    return cc_paid_incomes - cc_paid_expenses

def carry_forward_period(period):
    """Auto-populate an empty period with the most recent prior period's line items,
    all marked Unpaid. No-op if the period already has transactions, so it never
    overwrites work in progress.

    Installment purchases (installments_left > 0) count down by one each time they're
    carried forward, and stop being generated once the last installment has been copied
    (installments_left == 1 in the source row means that was the final payment).

    Traceability handoff: the prior period's own CURRENT BCC (actual settled checking
    balance — PAID CC transactions only) replaces whatever amount 'Saldo Mes Anterior'
    had, so each period's starting balance is the real result of the previous one — not
    just a stale copy of last month's number. This must be CURRENT BCC, not REAL EXPECTED:
    REAL EXPECTED already reserves next period's rent (ARRIENDO PRÓX.), and that same rent
    shows up again as next period's own recorded Arriendo expense — using REAL EXPECTED
    here would subtract that rent twice."""
    with sqlite3.connect(DB_NAME) as conn:
        has_any = conn.execute("SELECT 1 FROM transactions WHERE period=? LIMIT 1", (period,)).fetchone()
        if has_any:
            return
        prev_row = conn.execute("SELECT period FROM transactions WHERE period < ? ORDER BY period DESC LIMIT 1", (period,)).fetchone()
        if not prev_row:
            return
        prev_period = prev_row[0]
        rows = conn.execute(
            "SELECT account, type, category, description, amount, installments_left FROM transactions WHERE period=?", (prev_period,)
        ).fetchall()
        default_date = f"{period}-15"  # inside the period; user sets the real date on edit
        current_bcc_prev = compute_current_bcc(get_transactions(prev_period))

        new_rows = []
        for acc, tx_type, cat, desc, amt, inst_left in rows:
            if acc == 'CC' and tx_type == 'Income' and desc == 'Saldo Mes Anterior':
                amt = current_bcc_prev
            if inst_left and inst_left > 0:
                if inst_left <= 1:
                    continue  # last installment was already carried; don't generate another
                inst_left -= 1
            new_rows.append((period, acc, tx_type, cat, desc, amt, default_date, inst_left or 0))

        if new_rows:
            conn.executemany(
                "INSERT INTO transactions (period, account, type, category, description, amount, status, note, checked, date, installments_left) VALUES (?, ?, ?, ?, ?, ?, 'Unpaid', '', 0, ?, ?)",
                new_rows
            )

# Categories DB Calls
def get_categories():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT id, account, type, category, description FROM categories ORDER BY category, description").fetchall()

def add_category(account, tx_type, category, description):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT INTO categories (account, type, category, description) VALUES (?, ?, ?, ?)", (account, tx_type, category, description))

def delete_category(cat_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))

def delete_category_group(account, category):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM categories WHERE account = ? AND category = ?", (account, category))

def update_category_group_name(account, old_cat, new_cat):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE categories SET category = ? WHERE account = ? AND category = ?", (new_cat, account, old_cat))
        conn.execute("UPDATE transactions SET category = ? WHERE account = ? AND category = ?", (new_cat, account, old_cat))

def update_leaf_name(cat_id, account, category, old_desc, new_desc):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE categories SET description = ? WHERE id = ?", (new_desc, cat_id))
        conn.execute("UPDATE transactions SET description = ? WHERE account = ? AND category = ? AND description = ?", (new_desc, account, category, old_desc))

# --- Modals ---
class TransactionModal(ModalScreen[dict]):
    CSS = """
    TransactionModal { align: center middle; background: $background 50%; }
    #dialog { width: 50; height: auto; padding: 2 4; border: thick $accent; background: $surface; }
    .row { height: auto; margin-bottom: 1; }
    .row > * { width: 1fr; margin-right: 1; }
    .info-label { margin-bottom: 1; text-style: bold; color: $text-muted; }
    #modal-title { text-style: bold; margin-bottom: 1; }
    #m_period_hint { width: 1fr; content-align: left middle; text-style: bold; }
    """
    def __init__(self, tx_id=None, period="", acc="", tx_type="", cat="", desc="", amt="", status="Paid", note="", tx_date="", installments_left=0, **kwargs):
        super().__init__(**kwargs)
        self.tx_id, self.period, self.acc, self.tx_type, self.cat = tx_id, period, acc, tx_type, cat
        self.desc, self.amt, self.status, self.note = desc, str(amt) if amt else "", status, note
        self.tx_date = tx_date or date.today().isoformat()
        self.installments_left = str(installments_left) if installments_left else ""

    def compose(self):
        with Vertical(id="dialog"):
            yield Label("Edit Entry" if self.tx_id else "Add Entry", id="modal-title")
            yield Label(f"Item: {self.desc}", classes="info-label")
            with Horizontal(classes="row"):
                yield Input(placeholder="Date (YYYY-MM-DD)", value=self.tx_date, id="m_date")
                yield Label("", id="m_period_hint")
            with Horizontal(classes="row"):
                yield Input(placeholder="Amount ($)", value=self.amt, id="m_amt")
                yield Select([("Paid", "Paid"), ("Unpaid", "Unpaid")], value=self.status, id="m_status")
            with Horizontal(classes="row"):
                yield Input(placeholder="Note (Optional)", value=self.note, id="m_note")
                yield Input(placeholder="Cuotas restantes (opcional)", value=self.installments_left, id="m_installments")
            with Horizontal(classes="row"):
                yield Button("Save", variant="success", id="m_save")
                yield Button("Cancel", variant="error", id="m_cancel")

    def on_mount(self):
        self._update_hint()
        self.query_one("#m_amt").focus()

    @on(Input.Changed, "#m_date")
    def _on_date_changed(self):
        self._update_hint()

    def _update_hint(self):
        hint = self.query_one("#m_period_hint", Label)
        try:
            hint.update(f"→ period {period_for_date(self.query_one('#m_date').value.strip())}")
            hint.styles.color = "#3ddc84"
        except ValueError:
            hint.update("→ invalid date")
            hint.styles.color = "#ff6b6b"

    @on(Button.Pressed, "#m_save")
    def save(self):
        try: amt = float(self.query_one("#m_amt").value)
        except ValueError: self.notify("Amount must be a number", severity="error"); return
        tx_date = self.query_one("#m_date").value.strip()
        try:
            period = period_for_date(tx_date)
        except ValueError:
            self.notify("Date must be YYYY-MM-DD", severity="error"); return

        installments_raw = self.query_one("#m_installments").value.strip()
        installments_left = 0
        if installments_raw:
            try:
                installments_left = int(installments_raw)
                if installments_left < 0: raise ValueError
            except ValueError:
                self.notify("Cuotas restantes debe ser un entero ≥ 0", severity="error"); return

        data = {
            "id": self.tx_id, "period": period, "acc": self.acc,
            "type": self.tx_type, "cat": self.cat, "desc": self.desc,
            "amt": amt, "status": self.query_one("#m_status").value,
            "note": self.query_one("#m_note").value.strip(), "date": tx_date,
            "installments_left": installments_left,
        }
        self.dismiss(data)

    @on(Button.Pressed, "#m_cancel")
    def cancel(self):
        self.dismiss(None)

class InputModal(ModalScreen[str]):
    CSS = """
    InputModal { align: center middle; background: $background 50%; }
    #input-dialog { width: 45; height: auto; padding: 2 4; border: thick $accent; background: $surface; }
    """
    def __init__(self, title, placeholder, initial_value="", **kwargs):
        super().__init__(**kwargs)
        self.title, self.placeholder, self.initial_value = title, placeholder, initial_value

    def compose(self):
        with Vertical(id="input-dialog"):
            yield Label(self.title)
            yield Input(placeholder=self.placeholder, value=self.initial_value, id="m_val")
            with Horizontal():
                yield Button("Save", variant="success", id="m_save")
                yield Button("Cancel", variant="error", id="m_cancel")
                
    def on_mount(self):
        self.query_one(Input).focus()

    @on(Button.Pressed, "#m_save")
    def save(self):
        val = self.query_one("#m_val").value.strip()
        self.dismiss(val if val else None)
        
    @on(Button.Pressed, "#m_cancel")
    def cancel(self):
        self.dismiss(None)

class NewCategoryModal(ModalScreen[dict]):
    CSS = """
    NewCategoryModal { align: center middle; background: $background 50%; }
    #nc-dialog { width: 60; height: auto; padding: 2 4; border: thick $accent; background: $surface; }
    .row { height: auto; margin-bottom: 1; }
    .row > * { width: 1fr; margin-right: 1; }
    """
    def __init__(self, acc="", tx_type="", cat="", **kwargs):
        super().__init__(**kwargs)
        self.acc, self.tx_type, self.cat = acc, tx_type, cat

    def compose(self):
        with Vertical(id="nc-dialog"):
            yield Label("Add New Tree Item")
            with Horizontal(classes="row"):
                yield Select([("Cuenta Corriente", "CC"), ("TC Nacional", "TCN"), ("TC Internacional", "TCI")], value=self.acc or Select.BLANK, id="nc_acc")
                yield Select([("Egreso (Expense)", "Expense"), ("Ingreso (Income)", "Income")], value=self.tx_type or Select.BLANK, id="nc_type")
            with Horizontal(classes="row"):
                yield Input(placeholder="Group (e.g., Egresos)", value=self.cat, id="nc_cat")
                yield Input(placeholder="New Item Name", id="nc_desc")
            with Horizontal(classes="row"):
                yield Button("Save", variant="success", id="nc_save")
                yield Button("Cancel", variant="error", id="nc_cancel")

    def on_mount(self):
        self.query_one("#nc_desc").focus()

    @on(Button.Pressed, "#nc_save")
    def save(self):
        data = {
            "acc": self.query_one("#nc_acc").value, "type": self.query_one("#nc_type").value,
            "cat": self.query_one("#nc_cat").value.strip(), "desc": self.query_one("#nc_desc").value.strip()
        }
        if Select.BLANK in (data["acc"], data["type"]) or not data["cat"] or not data["desc"]:
            self.notify("Please fill all fields", severity="error")
            return
        self.dismiss(data)

    @on(Button.Pressed, "#nc_cancel")
    def cancel(self):
        self.dismiss(None)

class ConfirmModal(ModalScreen[bool]):
    CSS = """
    ConfirmModal { align: center middle; background: $background 50%; }
    #confirm-dialog { width: 45; height: auto; padding: 2 4; border: thick $error; background: $surface; }
    """
    def __init__(self, message: str, **kwargs):
        super().__init__(**kwargs)
        self.message = message

    def compose(self):
        with Vertical(id="confirm-dialog"):
            yield Label(self.message)
            with Horizontal():
                yield Button("Confirm", variant="error", id="c_confirm")
                yield Button("Cancel", variant="default", id="c_cancel")

    def on_mount(self):
        self.query_one("#c_cancel").focus()

    @on(Button.Pressed, "#c_confirm")
    def confirm(self):
        self.dismiss(True)

    @on(Button.Pressed, "#c_cancel")
    def cancel(self):
        self.dismiss(False)

class DashboardScreen(Screen):
    """Full-screen overview: every category group's actual-vs-expected, plus one
    macOS-storage-style bar showing how the month's income splits across spending groups."""
    BINDINGS = [
        ("escape", "close_dashboard", "Close"),
        ("q", "close_dashboard", "Close"),
        ("ctrl+d", "close_dashboard", "Close"),
    ]

    CSS = """
    DashboardScreen { background: #0a0a0f; }
    #dash-container { padding: 1 3; height: 100%; }
    #dash-header { height: auto; border-bottom: solid #222235; padding-bottom: 1; margin-bottom: 1; }
    #dash-title { color: #a0a0a0; }
    .dash-section-title { text-style: bold; color: #a0a0a0; margin-top: 1; margin-bottom: 1; }
    #dash-bcc-bar { border: round #2a5a5d; width: 100%; height: 3; padding: 0 1; }
    .bcc-segment { height: 1; }
    #dash-legend-row { height: auto; layout: horizontal; margin-top: 1; margin-bottom: 1; }
    .legend-item { width: 1fr; height: 2; padding-right: 1; }
    #dash-groups { height: 1fr; margin-top: 1; }
    .group-row-header { height: 1; padding-left: 1; border-left: solid #222235; }
    .grp-bar-row { height: 3; margin-bottom: 1; }
    .grp-box { border: round #2a5a5d; width: 42; height: 3; background: #14141e; }
    .grp-fill { height: 1; }
    .grp-values { padding-left: 2; height: 3; content-align: left middle; }
    """

    def __init__(self, period: str, **kwargs):
        super().__init__(**kwargs)
        self.period = period

    def compose(self) -> ComposeResult:
        with Vertical(id="dash-container"):
            with Vertical(id="dash-header"):
                yield Label(f"DASHBOARD — {self.period}  ({period_range_label(self.period)})", id="dash-title")
            yield Label("BALANCE GENERAL DEL MES", classes="dash-section-title")
            with Horizontal(id="dash-bcc-bar"):
                yield Static("", id="seg-cc-unpaid", classes="bcc-segment")
                yield Static("", id="seg-tcn", classes="bcc-segment")
                yield Static("", id="seg-tci", classes="bcc-segment")
                yield Static("", id="seg-leftover", classes="bcc-segment")
            with Horizontal(id="dash-legend-row"):
                pass
            yield Label("GRUPOS", classes="dash-section-title")
            with VerticalScroll(id="dash-groups"):
                pass
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_dashboard()

    def action_close_dashboard(self) -> None:
        self.app.pop_screen()

    async def refresh_dashboard(self) -> None:
        period = self.period
        txs = get_transactions(period)
        current_bcc = compute_current_bcc(txs)
        tcn, tci, cc_unpaid, income_unpaid = compute_balance_components(period, txs)
        funds = current_bcc + income_unpaid
        real_expected = funds - tcn - tci - cc_unpaid

        pcts, leftover_pct, overspent = compute_bcc_bar_segments(cc_unpaid, tcn, tci, funds)
        seg_specs = [
            ("#seg-cc-unpaid", cc_unpaid, CC_UNPAID_COLOR, "Unpaid Cta. Corriente"),
            ("#seg-tcn", tcn, TCN_COLOR, "TCN"),
            ("#seg-tci", tci, TCI_COLOR, "TCI"),
        ]
        legend_items = []
        for (selector, amount, color, label), pct in zip(seg_specs, pcts):
            seg = self.query_one(selector, Static)
            seg.styles.width = f"{pct}%"
            seg.styles.background = color
            # Two lines — name on top, amount below — so each legend stays narrow and the
            # four sit in one row, same left-to-right order as the bar segments.
            item = Text("● ", style=color)
            item.append(f"{label}\n", style="#9090a8")
            item.append(f"${amount:,.0f}", style="#c8c8d4")
            legend_items.append(item)

        leftover_seg = self.query_one("#seg-leftover", Static)
        if overspent:
            leftover_seg.styles.width = "0%"
            leftover_seg.styles.background = "#0a0a0f"
            final_item = Text("⚠ Faltante\n", style=f"bold {OVERSPEND_STYLE}")
            final_item.append(f"${real_expected:,.0f}", style=f"bold {OVERSPEND_STYLE}")
        else:
            leftover_seg.styles.width = f"{leftover_pct}%"
            leftover_seg.styles.background = AVAILABLE_STYLE
            final_item = Text("● ", style=AVAILABLE_STYLE)
            final_item.append("Disponible\n", style="#9090a8")
            final_item.append(f"${real_expected:,.0f}", style="#c8c8d4")
        legend_items.append(final_item)

        legend_row = self.query_one("#dash-legend-row", Horizontal)
        await legend_row.remove_children()
        await legend_row.mount_all([Label(item, classes="legend-item") for item in legend_items])

        summaries = get_group_summaries(period)
        container = self.query_one("#dash-groups", VerticalScroll)
        await container.remove_children()
        rows = []
        for g in summaries:
            header = Text(f"{g['category']}  ", style=DIM_WHITE)
            header.append(f"({ACCOUNT_NAMES.get(g['account'], g['account'])})", style=DIM_WHITE_LOW)
            rows.append(Label(header, classes="group-row-header"))

            # Same concept as the balance bar: a rounded box with a proportional colored
            # segment inside, instead of block-character bars. Fill capped at 100%.
            pct = 100.0 if g["expected"] <= 0 else min(100.0, max(0.0, g["actual"] / g["expected"] * 100))
            fill = Static(classes="grp-fill")
            fill.styles.width = f"{pct}%"
            fill.styles.background = GROUP_BAR_COLOR
            box = Horizontal(fill, classes="grp-box")

            over = g["actual"] > g["expected"]
            values = Text(f"${g['actual']:,.0f}", style=f"bold {OVERSPEND_STYLE}" if over else "#9090a8")
            values.append(" / ", style="#4a4a5a")
            values.append(f"${g['expected']:,.0f}", style="#5a5a6a")

            rows.append(Horizontal(box, Label(values, classes="grp-values"), classes="grp-bar-row"))
        await container.mount_all(rows)

# --- Main App ---
class FinanceTrackerApp(App):
    TITLE = "Personal Finance Tracker"
    
    BINDINGS = [
        ("a", "add_transaction", "Add Trans"),
        ("e", "edit_transaction", "Edit Trans"),
        ("d", "delete_transaction", "Delete Trans"),
        ("ctrl+k", "delete_all_transactions", "Delete All"),
        ("space", "toggle_status", "Toggle Paid"),
        ("ctrl+t", "toggle_checked", "Tick"),
        ("n", "new_tree_item", "New Tree Item"),
        ("r", "rename_tree", "Rename Tree Item"),
        ("x", "delete_by_position", "Delete Tree Item"),
        ("ctrl+d", "show_dashboard", "Dashboard"),
    ]

    CSS = """
    Screen { background: #0a0a0f; }
    Header { background: #1a1a2e; color: #7070c0; }
    Footer { background: #1a1a2e; }
    FooterKey { background: #1a1a2e; }
    FooterKey > .footer-key--key { background: #252540; color: #7070c0; }
    FooterKey > .footer-key--description { color: #66667e; }

    #main-container { layout: horizontal; height: 100%; }
    #sidebar { width: 32%; height: 100%; border-right: solid #222235; padding: 1; }
    #content { width: 68%; height: 100%; padding: 0 1; }

    Tree { background: #0d0d1a; padding: 0 1; }
    Tree > .tree--cursor { background: #1e2a4a; color: #c8c8f8; }
    Tree > .tree--guides { color: #2a2a4a; }
    Tree > .tree--guides-hover { color: #3a3a5a; }
    Tree > .tree--guides-selected { color: #3a3a5a; }

    DataTable { height: 1fr; background: #0d0d1a; }
    DataTable > .datatable--header { background: #141422; color: #4a6a9a; text-style: bold; }
    /* Selected row: only a slightly-lighter background — keep every cell's own text color/style.
       (Textual's default cursor also sets color + bold via $block-cursor-*, so pin them back.) */
    DataTable > .datatable--cursor { background: #1c1c2b; color: #c8c8d4; text-style: none; }
    DataTable > .datatable--hover { background: #14141e; }

    #top-bar { height: auto; padding-bottom: 0; border-bottom: solid #222235; margin-bottom: 1; }
    #top-bar-row1 { height: auto; layout: horizontal; align: left middle; }
    #input_period { width: 30; height: 1; margin: 0; }
    #input_period SelectCurrent { background: #123a3d; border: none; padding: 0 1; height: 1; }
    #input_period SelectCurrent Static#label { color: #56d8e6; text-style: bold; }
    #input_period SelectCurrent .arrow { color: #56d8e6 50%; }

    .top-metric { text-style: bold; padding: 0 2; content-align: center middle; }
    .top-metric-full { text-style: bold; padding: 0 1; width: 100%; }
    .period-range { color: #d4a843; padding: 0 1; content-align: center middle; }

    #sidebar-balances { height: auto; border-top: solid #222235; padding-top: 1; margin-top: 1; }
    .balance-label { text-style: bold; padding-left: 1; }
    .balance-card { color: #9090a8; }
    .balance-real { color: #3ddc84; text-style: bold; }
    .balance-calc { color: white 40%; padding-left: 1; }
    .balance-current { color: #4a9eff; }
    .bal-rule { height: 1; margin: 0; color: #2a2a4a; }
    .clickable:hover { color: $warning; text-style: bold; background: $surface; }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_filter = None 

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-container"):
            with Vertical(id="sidebar"):
                yield Tree("My Finances", id="finance_tree")
                with Vertical(id="sidebar-balances"):
                    yield Label("CURRENT BCC: $0", id="lbl_current_bcc", classes="balance-label balance-current")
                    yield Label("INGRESOS CC UNPAID: $0", id="lbl_income_unpaid", classes="balance-label balance-card")
                    yield Rule(classes="bal-rule")
                    yield Label("TCN: $0", id="lbl_tcn", classes="balance-label balance-card")
                    yield Label("TCI: $0", id="lbl_tci", classes="balance-label balance-card")
                    yield Label("EGRESOS CC UNPAID: $0", id="lbl_cc_unpaid", classes="balance-label balance-card")
                    yield Label("", id="lbl_real_calc", classes="balance-calc")
                    yield Label("REAL EXPECTED: $0", id="lbl_real", classes="balance-label balance-real")
            
            with Vertical(id="content"):
                with Vertical(id="top-bar"):
                    with Horizontal(id="top-bar-row1"):
                        yield Select(get_month_options(), value=get_current_month(), id="input_period")
                        yield Label("", id="lbl_period_range", classes="period-range")
                        yield Label("Checked 0 of 0", id="lbl_checked", classes="top-metric")
                    yield Label("Subtotal: $0", id="lbl_subtotal", classes="top-metric-full")
                yield DataTable(id="data_table")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(WALLET_THEME)
        self.theme = "wallet-dark"
        init_db()
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("ID", "Date", "Account", "Type", "Status", "Description", "Note", "Amount", "Cuotas", "✓")
        carry_forward_period(self.query_one("#input_period", Select).value)
        self.build_tree_structure()
        self.refresh_table()

    # --- Tree Logic ---
    def build_tree_structure(self) -> None:
        tree = self.query_one("#finance_tree", Tree)
        tree.clear()
        tree.root.expand()

        period = self.query_one("#input_period", Select).value
        txs = get_transactions(period) if period != Select.BLANK else []

        categories = get_categories()
        nodes = {}
        records = []  # (tree_node, depth, base_text, base_style, all_checked)

        acc_order = ["CC", "TCN", "TCI"]
        for i, acc_key in enumerate(acc_order, 1):
            acc_prefix = f"{i}."
            acc_txs = [tx for tx in txs if tx[2] == acc_key]
            acc_all_checked = bool(acc_txs) and all(tx[9] for tx in acc_txs)
            acc_base = node_base_text(acc_prefix, ACCOUNT_NAMES.get(acc_key, acc_key))
            nodes[acc_key] = tree.root.add(
                acc_base, expand=True,
                data={"is_account": True, "account": acc_key, "prefix": acc_prefix}
            )
            records.append((nodes[acc_key], 0, acc_base, ACCOUNT_STYLES.get(acc_key, "bold orange3"), acc_all_checked))

            acc_categories = [c for c in categories if c[1] == acc_key]
            cat_groups = {}
            for c in acc_categories:
                cat_groups.setdefault(c[3], []).append(c)

            sorted_cat_names = sorted(cat_groups.keys())

            for j, cat_name in enumerate(sorted_cat_names, 1):
                cat_prefix = f"{i}.{j}."
                items = sorted(cat_groups[cat_name], key=lambda x: x[4])
                cat_txs = [tx for tx in acc_txs if tx[4] == cat_name]
                cat_style = group_style(c[2] for c in items)

                # Smart-flatten: a single-item category is redundant nesting, so promote
                # the item up to the account level, displayed with the category name.
                if len(items) == 1:
                    cat_id, acc, tx_type, _, desc = items[0]
                    promoted_checked = bool(cat_txs) and all(tx[9] for tx in cat_txs)
                    promoted_base = node_base_text(cat_prefix, cat_name)
                    promoted_node = nodes[acc_key].add_leaf(
                        promoted_base,
                        data={"is_leaf": True, "is_promoted": True, "cat_id": cat_id, "account": acc,
                              "type": tx_type, "category": cat_name, "desc": desc, "prefix": cat_prefix}
                    )
                    records.append((promoted_node, 1, promoted_base, cat_style, promoted_checked))
                    continue

                cat_all_checked = bool(cat_txs) and all(tx[9] for tx in cat_txs)
                cat_base = node_base_text(cat_prefix, cat_name)
                group_node = nodes[acc_key].add(
                    cat_base,
                    data={"is_category": True, "account": acc_key, "category": cat_name, "prefix": cat_prefix}
                )
                records.append((group_node, 1, cat_base, cat_style, cat_all_checked))

                for k, item in enumerate(items, 1):
                    cat_id, acc, tx_type, _, desc = item
                    leaf_prefix = f"{i}.{j}.{k}."
                    leaf_txs = [tx for tx in cat_txs if tx[5] == desc]
                    leaf_all_checked = bool(leaf_txs) and all(tx[9] for tx in leaf_txs)
                    leaf_base = node_base_text(leaf_prefix, desc)
                    leaf_node = group_node.add_leaf(
                        leaf_base,
                        data={"is_leaf": True, "cat_id": cat_id, "account": acc, "type": tx_type, "category": cat_name, "desc": desc, "prefix": leaf_prefix}
                    )
                    records.append((leaf_node, 2, leaf_base, LEAF_STYLE, leaf_all_checked))

        # Second pass: align ticks to a shared column. Each tree level adds GUIDE_DEPTH
        # cols of indent, so account for that when finding the widest node.
        GUIDE_DEPTH = tree.guide_depth
        tick_col = max((cell_len(b) + GUIDE_DEPTH * d for _, d, b, _, _ in records), default=0) + 1
        for node, depth, base, base_style, all_checked in records:
            node.set_label(format_node_label(base, depth, base_style, all_checked, pad_to=tick_col - GUIDE_DEPTH * depth))

        tree.root.expand_all()

    @on(Tree.NodeSelected, "#finance_tree")
    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node == self.query_one("#finance_tree").root:
            self.current_filter = None 
        else:
            self.current_filter = event.node.data 
        self.refresh_table()

    # --- Table Logic ---
    def refresh_table(self) -> None:
        period = self.query_one("#input_period", Select).value
        if period == Select.BLANK: return

        self.query_one("#lbl_period_range", Label).update(period_range_label(period))
        table = self.query_one(DataTable)
        # Remember which row the cursor is on so a refresh (tick, toggle paid, edit…)
        # doesn't snap it back to the first row.
        prev_row_key = None
        if table.row_count:
            prev_row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        table.clear()

        txs = get_transactions(period)
        filtered_txs = txs
        
        if self.current_filter:
            if self.current_filter.get("is_account"):
                filtered_txs = [tx for tx in txs if tx[2] == self.current_filter["account"]]
            elif self.current_filter.get("is_category"):
                filtered_txs = [tx for tx in txs if tx[2] == self.current_filter["account"] and tx[4] == self.current_filter["category"]]
            elif self.current_filter.get("is_leaf"):
                filtered_txs = [tx for tx in txs if tx[2] == self.current_filter["account"] and tx[4] == self.current_filter["category"] and tx[5] == self.current_filter["desc"]]
                
        for tx in filtered_txs:
            paid = tx[7] == 'Paid'
            style = ROW_STYLE if paid else UNPAID_STYLE
            status_icon = Text("[✓]", style="#3ddc84") if paid else Text("[ ]", style="#6a6a7a")
            tick = Text("✓", style="bold #3ddc84") if tx[9] else Text("")
            acc_style = ACCOUNT_STYLES.get(tx[2], "") if paid else style
            is_income_positive = tx[3] == 'Income' and tx[6] >= 0
            amt_style = (INCOME_AMOUNT if is_income_positive else EXPENSE_AMOUNT) if paid else style
            try:
                date_disp = date.fromisoformat(tx[10]).strftime("%d %b") if tx[10] else ""
            except ValueError:
                date_disp = tx[10] or ""
            installments_disp = Text(str(tx[11]), style="#e5c07b") if tx[11] else Text("")

            table.add_row(
                Text(str(tx[0]), style=style),
                Text(date_disp, style="#7090e0" if paid else style),
                Text(tx[2], style=acc_style),
                Text(tx[3], style=style),
                status_icon,
                Text(tx[5], style=style),
                Text(tx[8], style=style),
                Text(f"${tx[6]:,.0f}", style=amt_style, justify="right"),
                installments_disp,
                tick,
                key=str(tx[0])
            )
            
        if prev_row_key is not None and prev_row_key in {str(tx[0]) for tx in filtered_txs}:
            table.move_cursor(row=table.get_row_index(prev_row_key))

        self.calculate_balances(txs)
        self.update_subtotal_label(filtered_txs)

    def update_subtotal_label(self, filtered_txs) -> None:
        # Leaf/category windows are homogeneous (one item or one expense/income group),
        # so a plain paid-vs-all sum reads cleanly. Account/overall windows mix income and
        # expense, so they need a net (income - expense) on both sides instead.
        if self.current_filter and (self.current_filter.get("is_leaf") or self.current_filter.get("is_category")):
            if self.current_filter.get("is_category"):
                window_name = self.current_filter["category"]
            else:
                window_name = self.current_filter["category"] if self.current_filter.get("is_promoted") else self.current_filter["desc"]
            actual = sum(tx[6] for tx in filtered_txs if tx[7] == 'Paid')
            expected = sum(tx[6] for tx in filtered_txs)
        else:
            window_name = ACCOUNT_NAMES.get(self.current_filter['account'], '') if self.current_filter else "Overall"
            actual = (sum(tx[6] for tx in filtered_txs if tx[3] == 'Income' and tx[7] == 'Paid')
                      - sum(tx[6] for tx in filtered_txs if tx[3] == 'Expense' and tx[7] == 'Paid'))
            expected = (sum(tx[6] for tx in filtered_txs if tx[3] == 'Income')
                        - sum(tx[6] for tx in filtered_txs if tx[3] == 'Expense'))

        self.query_one("#lbl_subtotal", Label).update(f"{window_name} — Actual ${actual:,.0f} / Esperado ${expected:,.0f}")

    def calculate_balances(self, txs) -> None:
        period = self.query_one("#input_period", Select).value
        tcn, tci, cc_unpaid, income_unpaid = compute_balance_components(period, txs)
        current_bcc = compute_current_bcc(txs)
        balance_real = current_bcc + income_unpaid - tcn - tci - cc_unpaid

        self.query_one("#lbl_current_bcc", Label).update(f"CURRENT BCC: ${current_bcc:,.0f}")
        self.query_one("#lbl_income_unpaid", Label).update(f"INGRESOS CC UNPAID: ${income_unpaid:,.0f}")
        self.query_one("#lbl_tcn", Label).update(f"TCN: ${tcn:,.0f}")
        self.query_one("#lbl_tci", Label).update(f"TCI: ${tci:,.0f}")
        self.query_one("#lbl_cc_unpaid", Label).update(f"EGRESOS CC UNPAID: ${cc_unpaid:,.0f}")
        self.query_one("#lbl_real_calc", Label).update(f"{current_bcc:,.0f} + {income_unpaid:,.0f} - {tcn:,.0f} - {tci:,.0f} - {cc_unpaid:,.0f}")
        real_lbl = self.query_one("#lbl_real", Label)
        if balance_real < 0:
            # Current funds can't cover the statement — you'd have to dip into next month's pay.
            real_lbl.update(f"REAL EXPECTED: ${balance_real:,.0f}  ⚠ LIFEGUARD")
            real_lbl.styles.color = "#ff6b6b"
        else:
            real_lbl.update(f"REAL EXPECTED: ${balance_real:,.0f}")
            real_lbl.styles.color = "#3ddc84"

        checked_count = sum(1 for tx in txs if tx[9])
        self.query_one("#lbl_checked", Label).update(f"Checked {checked_count} of {len(txs)}")

    @on(Select.Changed, "#input_period")
    def on_period_changed(self, event: Select.Changed) -> None:
        if event.value != Select.BLANK:
            carry_forward_period(event.value)
            self.build_tree_structure()
            self.refresh_table()

    # --- Hotkey Actions ---
    def action_toggle_status(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            self.notify("Select a transaction in the table and press Space to toggle Paid/Unpaid.", severity="warning")
            return
        tx_id = int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        toggle_transaction_status(tx_id)
        self.refresh_table()

    def action_toggle_checked(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            self.notify("Select a transaction to tick.", severity="warning")
            return
        tx_id = int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        toggle_transaction_checked(tx_id)
        self.build_tree_structure()
        self.refresh_table()

    def action_add_transaction(self) -> None:
        if not self.current_filter or not self.current_filter.get("is_leaf"):
            self.notify("Select a specific item in the tree (e.g., '1.1.3. Extras') to add an entry.", severity="warning")
            return
            
        period = self.query_one("#input_period", Select).value
        acc, tx_type, cat, desc = self.current_filter["account"], self.current_filter["type"], self.current_filter["category"], self.current_filter["desc"]
        self.push_screen(TransactionModal(period=period, acc=acc, tx_type=tx_type, cat=cat, desc=desc, status="Paid"), self.on_transaction_saved)

    def action_edit_transaction(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            self.notify("Highlight a transaction in the table and press 'e' to edit.", severity="warning")
            return
        tx_id = int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        tx = next((t for t in get_transactions(self.query_one("#input_period", Select).value) if t[0] == tx_id), None)
        if tx:
            self.push_screen(TransactionModal(tx_id=tx[0], period=tx[1], acc=tx[2], tx_type=tx[3], cat=tx[4], desc=tx[5], amt=tx[6], status=tx[7], note=tx[8], tx_date=tx[10], installments_left=tx[11]), self.on_transaction_saved)

    def on_transaction_saved(self, data) -> None:
        if data:
            if data["id"]: update_transaction(data["id"], data["period"], data["acc"], data["type"], data["cat"], data["desc"], data["amt"], data["status"], data["note"], data["date"], data["installments_left"])
            else: add_transaction(data["period"], data["acc"], data["type"], data["cat"], data["desc"], data["amt"], data["status"], data["note"], data["date"], data["installments_left"])
            viewed = self.query_one("#input_period", Select).value
            if data["period"] != viewed:
                self.notify(f"Saved to {data['period']} (dated after the {STATEMENT_CLOSE_DAY}th).", severity="information")
            self.build_tree_structure()
            self.refresh_table()

    def action_delete_transaction(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            self.notify("Select a transaction to delete.", severity="warning")
            return
        tx_id = int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        self.push_screen(ConfirmModal("Delete this transaction?"), lambda confirmed: self._do_delete_transaction(tx_id, confirmed))

    def _do_delete_transaction(self, tx_id: int, confirmed: bool) -> None:
        if confirmed:
            delete_transaction(tx_id)
            self.notify("Transaction deleted.", severity="info")
            self.refresh_table()

    def action_delete_all_transactions(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            self.notify("No transactions to delete in this view.", severity="warning")
            return
        tx_ids = [int(row_key.value) for row_key in table.rows.keys()]
        self.push_screen(ConfirmModal(f"Delete all {len(tx_ids)} transactions in this view?"), lambda confirmed: self._do_delete_all_transactions(tx_ids, confirmed))

    def _do_delete_all_transactions(self, tx_ids: list, confirmed: bool) -> None:
        if confirmed:
            delete_transactions(tx_ids)
            self.notify(f"{len(tx_ids)} transactions deleted.", severity="info")
            self.build_tree_structure()
            self.refresh_table()

    def action_show_dashboard(self) -> None:
        period = self.query_one("#input_period", Select).value
        if period != Select.BLANK:
            self.push_screen(DashboardScreen(period))

    def action_new_tree_item(self) -> None:
        acc, tx_type, cat = "", "", ""
        if self.current_filter:
            acc, tx_type, cat = self.current_filter.get("account", ""), self.current_filter.get("type", ""), self.current_filter.get("category", "")
        self.push_screen(NewCategoryModal(acc=acc, tx_type=tx_type, cat=cat), self.on_new_category_saved)

    def on_new_category_saved(self, data) -> None:
        if data:
            add_category(data["acc"], data["type"], data["cat"], data["desc"])
            self.build_tree_structure()
            self.refresh_table()

    def action_rename_tree(self) -> None:
        tree = self.query_one("#finance_tree", Tree)
        node = tree.cursor_node
        if not node or not node.data: return
        if node.data.get("is_category") or node.data.get("is_promoted"):
            def after_rename(new_name):
                if new_name:
                    update_category_group_name(node.data["account"], node.data["category"], new_name)
                    self.build_tree_structure(); self.refresh_table()
            self.push_screen(InputModal("Rename Category Group", "New Name", node.data["category"]), after_rename)
        elif node.data.get("is_leaf"):
            def after_rename(new_name):
                if new_name:
                    update_leaf_name(node.data["cat_id"], node.data["account"], node.data["category"], node.data["desc"], new_name)
                    self.build_tree_structure(); self.refresh_table()
            self.push_screen(InputModal("Rename Item", "New Name", node.data["desc"]), after_rename)

    def action_delete_by_position(self) -> None:
        self.push_screen(InputModal("Delete Tree Item", "Enter position number (e.g., 1.2)"), self.on_position_delete)

    def on_position_delete(self, position: str) -> None:
        if not position: return
        position = position.strip()
        if not position.endswith("."): position += "."
        tree = self.query_one("#finance_tree", Tree)

        def find_node(node):
            if node.data and node.data.get("prefix", "").strip() == position: return node
            for child in node.children:
                res = find_node(child)
                if res: return res
            return None

        target_node = find_node(tree.root)
        if not target_node:
            self.notify(f"Could not find category at position '{position}'.", severity="warning")
            return
        if target_node.data.get("is_account"):
            self.notify("Cannot delete root accounts (1, 2, or 3).", severity="error")
            return

        node_data = target_node.data
        name = node_data["category"] if node_data.get("is_promoted") else (node_data.get("desc") or node_data.get("category", ""))
        self.push_screen(ConfirmModal(f"Delete '{name}' and all its data?"), lambda confirmed: self._do_delete_tree_item(node_data, confirmed))

    def _do_delete_tree_item(self, node_data: dict, confirmed: bool) -> None:
        if confirmed:
            if node_data.get("is_leaf"):
                delete_category(node_data["cat_id"])
            elif node_data.get("is_category"):
                delete_category_group(node_data["account"], node_data["category"])
            self.build_tree_structure()
            self.refresh_table()

if __name__ == "__main__":
    app = FinanceTrackerApp()
    app.run()