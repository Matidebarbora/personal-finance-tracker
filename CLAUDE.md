# CLAUDE.md

Personal finance tracker — a single-file Textual TUI backed by SQLite. Tracks monthly income/expenses across three accounts (a checking account and two credit cards), with a tree-based category browser and a transaction table.

## Run

```bash
python3 app.py
```

- Python 3.12, **Textual 7.4** and **Rich** (no `requirements.txt` — install with `pip install textual rich`).
- The SQLite file `finances.db` is created/migrated automatically on first launch (`init_db()`), in the working directory.
- No tests. Verify changes by running headless with Textual's pilot, e.g.:
  ```python
  async with FinanceTrackerApp().run_test(size=(120, 40)) as pilot:
      await pilot.pause()
  ```
  This catches CSS/theme/mount errors without a real terminal. Color fidelity needs a truecolor terminal (iTerm2, etc.).

## Architecture

Everything lives in `app.py` (~900 lines), organized as:

1. **Module-level helpers & palette** — `get_current_month`, `get_month_options`, `node_base_text`, `format_node_label`, the `WALLET_THEME` (custom Textual `Theme`), the Option-B color constants (`ACCOUNT_STYLES`, `INCOME_STYLE`, `EXPENSE_STYLE`, `group_style()`, etc.), and the dashboard's bar helpers (`render_fill_bar`, `compute_bcc_bar_segments`, `get_group_summaries`).
2. **DB layer** — module-level functions, each opens its own `sqlite3.connect(DB_NAME)`. No ORM, no connection pooling, no shared cursor.
3. **Modal screens** — `TransactionModal`, `InputModal`, `NewCategoryModal`, `ConfirmModal` (each `ModalScreen` with inline CSS).
4. **`DashboardScreen`** — a full-screen (non-modal) `Screen`, opened with `Ctrl+D`, closed with `Esc`/`q`/`Ctrl+D`. See "Dashboard" below.
5. **`FinanceTrackerApp`** — the main `App`: tree build, table refresh, balance calc, and hotkey actions.

### Accounts (hardcoded)
`ACCOUNT_NAMES = {"CC": "Cuenta Corriente", "TCN": "TC Nacional", "TCI": "TC Internacional"}` and `acc_order = ["CC", "TCN", "TCI"]` in `build_tree_structure`. These three are fixed — the root accounts can't be deleted. CC is the checking account; TCN/TCI are credit cards.

## Data model

Three tables, all created/migrated in `init_db()` (migrations are inline `ALTER TABLE` guarded by `PRAGMA table_info`):

- **transactions**: `id, period, account, type, category, description, amount, status, note, checked, date, installments_left`
  - `period` = `"YYYY-MM"` (a **statement** month, not a calendar month — see below); `type` ∈ {`Income`, `Expense`}; `status` ∈ {`Paid`, `Unpaid`}; `checked` = 0/1 (month-end reconciliation tick); `date` = `"YYYY-MM-DD"` (the real transaction date); `installments_left` = 0 for normal/recurring rows, or a positive int for an installment purchase (see below).
  - When reading via `get_transactions()`, tuple indices matter throughout the code: `tx[2]=account, tx[3]=type, tx[4]=category, tx[5]=description, tx[6]=amount, tx[7]=status, tx[8]=note, tx[9]=checked, tx[10]=date, tx[11]=installments_left`. **Append-only**: new columns go at the end of the SELECT so these indices never shift.
- **categories**: `id, account, type, category, description, is_fixed`
  - Defines the tree structure (the catalog of line items). `is_fixed` is **dead** — the pinning feature was removed; the column is kept only so existing DBs still load.
- **settings**: `key, value` — **unused**. The table is still created in `init_db()` so existing DBs load, but there are no getter/setter helpers and nothing reads it (old DBs may hold a stale `arriendo_proximo_mes` row from the removed rent reserve — ignore it).

## Key behaviors (non-obvious)

- **Tree = Account → Category → Leaf**, built in `build_tree_structure()`. A **two-pass** build aligns the green ✓ ticks to a shared column (`tick_col` derived from the widest node + `tree.guide_depth`).
- **Smart-flatten**: a category with exactly **one** item is *promoted* — rendered directly at the account level using the **category name** (not the item name), with `data["is_promoted"]=True` and no expand arrow. Multi-item categories stay as groups. Promoted nodes behave as leaves for filtering/add, but rename/delete/subtotal use the **category** name. Adding a 2nd item auto-re-nests on next rebuild.
- **Automatic carry-forward** (`carry_forward_period`): when you land on a period with **no** transactions, all line items from the most recent prior period are copied in as **Unpaid/unchecked**. No-op if the period already has any rows, so it never clobbers work. Triggered in `on_mount` and `on_period_changed` — there is no manual "generate" key anymore.
- **Cross-period traceability**: while carrying forward, the `Saldo Mes Anterior` row (account `CC`, type `Income`) doesn't just copy its old amount — its value is replaced with the **prior period's CURRENT BCC** (`compute_current_bcc()`, PAID CC transactions only), so each period's starting balance is the real settled result of the previous one, not a stale manual number. This must be CURRENT BCC and *not* REAL EXPECTED: REAL EXPECTED is a projection that already subtracts this period's pending card statements and unpaid Egresos (and adds unpaid income) — carrying that forward would count those same items again once they settle as real transactions next period. Not clamped at 0: a negative CURRENT BCC carries forward as a negative "income" row (honestly showing the next period starts in the hole), which is why the table colors income red instead of green when `amount < 0`. `compute_balance_components()` is the single shared formula used by both the UI (`calculate_balances`) and this handoff path — don't reimplement the BCC/TCN/TCI/unpaid math a third time.
- **Installment purchases** (`installments_left`): set on a transaction via the "Cuotas restantes" field in `TransactionModal` (0/blank = normal recurring row, unaffected). On each carry-forward, a row with `installments_left > 0` is copied with the count **decremented by 1**; a row already at `1` is treated as the final installment and is **not** carried forward again, so the item silently stops appearing in future periods once paid off. The underlying `categories` entry (e.g. "Iphone") isn't touched — the leaf still exists in the tree for manual re-use, it just has no transactions once installments run out.
- **Ticks (`✓`)**: per-transaction `checked` flag (toggle with `Ctrl+T`). A tree node shows a green ✓ when **all** of its transactions are checked (propagates leaf → group → account). The top bar shows `Checked X of Y`.
- **Statement-anchored periods**: the credit card closes on `STATEMENT_CLOSE_DAY` (the 24th). A transaction's `period` is **derived from its `date`** by `period_for_date()`: day ≤ 24 → that month's statement; day ≥ 25 → next month's. So a charge on 26 Jun files into `2026-07`. The `TransactionModal` collects a date (defaults to today) and shows a live "→ period YYYY-MM" hint; `period` is recomputed on every save. The top bar shows the period's real window via `period_range_label()` (e.g. `25 May - 24 Jun`). Backfill on migration set existing rows to `period + "-15"` (the 15th maps back to the same period, so nothing reshuffles). Mental model: the 5th-of-month paycheck inside a period funds that period's statement, due the 8th of the *next* month.
- **Balances** (`calculate_balances`, shared via `compute_balance_components()`): `CURRENT BCC = CC income − CC expense, PAID only` (`compute_current_bcc()`). `TCN`/`TCI` are each the **full statement total** (paid + unpaid) — treated as a full draw against current funds, not just what's still unpaid. `EGRESOS CC UNPAID` is CC's own `Egresos`-category expenses (e.g. Gastos Comunes) still marked Unpaid this period. `INGRESOS CC UNPAID` is CC income still marked Unpaid (e.g. a paycheck not yet received). `REAL EXPECTED = CURRENT BCC + INGRESOS CC UNPAID − TCN − TCI − EGRESOS CC UNPAID` — the projected end-of-period scenario, so *every* pending (not paid) value counts: pending income adds, pending expenses subtract. A dim breakdown line sits above it. The rent reserve (ARRIENDO PRÓX.) is no longer part of this formula. When `REAL EXPECTED < 0` it turns **red with a `⚠ LIFEGUARD` flag** — the user's rule is to fund the statement from current money, so negative means they'd have to dip into next month's pay (emergency only). There is no separate "BCC EXPECTED" (paid+unpaid) metric anymore — it was removed as confusing/unused; only CURRENT BCC (settled) feeds REAL EXPECTED and the Saldo Mes Anterior handoff.
- **Rent reserve (removed)**: REAL EXPECTED used to also subtract an `ARRIENDO PRÓX.` reserve for next month's rent (rent debits on the 3rd, before the 5th paycheck). That deduction, its sidebar label and its click-to-edit manual fallback are gone; rent now only counts as the period's own `Arriendo` CC expense like any other line item. Its helpers (`get_line_item_amount()`, `next_period()`, `get_setting()`/`set_setting()`, `ARRIENDO_COLOR`) were deleted too.

## Dashboard (`Ctrl+D`)

`DashboardScreen` is a separate full-screen `Screen` (pushed via `action_show_dashboard`, not a modal), snapshotting the period selected on the main screen at open time — it doesn't live-update if you switch periods (there's no period selector on the dashboard itself yet).

- **Color scheme**: deliberately muted ("less cyberpunk") — the title (`#dash-title`) and section titles (`.dash-section-title`) and each group's header are all `DIM_WHITE`/`DIM_WHITE_LOW`, not the accent colors. There's no CURRENT BCC / REAL EXPECTED summary line anymore (removed as redundant with the bar); `real_expected` is still computed because the bar's Disponible/Faltante figure needs it.
- **Per-group rows** (`get_group_summaries()`): every `(account, category)` group — including single-item categories that the tree promotes/flattens — gets a dim-white header plus a bar row built the **same way as the balance bar**: a `.grp-box` (rounded dim-cyan border, fixed `width: 42`) containing one `.grp-fill` `Static` whose `styles.width` is `actual/expected` as a `%` (capped at 100 — never overflows), colored by `group_style()`. The `$actual / $expected` numbers sit to the right of the box (`$actual` turns red when it exceeds `$expected`).
- **"BALANCE GENERAL DEL MES" CURRENT BCC bar**: a single rounded-border box (`#dash-bcc-bar`, dim cyan) spanning the full width — a direct visualization of the REAL EXPECTED formula, not a spending-by-category breakdown. The box's width = 100% = funds = CURRENT BCC + INGRESOS CC UNPAID (so `Disponible` = REAL EXPECTED holds exactly). `compute_bcc_bar_segments()` sizes three fixed inner segments proportionally within it — `Unpaid Cta. Corriente` (gold, = EGRESOS CC UNPAID), `TCN` (purple), `TCI` (teal) — each an actual Textual widget (`Static` with `styles.width` set to a `%` string) rather than a hand-drawn block-character bar, so it's genuinely full-width responsive instead of guessing a fixed character count. The unused remainder is a green "Disponible" segment equal to REAL EXPECTED. If the three commitments exceed funds, they're rescaled to fill exactly 100% (never-overflow rule), there's no leftover segment, and the legend shows a red "⚠ Faltante" figure (= REAL EXPECTED, negative) instead. The four legend entries below (three segments + Disponible/Faltante) sit in one `#dash-legend-row` (`Horizontal`, each `.legend-item` `1fr`), each two lines (name / amount), in the same left-to-right order as the bar segments.

## Conventions

- DB functions take primitives and open/close their own connection. Keep that pattern; don't introduce a shared global connection.
- Tree node `data` dicts carry the routing flags: `is_account` / `is_category` / `is_leaf` (+ `is_promoted`). Actions branch on these.
- Colors come from the Option-B palette constants and `WALLET_THEME` (registered/applied in `on_mount`). Use the named constants, not raw hex, when adding tree/table styling. Subtle borders are `#222235`.
- Table actions guard on `table.row_count` before reading the cursor (don't wrap in broad `except`). Destructive tree/table deletes go through `ConfirmModal`.
