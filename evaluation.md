# Evaluation: PR Context Retrieval

Three examples from the included sample repository, all run with the default 8,000-token budget.
Each shows: what was retrieved, why, token usage, and what was intentionally left out.

---

## Example 1 — Bug Fix: Add Logging to Auth Failure Path

**Diff:** `auth/service.py` · +9/−2 lines · functions: `verify_password`, `authenticate`

The PR adds a `logging.warning()` call to the failed-login branch and changes the error
message string from `"Invalid password"` to `"Invalid credentials"`.

### Retrieval Plan

| # | Category | Source | Why |
|---|---|---|---|
| 1 | `function_body` | auth/service.py | Full body of `verify_password` — the function directly called by `authenticate`; needed to see whether the hash comparison is timing-safe |
| 2 | `function_body` | auth/service.py | Full body of `authenticate` — the directly modified function; lets the reviewer see all return paths, not just the changed lines |
| 3 | `call_site` | tests/test_auth.py | `verify_password` called here; reviewers can see the test assertions to check whether the changed error message breaks any expectations |
| 4 | `call_site` | tests/test_auth.py | `authenticate` called here; the test asserts on `result.success` and `result.session`; changing error strings doesn't affect these, but showing them confirms it |
| 5 | `test` | tests/test_auth.py | Full test file; behavioral contract for all auth paths including the lock-out and disabled-account cases touched by `authenticate` |
| 6 | `type_def` | auth/models.py | `LoginResult` is referenced in changed lines; LLM needs to know it has an optional `session` field to reason about the `None` case |
| 7 | `import` | auth/service.py | Import list shows `logging` was just added — reviewer can flag whether `logging.getLogger(__name__)` is at module level (it is) vs. inside the function |

**Token usage:**
```
Context budget:     8,000 tokens
Context used:         888 tokens  (7 items)
Diff:                 325 tokens
System prompt:        200 tokens
Estimated total:    1,413 tokens
```

### What Was Not Retrieved

| Excluded | Reason |
|---|---|
| `auth/models.py` full file | Only the `LoginResult` definition was needed, not `User` or `Session`; type_def search returns a targeted 20-line extract, not the whole file |
| `hash_password` function body | Not in `changed_functions`; the diff doesn't touch it; call-site search for `verify_password` is sufficient to show it's used in tests |
| `refund_payment`, other payment code | Unrelated file, zero overlap with changed symbols — would dilute the review with noise |

**Tradeoff observed:** The `verify_password` function (2 lines) and `authenticate` (15 lines) together cost 105 + 716 = 821 chars. Sending both at priority 1 is cheap and correct; a reviewer cannot judge the authenticate change without seeing its full body.

---

## Example 2 — Refactor: Payment Processor + Session Validation

**Diff:** `payments/processor.py` · +23/−8 lines · functions: `_validate_payment_amount` (new), `process_payment` (signature change)

The PR adds a session-validity check before the role check, extracts amount validation
into a helper, and adds `Session` as a required parameter to `process_payment`.

### Retrieval Plan (default 8,000-token budget)

| # | Category | Source | Why |
|---|---|---|---|
| 1 | `function_body` | payments/processor.py | Full body of `process_payment` — the function with a changed signature; caller-facing contract changed (new required `session` arg) |
| 2 | `call_site` | tests/test_payments.py | Every existing test calls `process_payment(user, amount)` without a `session` arg — this context immediately surfaces that all existing tests will break |
| 3 | `test` | tests/test_payments.py | Full test file; shows all the amount validation edge cases; reviewer can check whether `_validate_payment_amount` correctly replaces the inline checks |
| 4 | `type_def` | payments/processor.py | `PaymentResult` referenced in changed lines; reviewers need to know it has a `transaction_id` field to assess whether the refactored validation paths still populate it |
| 5 | `import` | payments/processor.py | Import of `is_session_valid` and `Session` just added; confirms the coupling to auth module |

**Token usage:**
```
Context budget:     8,000 tokens
Context used:         763 tokens  (5 items)
Diff:                 412 tokens
System prompt:        200 tokens
Estimated total:    1,375 tokens
```

### Budget Enforcement — Tight Budget Scenario

Running with `--budget 700` shows the priority ordering under pressure:

```
Budget:    700 tokens
Selected:  593 tokens (4 items) — function_body, call_site, test, import
Excluded:    1 item   (~170 tokens)

  - [type_def] payments/processor.py (~170 tokens)
    Reason for exclusion: budget exhausted after higher-priority items
```

The `type_def` (`PaymentResult`) is the right item to drop first. The LLM can infer that
`PaymentResult.success` and `PaymentResult.transaction_id` exist from the call sites and
test assertions it already has. The definition adds precision but not understanding.

### What Was Not Retrieved

| Excluded | Reason |
|---|---|
| `_validate_payment_amount` body | New function added by the PR; it exists in the diff itself, so retrieving it again from the file would duplicate content already visible to the reviewer |
| `auth/service.py` → `is_session_valid` body | The import was just added; retrieving the callee body could be useful but its definition is simple (a single timestamp comparison). Kept out to avoid scope creep; a reviewer can follow the import if needed |
| `refund_payment` | Not in `changed_functions`; grep confirms it appears only in the test file, not as a call site of any changed function |

---

## Example 3 — New Feature: Audit Logging

**Diff:** `auth/service.py` · +23/−2 lines · functions: `write_audit_log` (new), `authenticate` (modified)

The PR adds an audit logging function and calls it from `authenticate` on both success and
failure paths. It also adds `AUDIT_LOG_PATH` read from an env variable.

### Retrieval Plan

| # | Category | Source | Why |
|---|---|---|---|
| 1 | `function_body` | auth/service.py | Full body of `verify_password` — unchanged, but called inside `authenticate`; confirms the auth logic the audit log annotates |
| 2 | `function_body` | auth/service.py | Full body of `authenticate` — directly modified to call `write_audit_log`; reviewer needs to see all branches to verify audit coverage is complete |
| 3 | `call_site` | tests/test_auth.py | `verify_password` called in tests; confirms test expectations around the auth flow being audited |
| 4 | `call_site` | tests/test_auth.py | `authenticate` called in tests; reviewer can see whether tests will need updating to mock or assert on the new audit calls |
| 5 | `test` | tests/test_auth.py | Full test suite; shows there are no existing tests for `write_audit_log` — a natural finding: "no test coverage for the new audit function" |
| 6 | `import` | auth/service.py | `json` and `os` just imported; reviewer can assess whether `os.getenv` is the right approach for log path configuration |

**Token usage:**
```
Context budget:     8,000 tokens
Context used:         857 tokens  (6 items)
Diff:                 509 tokens
System prompt:        200 tokens
Estimated total:    1,566 tokens
```

### What Was Not Retrieved

| Excluded | Reason |
|---|---|
| `write_audit_log` body | The new function is entirely contained in the diff; retrieving it from the post-PR file would duplicate content already present |
| `AUDIT_LOG_PATH` module-level constant | Extracted from the diff; not a function, not a type — nothing to look up |
| Other service modules | `payments/processor.py` has no overlap with changed symbols; excluded to preserve budget for higher-signal items |

**Key finding this retrieval enables:** The test file (item 5) shows there are zero tests for
`write_audit_log`. Without this context, the LLM reviewing only the diff would have to guess.
With the test file in context, it can confidently flag: "The audit log writer has no test
coverage, and the `except OSError` branch that silently swallows errors is untested."

---

## Scalability on Large Open-Source Repos

The three examples above use a synthetic 5-file repository. Here's what changes on a real
codebase (e.g., Django, ~3,000 Python files):

**Without the grep optimization (original implementation):**
- `find_call_sites("authenticate")` opens all ~3,000 .py files
- ~3,000 file reads × ~10ms each ≈ 30 seconds per function

**With the grep pre-filter (current implementation):**
```
grep -rl "authenticate" django/ --include="*.py" → 12 files
```
- Only the 12 matching files are opened and parsed
- End-to-end call-site search: < 1 second

The grep subprocess uses OS memory-mapped I/O and scans files in parallel. It returns only
the file names, not their contents — no 3,000 file reads in Python.

This optimization is transparent: if `grep` is unavailable (Windows, restricted CI), the
code falls back to the Python file walk automatically.

**Git co-change on real repos:** A repository with 1,000 commits of history shows coupling
that no static analysis can find — for example, `settings.py` and `urls.py` co-changing
alongside `views.py` in 80% of feature commits. The `find_cochanged_files()` function
surfaces these patterns at no API cost (pure git log traversal).

---

## Summary of Design Tradeoffs

| Decision | Alternative | Why we chose this |
|---|---|---|
| Top 3 call sites only | All call sites | Prevents a widely-used utility function (e.g., `log`) from flooding the budget with 500 call sites |
| Top 2 test files | All test files | Test files are large; two give the behavioral contract without exhausting the budget on fixtures |
| CamelCase heuristic for type extraction | Full type checker | Type checker is slow and requires environment setup; heuristic is good enough for the top 5 candidates, false positives waste <200 tokens each |
| 4-char/token estimate | Exact tokenizer | Exact counting requires an API call per item; the estimate is within 15% and prevents budget overshoot |
| Snippet (±6 lines) for call sites | Full function body | Full body would use 5-10× more tokens; ±6 lines shows the call and its immediate context |
| Priority drop order: import > type_def > test > call_site > function_body | Equal priority | Forces function_body to always fit; ensures at least the changed function is in context even at tiny budgets |
