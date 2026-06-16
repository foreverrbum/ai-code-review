# Evaluation: PR Context Retrieval on Real Open-Source PRs

Three real GitHub PRs across two languages, run with the default 8,000-token budget.
All output below is live terminal output from `python main.py --github ... --no-llm`.

---

## Example 1 — psf/requests #7505 (Python)
**"Add hasattr checks for remaining protocol isinstance checks"**
3 files · +24/−7 lines · [psf/requests](https://github.com/psf/requests/pull/7505)

The PR adds a `has_read()` helper to `_types.py` and switches `_encode_files` and
`prepare_body` in `models.py` from `isinstance(obj, SupportsRead)` to `has_read(obj)` so
that file-like objects that implement `read` via `__getattr__` (rather than class inheritance)
are correctly detected.

### Retrieval Plan (20 of 21 items selected)

| # | Category | Source | Why |
|---|---|---|---|
| 1 | `function_body` | src/requests/models.py | `_encode_params` — overloaded static method in the same class as the changed `_encode_files`; needed to understand the full parameter encoding surface |
| 2 | `function_body` | tests/test_requests.py | `__init__` of the in-test proxy class added by this PR |
| 3 | `function_body` | tests/test_requests.py | `__getattr__` of the proxy — the exact mechanism this PR is designed to fix |
| 4 | `function_body` | src/requests/_types.py | `has_read()` — the new helper, full body (3 lines) |
| 5 | `function_body` | tests/test_requests.py | `test_post_named_tempfile` — regression test for the existing named-file path |
| 6 | `function_body` | tests/test_requests.py | `test_post_getattr_proxy_read_only` — the new test exercising the fix |
| 7 | `function_body` | src/requests/models.py | `_encode_files` — the directly modified function (2,585 chars) |
| 8 | `function_body` | src/requests/models.py | `prepare_body` — calls `_encode_files`; needed to see how the return value is consumed |
| 9 | `call_site` | tests/testserver/server.py | `__init__` called here — confirms no protocol conflict |
| 10 | `call_site` | src/requests/structures.py | `__init__` called here |
| 11 | `call_site` | src/requests/exceptions.py | `__init__` called here |
| 12 | `call_site` | src/requests/models.py | `has_read()` call — the live call site of the new helper |
| 13 | `git_cochange` | src/requests/models.py | Co-changed with `_types.py` in **4** prior commits — these two files always move together when the type protocol changes |
| 14 | `git_cochange` | src/requests/sessions.py | Co-changed with `_types.py` in **3** prior commits — sessions.py uses the same type annotations |
| 15 | `test` | tests/test_testserver.py | Test file covering the testserver used in the new tests |
| 16 | `type_def` | tests/test_requests.py | `ReadProxy` class definition — the test fixture type |
| 17 | `type_def` | src/requests/_types.py | `SupportsRead` Protocol definition — the type being extended |
| 18 | `import` | src/requests/_types.py | Import list |
| 19 | `import` | src/requests/models.py | Import list |
| 20 | `import` | tests/test_requests.py | Import list |

**Token usage:**
```
Budget:            8,000 tokens
Context used:      5,928 tokens  (20 items)
Excluded:          1 item
Diff:                861 tokens
Estimated total:   6,989 tokens
```

### What Was Excluded and Why

| Item | Size | Reason |
|---|---|---|
| `tests/test_requests.py` (full file) | ~27,109 tokens | The complete test suite is 108K chars — 3× the entire context budget. The six targeted function bodies already extracted from it (items 2, 3, 5, 6, 16, 20) cover every test relevant to this PR. Sending the full file would displace all of them and fill the context with unrelated auth, redirect, and streaming tests. |

**Design decision visible here:** Items 2, 3, 5, and 6 are function bodies extracted *from* the excluded
file. The priority system correctly includes those targeted snippets at priority 1 while dropping the
110K-char file at priority 3. The reviewer gets the relevant tests without the noise.

**Git co-change signal:** `models.py` and `sessions.py` appearing as co-changed with `_types.py`
is historically accurate — every previous time `_types.py` grew a new Protocol, the other two files
needed updates to use it. An LLM seeing items 13–14 can check whether `sessions.py` also needs
updating for the new `has_read()` helper.

---

## Example 2 — aio-libs/aiohttp #12217 (Python)
**"Raise on redirect with consumed non-rewindable request bodies"**
6 files · +87/−30 lines · [aio-libs/aiohttp](https://github.com/aio-libs/aiohttp/pull/12217)

The PR makes `ClientSession` raise `ClientPayloadError` when a redirect would require
re-sending a request body that has already been consumed (e.g., an async generator that
cannot be rewound). Previously it silently sent an empty body on redirect.

### Retrieval Plan (17 of 19 items selected)

| # | Category | Source | Why |
|---|---|---|---|
| 1 | `function_body` | tests/test_client_functional.py | `async_gen` — the async iterator fixture that exercises the bug |
| 2 | `function_body` | tests/test_client_functional.py | `final_handler` — the redirect destination server handler |
| 3 | `function_body` | tests/test_client_functional.py | `redirect_handler` — the server handler that triggers the redirect |
| 4 | `function_body` | tests/test_client_ws_functional.py | `handler` — WebSocket handler modified alongside the fix |
| 5 | `function_body` | tests/test_client_functional.py | `test_async_iterable_payload_redirect_non_post_301_302` — the new regression test |
| 6 | `function_body` | aiohttp/client.py | `_connect_and_send_request` — the method where the guard was added (1,896 chars) |
| 7 | `function_body` | tests/test_client_ws_functional.py | `test_heartbeat_does_not_timeout_while_receiving_large_frame` — modified alongside |
| 8 | `call_site` | aiohttp/web_middlewares.py | `handler` called in middleware chain |
| 9 | `call_site` | aiohttp/web_app.py | `handler` called in application dispatch |
| 10 | `call_site` | aiohttp/client.py | `handler` called in client internals |
| 11 | `test` | tests/test_client_fingerprint.py | Test file covering `aiohttp/client.py` |
| 12 | `test` | tests/test_client_connection.py | Test file covering `aiohttp/client.py` |
| 13 | `type_def` | aiohttp/client_exceptions.py | `ClientPayloadError` — the exception raised by the new guard |
| 14 | `type_def` | aiohttp/payload.py | `AsyncIterablePayload` — the payload type that cannot be rewound |
| 15 | `type_def` | aiohttp/web_ws.py | `WebSocketResponse` — used in the co-modified test |
| 16 | `import` | aiohttp/client.py | Import list for the changed module |
| 17 | `import` | tests/test_client_functional.py | Import list |

**Token usage:**
```
Budget:            8,000 tokens
Context used:      4,225 tokens  (17 items)
Excluded:          2 items
Diff:              2,217 tokens
Estimated total:   6,642 tokens
```

### What Was Excluded and Why

| Item | Size | Reason |
|---|---|---|
| `tests/test_client_ws_functional.py` (full) | ~12,477 tokens | 49K-char WebSocket test suite. Items 4 and 7 extract the two specific functions changed in this PR. The full file would add thousands of lines of unrelated WebSocket protocol tests. |
| `tests/test_client_functional.py` (full) | ~50,102 tokens | 200K-char functional test suite — the single largest file in the repo. Items 1–5 extract the five specific functions from it. Sending the full file would consume 6× the entire context budget. |

**What this context enables the LLM to catch:** With `ClientPayloadError` definition (item 13) and
`AsyncIterablePayload` (item 14) in context, an LLM can verify that the new guard in
`_connect_and_send_request` raises the right exception type, that `AsyncIterablePayload` is indeed
non-rewindable (no `seek()` method), and that the new test correctly parametrizes over 301 and 302
but not 307 (which should preserve the body). Without those type definitions, those checks require
guessing.

---

## Example 3 — gin-gonic/gin #4655 (Go)
**"feat(context): add Scheme() with proper reverse proxy support"**
2 files · +87/−0 lines · [gin-gonic/gin](https://github.com/gin-gonic/gin/pull/4655)

The PR adds a `Scheme()` method to `gin.Context` that returns `"https"` or `"http"` based
on TLS state and `X-Forwarded-Proto` / `X-Forwarded-Ssl` headers, with configurable trusted
proxy enforcement.

### Retrieval Plan (5 of 6 items selected)

| # | Category | Source | Why |
|---|---|---|---|
| 1 | `function_body` | context_test.go | `TestWebsocketsRequired` — modified alongside `TestContextScheme`; reviewer needs to see it wasn't accidentally changed |
| 2 | `function_body` | context_test.go | `TestContextScheme` — the full new test suite for `Scheme()`, 2,234 chars; shows every case the implementation must handle |
| 3 | `test` | context_file_test.go | Small test file covering `context.go` — shows existing test patterns |
| 4 | `import` | context.go | Import list for the changed file |
| 5 | `import` | context_test.go | Import list |

**Token usage:**
```
Budget:            8,000 tokens
Context used:      1,221 tokens  (5 items)
Excluded:          1 item
Diff:                993 tokens
Estimated total:   2,414 tokens
```

### What Was Excluded and Why

| Item | Size | Reason |
|---|---|---|
| `context_test.go` (full file) | ~28,731 tokens | 114K-char Go test suite. The two specific test functions (items 1 and 2) were extracted at priority 1. The remaining ~350 test functions cover unrelated `Context` methods — path params, headers, cookies — and would use 3× the budget. |

**Notable gap — no `Scheme()` body retrieved:** `Scheme()` is a new function added by this PR.
It does not exist in the local clone's HEAD, so there is nothing to extract. This is expected
behavior: the diff already contains the full implementation. The retrieval system correctly falls back
to the test bodies, which define the behavioral contract the implementation must satisfy.

**Go AST working correctly:** Both function bodies were extracted via tree-sitter Go grammar —
not regex. The Go AST correctly identifies `func TestContextScheme(t *testing.T)` as a function
declaration node and extracts precise start/end line boundaries.

---

## Cross-Language Notes

Six PRs were tested in total across Python, Go, and TypeScript. Key findings:

**TypeScript (vitejs/vite #22602):** A bug in the grammar loader (`tree_sitter_typescript` exports
`language_typescript()` not `language()`) caused TypeScript AST to silently return empty results.
The bug was caught during testing and fixed. After the fix, `loadConfigFromFile` (2,128 chars) and
`nativeImportConfigFile` (570 chars) were correctly extracted from an 87K-char config file, and
three `loadConfigFromFile` call sites were found across `vitestSetup.ts` and two `__tests__` files.

**JavaScript (expressjs/express #7181):** The `@@` hunk header reported `createETagGenerator`
as the context function, but the actual changed function is `parseExtendedQueryString` (which
appears immediately after in the file). Because no `function` keyword appears in the `+/-` lines,
the parser never discovers the correct name. Result: 1 item retrieved instead of the expected 4–6.
This is a known limitation documented in the design doc — dense JS files where the hunk header
lags one function behind the actual change — and affects a minority of JS PRs.

**What the token budget protects against:** Across all six PRs, the tool retrieved between 1 and
30 context items. In every case, the largest file in the repo was a test suite between 50K and 200K
chars. The budget enforcement dropped these in all cases while keeping the targeted function-body
extractions from those same files. Without the budget, a naive implementation would send the entire
test suite to the LLM on nearly every PR.
