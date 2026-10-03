# ClaimGuard

A local agent that works a hospital's medical-necessity denial queue (lumbar MRI, CPT 72148,
CARC 50). It searches the chart for proof, routes each case APPEAL / FETCH / DROP, drafts appeal
letters whose quotes are inserted by code, ranks the queue by expected value, and a specialist
approves every letter.

Built today from `docs/SPEC.txt` with AI coding help at the event. **Every model call at runtime is
local**: the only network destination in the code is `CG_LLM_BASE_URL`.

## Model and endpoint

| | |
|---|---|
| Base URL | `http://localhost:11434/v1` (Ollama's OpenAI-compatible endpoint) |
| Model | `qwen3.5:4b` (`CG_LLM_MODEL`) |
| Max tokens | 3000 (`CG_MAX_TOKENS`, raised because this model writes reasoning first) |
| Workers | 6 (`CG_WORKERS`) |
| Reproducible "today" | 2026-10-03 (`CG_TODAY`) |

Configuration lives in `.env`, which `engine.py` loads without overriding variables already set.

## How to run

```bash
python3 make_data.py --n 60 --seed 42     # synthetic cases, deterministic
python3 -m unittest discover tests        # unit tests, no model needed
python3 cg.py selftest                    # check the endpoint, run Maria end to end
```

### Selftest result on this box

```
model: qwen3.5:4b, base URL http://localhost:11434/v1
A-1001  route APPEAL  (11.6s, 3 model calls)
argument path: CONSERVATIVE_6W + RADICULOPATHY
  CONSERVATIVE_6W  MET  PT1-2, MED-1, MED-2   (source: search)
  RADICULOPATHY    MET  VN-1                  (source: submitted)
  RED_FLAG         NOT_FOUND
search trail: CONSERVATIVE_6W 'physical therapy duration weeks' -> PT1-2, MED-1, MED-2, PT2-1, PT2-2
invalid citations 0, downgrades 0, expected value 529.20 USD
```

The agent searches the chart, and every citation it keeps is verified. On citations this
model is weaker than the spec's example: the search returns PT2-1 and PT2-2, but the model
cites PT1-2, MED-1 and MED-2 instead of the PT2 pair. MED-1 is in Maria's gold evidence and
the other two genuinely show six weeks of care (an eight-week plan of care, naproxen from
2026-06-02 against an order date of 2026-08-07), so the route is right and the evidence
holds up, but a stronger model would be expected to quote the discharge summary. Only
`qwen3.5:4b` and the `bge-m3` embedding model are installed on this box.

## Deviations from spec

The spec allows keeping the intent of a rule and recording the change here.

| Spec says | ClaimGuard does | Why |
|---|---|---|
| vLLM via NemoClaw at `localhost:8000/v1` | Ollama at `http://localhost:11434/v1`, `CG_LLM_API_KEY=ollama` | Ollama is installed and serving on this box. Same API shape, so switching back to vLLM is one environment variable |
| `server.py` using `http.server` | `review_app.py` using Streamlit, reading the same `out/` folder | The team already has a working Streamlit dashboard. Approve/Reject call the same `engine.approve` as the CLI |
| Standard library only | Engine, CLI, generator and tests are standard library only. Only the review page uses Streamlit | `cg.py` must run inside the sandbox with no pip; the review page runs on the host |
| SQLite store from the earlier build | Not used. Case JSON files plus `out/audit.jsonl` are the store | Fewer moving parts |
| (no telemetry) | `.streamlit/config.toml` sets `gatherUsageStats = false` and binds to `127.0.0.1:8080` | Hard rule 1 |
| `CG_MAX_TOKENS` default 1200, keep `<think>` stripping | `CG_REASONING_EFFORT=none` is sent in the request body, and `CG_MAX_TOKENS=3000` | `qwen3.5:4b` is a thinking model. Ollama returns its chain of thought in `message.reasoning` and leaves `content` empty, so without this the model spent the whole 3000-token budget thinking and never answered (`finish_reason: length`, 88 s per call). Ollama honours `reasoning_effort: "none"`: the same call then answers in 5 s. `<think>` stripping and the `reasoning` fallback are both still in place for other servers |
| Evidence loop: a final reply is accepted once it parses | When the final reply cites sentence IDs the model was never shown and searches remain, the loop names the dropped citations and asks for a search instead of accepting the answer (at most 2 such corrections, `MAX_CORRECTIONS`) | The small model copied the sentence IDs out of the prompt's own reply-format example instead of searching, so Maria was routed FETCH on dropped citations. The correction gives the model the step rule 4 already asks of it. **The verifier is unchanged** and still drops every unshown or invented ID; this only costs a turn before that happens |
| Case dates independent of route | Three showcase cases (A-1004, A-1009, A-1015) are Harborline with denial dates 2026-08-04…08-09 | Only Harborline's 60-day window can produce an urgent deadline from an August–September denial. The demo needs three cases due within seven days. All other dates and all dollar amounts stay independent of the route, so the benchmark remains fair |

Harborline cases denied before 2026-08-04 fall outside the 60-day window and are **expired**. That
is realistic, so they are left in the data and the engine flags them.

## Commands

```bash
python3 cg.py selftest                     # endpoint, model, one case end to end
python3 cg.py run --new                    # every case with status new
python3 cg.py run --ids A-1001..A-1020     # the showcase set
python3 cg.py run --ids A-1001,A-1013 --workers 6
python3 cg.py explain A-1001               # route, evidence, search trail (no patient name)
python3 cg.py approve A-1001 --by "Dana Lee"
python3 cg.py reject A-1001 --by "Dana Lee" --reason "wrong patient"
python3 cg.py digest                       # the chat-safe summary
python3 cg.py reset                        # cases back to new, out/ cleared except audit.jsonl
```

Exit codes: 0 ok, 2 unknown claim or bad status, 1 anything else.

## Proof points

- **Local only.** `grep -rnE "https?://" *.py` returns `localhost` plus two expected hits: the
  planted URL inside A-1013's payer letter and the injection-detection pattern list.
- **The model never types a quote.** The letter writer only emits `[[PT2-1]]` placeholders; code
  substitutes the exact sentence. `quote_check` re-reads the finished letter and asserts every
  double-quoted string is a chart sentence, character for character. `quote_violations` is 0 on
  every output.
- **Code decides the route**, not the model: `engine.route` over three-valued statuses.
- **The verifier does real work.** Citations that do not exist or were never shown are dropped and
  counted; a MET with no surviving evidence is downgraded to NOT_FOUND.
- **A person approves every letter.** `engine.approve` requires a name, refuses a case that is not
  `draft`, re-renders the letter with the approval line, re-runs the quote check and writes an
  audit event. Approved is only a status; nothing is sent anywhere.
- **Append-only audit log.** `out/audit.jsonl` is opened in append mode behind a lock and is never
  rewritten, including by `reset`.
- **The digest carries no patient data** (hard rule 7). A unit test asserts `digest.txt` contains no
  claim id, member id, ICD-10 code or patient name, checked against all 60 generated cases.

### A note on what the audit log holds

The audit log records prompts and replies as SHA-256 only, and no patient name, member id, date of
birth or ICD-10 code is ever passed to `audit()`. The one chart-derived free text in the log is
`raw_final`, the model's own final reply, which the spec requires ("keep the raw model reply in the
audit log"). It stays local: `out/` is gitignored and never leaves the box. The boundary that hard
rule 7 governs is `digest.txt`, the only artifact meant for a chat channel, and that is covered by
a test.

## Data

`data/criteria.json` (5 criteria), `data/policies.json` (3 fictional payers) and
`data/cases/A-1001.json` … `A-1060.json`.

- Synthetic only: Demo General Hospital, fictional payers, fictional patients (hard rule 8).
- Gold answer labels live under `gold` in each case file and are never shown to the model.
- Case mix: 32 APPEAL (12 with proof in the submitted note, 16 needing search, 4 red-flag), 14 FETCH,
  14 DROP; 20 cases per payer.
- A-1001 (Maria Alvarez) and A-1013 (James Okafor) are written by hand, exactly as the spec describes.
  A-1013's payer letter carries the planted prompt-injection paragraph; its gold `injection` is true.
- Gold routes are computed with `engine.route`, the same function the engine uses at run time.
- `python3 make_data.py --n 60 --seed 42` is deterministic: two runs produce byte-identical files.

## Status

- [x] Brief 1: criteria, policies, 60 synthetic cases, router, generator tests
- [x] Brief 2: engine core (model client, chart search, evidence loop, verifier, router, scoring)
- [x] Brief 3: letters, requests, memos, quote check, queue, digest, full CLI
- [ ] Brief 4: review page
- [ ] Brief 5: eval, benchmark, SKILL.md, sandbox proofs
