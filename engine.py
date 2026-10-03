"""ClaimGuard engine: config, chart search, evidence loop, verifier, router, outputs, audit.

Standard library only (hard rule 2). The only network destination is CG_LLM_BASE_URL
(hard rule 1), which is the local OpenAI-compatible inference endpoint.
"""

import os
import json
import re
import time
import math
import hashlib
import threading
import urllib.request
import urllib.error
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parent
DATA, OUT, PROMPTS = ROOT / "data", ROOT / "out", ROOT / "prompts"


def load_env(path=None):
    """Load KEY=VALUE lines from .env without overriding variables already set."""
    env_path = Path(path) if path else ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env()

BASE_URL = os.getenv("CG_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
API_KEY = os.getenv("CG_LLM_API_KEY", "ollama")
MAX_TOKENS = int(os.getenv("CG_MAX_TOKENS", "1200"))
WORKERS = int(os.getenv("CG_WORKERS", "6"))
TODAY = date.fromisoformat(os.getenv("CG_TODAY", "2026-10-03"))

MET, NOT_MET, NOT_FOUND = "MET", "NOT_MET", "NOT_FOUND"
STATUSES = (MET, NOT_MET, NOT_FOUND)

# win_score by route. 0.54 is the share of private-payer denials Premier Inc. (2024)
# reports as eventually overturned; 0.20 for FETCH is a team assumption. Both are
# labelled as assumptions wherever they are shown.
WIN_SCORE = {"APPEAL": 0.54, "FETCH": 0.20, "DROP": 0.0, "MANUAL": 0.0}
ASSUMPTIONS = {
    "win_score_appeal": 0.54,
    "source": "Premier Inc. 2024",
    "win_score_fetch": 0.20,
    "source_fetch": "team assumption",
}


# --------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------

def load_criteria():
    return json.loads((DATA / "criteria.json").read_text())


def load_policies():
    return json.loads((DATA / "policies.json").read_text())


def criteria_by_id():
    return {c["id"]: c for c in load_criteria()}


def policy_by_id(policy_id):
    for policy in load_policies():
        if policy["policy_id"] == policy_id:
            return policy
    raise KeyError("unknown policy %s" % policy_id)


def case_path(claim_id):
    return DATA / "cases" / ("%s.json" % claim_id)


def load_case(claim_id):
    return json.loads(case_path(claim_id).read_text())


def all_claim_ids():
    return sorted(p.stem for p in (DATA / "cases").glob("*.json"))


def write_json_atomic(path, payload):
    """Write JSON through a temp file plus os.replace, so readers never see a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(str(tmp), str(path))


def policy_criteria_ids(policy):
    """Criterion IDs that appear in any of the policy's paths, in first-seen order."""
    ids = []
    for path in policy["paths"]:
        for cid in path:
            if cid not in ids:
                ids.append(cid)
    return ids


# --------------------------------------------------------------------------
# three-valued router (spec: "Verification and the three-way router")
# --------------------------------------------------------------------------

def route(policy_paths, statuses, evidence_counts=None):
    """Decide APPEAL / FETCH / DROP from three-valued criterion statuses.

    MET is true, NOT_MET is false, NOT_FOUND is unknown. A path is true when all of
    its criteria are true, false when any is false, otherwise unknown. The policy is
    true if any path is true, false if every path is false, otherwise unknown.

    The generator uses this same function to compute gold routes.
    """
    evidence_counts = evidence_counts or {}

    def truth(cid):
        status = statuses.get(cid, NOT_FOUND)
        if status == MET:
            return True
        if status == NOT_MET:
            return False
        return None

    path_truth = []
    for path in policy_paths:
        values = [truth(cid) for cid in path]
        if all(v is True for v in values):
            path_truth.append(True)
        elif any(v is False for v in values):
            path_truth.append(False)
        else:
            path_truth.append(None)

    in_policy = policy_criteria_ids({"paths": policy_paths})

    if any(v is True for v in path_truth):
        # The argument path is the true path with the most evidence sentences;
        # ties go to the earlier path.
        best, best_count = None, -1
        for path, value in zip(policy_paths, path_truth):
            if value is not True:
                continue
            count = sum(evidence_counts.get(cid, 0) for cid in path)
            if count > best_count:
                best, best_count = path, count
        return {"route": "APPEAL", "argument_path": list(best), "missing": [], "drop_reasons": []}

    if all(v is False for v in path_truth):
        reasons = [cid for cid in in_policy if statuses.get(cid, NOT_FOUND) == NOT_MET]
        return {"route": "DROP", "argument_path": [], "missing": [], "drop_reasons": reasons}

    # Unknown: fetch the missing documents of the most nearly satisfied path, which is
    # the path with no false criterion and the fewest unknown ones; ties go earlier.
    best, best_unknowns = None, None
    for path, value in zip(policy_paths, path_truth):
        if value is not None:
            continue
        unknowns = [cid for cid in path if truth(cid) is None]
        if best_unknowns is None or len(unknowns) < len(best_unknowns):
            best, best_unknowns = path, unknowns
    return {"route": "FETCH", "argument_path": [], "missing": list(best_unknowns), "drop_reasons": []}


# --------------------------------------------------------------------------
# audit log (append only, never rewritten -- hard rule 9)
# --------------------------------------------------------------------------

_AUDIT_LOCK = threading.Lock()
_RUN_ID = "adhoc"


def set_run_id(run_id):
    global _RUN_ID
    _RUN_ID = run_id


def current_run_id():
    return _RUN_ID


def audit(event, claim_id=None, **details):
    """Append one JSON line to out/audit.jsonl. Never truncates or rewrites."""
    line = json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run_id": _RUN_ID,
        "claim_id": claim_id,
        "event": event,
        "details": details,
    }, sort_keys=True)
    with _AUDIT_LOCK:
        OUT.mkdir(parents=True, exist_ok=True)
        with open(str(OUT / "audit.jsonl"), "a") as handle:
            handle.write(line + "\n")
            handle.flush()


def sha256(text):
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


# --------------------------------------------------------------------------
# local model client -- the only network destination is BASE_URL (hard rule 1)
# --------------------------------------------------------------------------

_MODEL_NAME = None
_SEND_REASONING = os.getenv("CG_REASONING_EFFORT") or None


def _post(url, payload, timeout=180):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % API_KEY})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def list_models():
    request = urllib.request.Request(
        BASE_URL + "/models", headers={"Authorization": "Bearer %s" % API_KEY})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return [item["id"] for item in payload.get("data", [])]


def model_name():
    """CG_LLM_MODEL, or the first model the endpoint lists. Cached."""
    global _MODEL_NAME
    if _MODEL_NAME:
        return _MODEL_NAME
    configured = os.getenv("CG_LLM_MODEL")
    if configured:
        _MODEL_NAME = configured
        return _MODEL_NAME
    models = list_models()
    if not models:
        raise RuntimeError("no models listed at %s" % BASE_URL)
    _MODEL_NAME = models[0]
    return _MODEL_NAME


def clean(text):
    """Strip hidden reasoning in think tags and code fences."""
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # a dangling <think> with no closing tag: drop everything after it
    text = re.sub(r"<think>.*$", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"</?think>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"```[a-zA-Z0-9_-]*", " ", text)
    return text.strip()


def first_json(text):
    """Return the first balanced JSON object in text, or None.

    Walks the braces while respecting strings and escapes, so braces inside string
    values do not end the object early.
    """
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:index + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def chat(messages):
    """One call to the local chat-completions endpoint. Temperature 0, one retry."""
    global _SEND_REASONING
    payload = {
        "model": model_name(),
        "messages": messages,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    if _SEND_REASONING:
        payload["reasoning_effort"] = _SEND_REASONING

    prompt_blob = json.dumps(messages, sort_keys=True)
    url = BASE_URL + "/chat/completions"
    started = time.time()
    last_error = None
    for attempt in (0, 1):
        try:
            data = _post(url, payload)
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and "reasoning_effort" in payload:
                # this server rejects the field: drop it and stop sending it
                payload.pop("reasoning_effort", None)
                _SEND_REASONING = None
                last_error = exc
                continue
            raise
        except (urllib.error.URLError, ConnectionResetError, TimeoutError) as exc:
            last_error = exc
            if attempt == 1:
                raise
            time.sleep(1.0)
    else:
        raise last_error if last_error else RuntimeError("model call failed")

    ms = int((time.time() - started) * 1000)
    text = ""
    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        # Thinking models return the chain of thought in a separate field and leave
        # content empty. Ollama uses "reasoning". Fall back to it so a complete JSON
        # object is still found when thinking was not disabled.
        if not text:
            for field in ("reasoning_content", "reasoning"):
                if message.get(field):
                    text = message[field]
                    break
    usage = data.get("usage") or {}
    estimated = "prompt_tokens" not in usage
    return {
        "text": text,
        "prompt_tokens": int(usage.get("prompt_tokens", len(prompt_blob) // 4)),
        "completion_tokens": int(usage.get("completion_tokens", len(text) // 4)),
        "tokens_estimated": estimated,
        "ms": ms,
        "model": payload["model"],
        "prompt_sha": sha256(prompt_blob),
        "response_sha": sha256(text),
    }


# --------------------------------------------------------------------------
# chart index and search
# --------------------------------------------------------------------------

STOP_WORDS = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "was", "is",
              "are", "be", "by", "at", "as", "from", "that", "this", "it", "patient", "she", "he",
              "her", "his"}

SYNONYMS = {
    "pt": ["physical therapy"],
    "nsaid": ["naproxen", "ibuprofen", "meloxicam"],
    "x-ray": ["xray", "radiograph"],
    "xray": ["xray", "radiograph"],
    "numbness": ["tingling", "sensation"],
    "weakness": ["strength"],
    "cancer": ["malignancy", "carcinoma"],
}


def normalize(text):
    return re.sub(r"x[\s-]?ray", "xray", (text or "").lower())


def sentence_index(case):
    """Ordered mapping of sentence ID -> sentence record."""
    index = {}
    for doc in case["docs"]:
        for number, text in enumerate(doc["sentences"], start=1):
            index["%s-%d" % (doc["doc_id"], number)] = {
                "doc_id": doc["doc_id"],
                "n": number,
                "text": text,
                "title": doc["title"],
                "type": doc["type"],
                "date": doc["date"],
            }
    return index


def submitted_ids(case):
    submitted = case.get("submitted_doc_id", "VN")
    return [sid for sid, rec in sentence_index(case).items() if rec["doc_id"] == submitted]


_TERM_RE = {}


def term_regex(term):
    if term not in _TERM_RE:
        _TERM_RE[term] = re.compile(r"\b%s\b" % re.escape(normalize(term)))
    return _TERM_RE[term]


def build_terms(criterion, query):
    """Query words (stop words removed, synonyms applied) plus the criterion keywords.

    Multi-word keywords such as "physical therapy" stay one phrase term.
    """
    terms = []

    def add(term):
        term = normalize(term).strip()
        if term and term not in terms:
            terms.append(term)

    for word in re.findall(r"[a-z0-9/+-]+", normalize(query)):
        if word in STOP_WORDS:
            continue
        add(word)
        for synonym in SYNONYMS.get(word, []):
            add(synonym)
    for keyword in (criterion or {}).get("keywords", []):
        add(keyword)
    return terms


def search_chart(case, criterion, query, shown_ids):
    """Score every unseen sentence in the chart and return the best five.

    Score is the sum over matched terms of idf * tf / (tf + 1), with
    idf = log(1 + N / df) computed over every sentence in the case.
    """
    index = sentence_index(case)
    terms = build_terms(criterion, query)
    if not terms:
        return []

    normalized = {sid: normalize(rec["text"]) for sid, rec in index.items()}
    total = len(index) or 1

    document_frequency = {}
    for term in terms:
        pattern = term_regex(term)
        document_frequency[term] = sum(1 for text in normalized.values() if pattern.search(text))

    seen = set(shown_ids or [])
    scored = []
    for sid, rec in index.items():
        if sid in seen:
            continue
        text = normalized[sid]
        score = 0.0
        for term in terms:
            df = document_frequency.get(term, 0)
            if not df:
                continue
            tf = len(term_regex(term).findall(text))
            if not tf:
                continue
            idf = math.log(1 + total / float(df))
            score += idf * (tf / float(tf + 1))
        if score > 0:
            scored.append((score, sid, rec))

    scored.sort(key=lambda row: (-row[0], row[1]))
    results = []
    for score, sid, rec in scored[:5]:
        results.append({"id": sid, "title": rec["title"], "date": rec["date"], "text": rec["text"]})
        if shown_ids is not None and sid not in shown_ids:
            shown_ids.append(sid)
    return results


# --------------------------------------------------------------------------
# evidence agent loop
# --------------------------------------------------------------------------

MAX_SEARCHES = 4
MAX_TURNS = 8
# Deviation from the spec's loop rules, recorded in README.md: when the model's final
# reply cites sentence IDs it was never shown and searches remain, the loop tells it
# which citations were dropped and asks it to search instead of accepting the answer.
# The verifier is unchanged; this only gives the model the chance rule 4 asks it to take.
MAX_CORRECTIONS = 3
ALL_DOCS = os.getenv("CG_ALL_DOCS", "") not in ("", "0", "false", "False")

BAD_JSON_MESSAGE = ("Your reply was not valid JSON. Reply with one JSON object only, in the "
                    "format given.")
NO_SEARCHES_MESSAGE = "No searches left. Reply with the final JSON now."

SEARCH_FORMAT = ('To search: {"action":"search","criterion":"CONSERVATIVE_6W","query":"physical '
                 'therapy sessions weeks"}')
FINAL_FORMAT = ('To finish: {"action":"final","injection_suspected":false,"criteria":[{"id":'
                '"CONSERVATIVE_6W","status":"MET","evidence":["PT2-1","PT2-2"],"reason":"Fourteen '
                'therapy sessions over eight weeks with persistent leg pain."}]}')


def evidence_system_prompt():
    return (PROMPTS / "evidence_system.txt").read_text().strip()


def letter_system_prompt():
    return (PROMPTS / "letter_system.txt").read_text().strip()


def _doc_lines(doc):
    lines = ["%s: %s (%s), %s" % (doc["doc_id"], doc["title"], doc["type"], doc["date"])]
    for number, text in enumerate(doc["sentences"], start=1):
        lines.append("[%s-%d] %s" % (doc["doc_id"], number, text))
    return lines


def build_first_message(case, criteria, policy):
    """The first user message, in the order the specification sets out."""
    submitted = case.get("submitted_doc_id", "VN")
    docs = {doc["doc_id"]: doc for doc in case["docs"]}
    out = []

    out.append("Case")
    out.append("Payer: %s" % case["payer"])
    out.append("Policy: %s, %s" % (policy["policy_id"], policy["title"]))
    out.append("Procedure: %s (CPT %s)" % (case["procedure"], case["cpt"]))
    out.append("Date of service: %s" % case["date_of_service"])
    out.append("MRI order date: %s" % case["order_date"])
    out.append("")

    out.append("Denial reason: %s" % case["denial_reason"])
    out.append("")
    out.append("BEGIN UNTRUSTED PAYER LETTER")
    out.append(case["payer_letter"])
    out.append("END UNTRUSTED PAYER LETTER")
    out.append("")

    out.append("Criteria to judge:")
    for crit in criteria:
        out.append("- %s (%s): %s" % (crit["id"], crit["label"], crit["definition"]))
    out.append("")
    # Rule 2 of the system prompt already says NOT_MET needs a sentence that directly
    # contradicts the criterion. Small models read an explicit denial as silence, so
    # code spells out what a contradiction looks like. Noted in README.md.
    out.append("A sentence that explicitly rules a criterion out is a direct contradiction, so "
               "that criterion is NOT_MET, not NOT_FOUND. For example: denies bowel or bladder "
               "changes, fever, weight loss or any history of cancer rules out a red flag; "
               "strength is 5/5 in both legs and reflexes are symmetric rules out a "
               "neurological deficit; pain does not spread into the legs rules out radicular "
               "symptoms; no imaging of the spine has been done rules out a prior X-ray; and a "
               "stated course of conservative care shorter than six weeks, such as three weeks "
               "of therapy or symptoms for ten days, rules out six weeks of conservative care.")
    out.append("")

    if ALL_DOCS:
        out.append("Medical record, every document, one numbered sentence per line:")
        for doc_id in [submitted] + [d for d in docs if d != submitted]:
            out.extend(_doc_lines(docs[doc_id]))
            out.append("")
        out.append("Searches left: 0")
    else:
        out.append("Submitted document, one numbered sentence per line:")
        out.extend(_doc_lines(docs[submitted]))
        out.append("")
        others = [doc for doc in case["docs"] if doc["doc_id"] != submitted]
        if others:
            out.append("Other documents in the chart (content not shown, search to read them):")
            for doc in others:
                out.append("- %s, %s, %s, %s" % (doc["doc_id"], doc["type"], doc["title"], doc["date"]))
        else:
            out.append("There are no other documents in the chart.")
        out.append("")
        out.append("Searches left: %d" % MAX_SEARCHES)
        out.append("")
        out.append("You have read only the submitted document above. You may cite only sentence "
                   "IDs that appear above or in search results. Any other ID is dropped by the "
                   "system and its criterion loses its evidence. You have not seen a single "
                   "sentence of the other documents, so you cannot know their sentence IDs; "
                   "guessing one loses the evidence for that criterion.")
        out.append("Choose this reply now: if every criterion can be decided from the numbered "
                   "sentences above, send the final JSON. Otherwise send one search for a "
                   "criterion you cannot decide yet.")

    out.append("")
    out.append(SEARCH_FORMAT)
    out.append(FINAL_FORMAT)
    out.append("Include every listed criterion exactly once in the final reply.")
    return "\n".join(out)


def _format_search_results(criterion_id, query, results):
    header = "Search results for %s, query %s" % (criterion_id, query)
    if not results:
        return header + "\nNo matching sentences."
    lines = [header]
    for item in results:
        lines.append("[%s] (%s, %s) %s" % (item["id"], item["title"], item["date"], item["text"]))
    return "\n".join(lines)


def _unshown_citations(payload, shown_ids):
    """Criterion ids and sentence ids the model cited without ever being shown them."""
    seen = set(shown_ids or [])
    offenders = {}
    for item in (payload.get("criteria") or []):
        if not isinstance(item, dict):
            continue
        evidence = item.get("evidence")
        if isinstance(evidence, str):
            evidence = [evidence]
        if not isinstance(evidence, list):
            continue
        bad = [str(sid).strip() for sid in evidence if str(sid).strip() not in seen]
        if bad:
            offenders[str(item.get("id", "") or "?")] = bad
    return offenders


def _unsearched_not_found(payload, searched):
    """Criteria marked NOT_FOUND that the model never searched for.

    The system prompt's rule 4 tells the model to search the rest of the chart before
    marking a criterion NOT_FOUND. Small models skip that step, so the loop asks.
    """
    out = []
    for item in (payload.get("criteria") or []):
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id", "") or "").strip().upper()
        status = str(item.get("status", "") or "").strip().upper()
        if status == NOT_FOUND and cid and cid not in searched:
            out.append(cid)
    return out


def _correction_message(offenders, unsearched, searches_left):
    parts = []
    if offenders:
        dropped = sorted(set(sid for ids in offenders.values() for sid in ids))
        parts.append("These sentence IDs were never shown to you and have been dropped: %s. "
                     "That leaves %s without evidence."
                     % (", ".join(dropped), ", ".join(sorted(offenders))))
    if unsearched:
        parts.append("You marked %s as NOT_FOUND without searching the rest of the chart. "
                     "Rule 4 says to search before marking a criterion NOT_FOUND, and the "
                     "other documents may well contain it."
                     % ", ".join(unsearched))
    parts.append("You have %d searches left. Reply with one search JSON object now for a "
                 "criterion that still needs evidence." % searches_left)
    return " ".join(parts)


def _valid_final(payload):
    if not isinstance(payload, dict):
        return False
    criteria = payload.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        return False
    for item in criteria:
        if not isinstance(item, dict):
            return False
        # evidence may be left out for a NOT_FOUND criterion, where there is none;
        # the verifier treats a missing list as empty.
        if "id" not in item or "status" not in item:
            return False
    return True


def assess(case, criteria, policy):
    """Hold the short conversation with the local model and return its final JSON.

    The model may search the chart up to four times, inside at most eight turns.
    """
    messages = [
        {"role": "system", "content": evidence_system_prompt()},
        {"role": "user", "content": build_first_message(case, criteria, policy)},
    ]
    index = sentence_index(case)
    criteria_map = {crit["id"]: crit for crit in criteria}

    if ALL_DOCS:
        shown_ids = list(index)
    else:
        shown_ids = submitted_ids(case)
    shown_source = {sid: "submitted" for sid in shown_ids}

    searches_used = 0
    bad_json = 0
    corrections = 0
    calls = []
    search_trail = []
    result = {
        "final": None, "shown_ids": shown_ids, "shown_source": shown_source,
        "search_trail": search_trail, "calls": calls, "raw_final": "", "error": None,
    }

    for _turn in range(MAX_TURNS):
        try:
            call = chat(messages)
        except Exception as exc:                      # model unreachable or timed out
            result["error"] = "model call failed: %r" % (exc,)
            return result

        calls.append(call)
        audit("llm_call", case["claim_id"], model=call["model"], prompt_sha256=call["prompt_sha"],
              response_sha256=call["response_sha"], prompt_tokens=call["prompt_tokens"],
              completion_tokens=call["completion_tokens"], ms=call["ms"],
              tokens_estimated=call["tokens_estimated"])

        text = clean(call["text"])
        payload = first_json(text)
        messages.append({"role": "assistant", "content": call["text"]})

        if payload is None or not isinstance(payload, dict):
            bad_json += 1
            if bad_json > 2:
                result["error"] = "invalid JSON"
                return result
            messages.append({"role": "user", "content": BAD_JSON_MESSAGE})
            continue

        action = str(payload.get("action", "")).lower()
        if action == "search" and not ALL_DOCS:
            if searches_used >= MAX_SEARCHES:
                messages.append({"role": "user", "content": NO_SEARCHES_MESSAGE})
                continue
            criterion_id = str(payload.get("criterion", "") or "")
            query = str(payload.get("query", "") or "")
            criterion = criteria_map.get(criterion_id)
            results = search_chart(case, criterion, query, shown_ids)
            for item in results:
                shown_source.setdefault(item["id"], "search")
            searches_used += 1
            search_trail.append({"criterion": criterion_id, "query": query,
                                 "results": [item["id"] for item in results]})
            audit("search", case["claim_id"], criterion=criterion_id, query=query,
                  results=[item["id"] for item in results])
            messages.append({"role": "user",
                             "content": _format_search_results(criterion_id, query, results)})
            continue

        if action == "search" and ALL_DOCS:
            messages.append({"role": "user", "content": NO_SEARCHES_MESSAGE})
            continue

        if _valid_final(payload):
            offenders = _unshown_citations(payload, shown_ids)
            searched = set(step["criterion"] for step in search_trail)
            unsearched = _unsearched_not_found(payload, searched)
            if ((offenders or unsearched) and searches_used < MAX_SEARCHES
                    and corrections < MAX_CORRECTIONS):
                corrections += 1
                audit("citation_correction", case["claim_id"], offenders=offenders,
                      unsearched_not_found=unsearched, attempt=corrections)
                messages.append({"role": "user", "content": _correction_message(
                    offenders, unsearched, MAX_SEARCHES - searches_used)})
                continue
            result["final"] = payload
            result["raw_final"] = call["text"]
            return result

        bad_json += 1
        if bad_json > 2:
            result["error"] = "invalid JSON"
            return result
        messages.append({"role": "user", "content": BAD_JSON_MESSAGE})

    if result["final"] is None and result["error"] is None:
        result["error"] = "turn limit reached without a final reply"
    return result


# --------------------------------------------------------------------------
# verifier
# --------------------------------------------------------------------------

INJECTION_PATTERNS = ["ignore previous", "ignore your", "disregard", "system prompt", "upload",
                      "send the", "http://", "https://", "mark every"]


def scan_injection(text):
    """Return the instruction patterns found in untrusted text."""
    lowered = (text or "").lower()
    return [pattern for pattern in INJECTION_PATTERNS if pattern in lowered]


def verify(case, policy, final, shown_ids):
    """Check every citation the model made. Returns (criteria, stats, flags).

    Follows the specification's five steps: drop criteria outside the policy, keep
    only evidence that exists and was shown, downgrade unsupported statuses, scan
    the payer letter for instructions, and record the source of the evidence.
    """
    index = sentence_index(case)
    seen = set(shown_ids or [])
    submitted = case.get("submitted_doc_id", "VN")
    policy_ids = policy_criteria_ids(policy)

    reported = {}
    if final:
        for item in final.get("criteria") or []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id", "") or "").strip().upper()
            if cid and cid not in reported:
                reported[cid] = item

    invalid_citations = []
    downgrades = 0
    criteria = []

    for cid in policy_ids:
        item = reported.get(cid)
        if item is None:
            criteria.append({"id": cid, "status": NOT_FOUND, "evidence": [],
                             "reason": "not addressed by the reviewer", "source": None})
            continue

        status = str(item.get("status", "") or "").strip().upper()
        if status not in STATUSES:
            status = NOT_FOUND
        reason = str(item.get("reason", "") or "").strip()

        raw_evidence = item.get("evidence")
        if isinstance(raw_evidence, str):
            raw_evidence = [raw_evidence]
        if not isinstance(raw_evidence, list):
            raw_evidence = []

        kept = []
        for sid in raw_evidence:
            sid = str(sid).strip()
            if not sid:
                continue
            if sid not in index:
                invalid_citations.append({"id": sid, "reason": "does not exist"})
                audit("citation_dropped", case["claim_id"], id=sid, reason="does not exist")
            elif sid not in seen:
                invalid_citations.append({"id": sid, "reason": "never shown"})
                audit("citation_dropped", case["claim_id"], id=sid, reason="never shown")
            elif sid not in kept:
                kept.append(sid)

        if status in (MET, NOT_MET) and not kept:
            status = NOT_FOUND
            downgrades += 1
            reason = (reason + " " if reason else "") + "downgraded: no valid evidence"
            audit("downgraded", case["claim_id"], criterion=cid)

        source = None
        if kept:
            source = "submitted" if any(sid.startswith(submitted + "-") for sid in kept) else "search"

        criteria.append({"id": cid, "status": status, "evidence": kept,
                         "reason": reason.strip(), "source": source})

    flags = []
    letter_hits = scan_injection(case.get("payer_letter", ""))
    model_suspected = bool(final and final.get("injection_suspected"))
    if letter_hits or model_suspected:
        flags.append("injection")
        audit("injection_flagged", case["claim_id"], patterns=letter_hits,
              model_suspected=model_suspected)

    stats = {"invalid_citations": invalid_citations, "downgrades": downgrades}
    return criteria, stats, flags


# --------------------------------------------------------------------------
# routing, scoring and ranking
# --------------------------------------------------------------------------

ROUTE_ORDER = {"APPEAL": 0, "FETCH": 1, "DROP": 2, "MANUAL": 3}


def decide(case, policy, criteria):
    """Route the case from verified evidence and score it for the queue."""
    statuses = {item["id"]: item["status"] for item in criteria}
    counts = {item["id"]: len(item["evidence"]) for item in criteria}
    decision = route(policy["paths"], statuses, counts)

    deadline = date.fromisoformat(case["denial_date"]) + _days(policy["appeal_window_days"])
    days_left = (deadline - TODAY).days
    win_score = WIN_SCORE.get(decision["route"], 0.0)
    expected_payment = case["expected_payment"]

    decision.update({
        "win_score": win_score,
        "expected_payment": expected_payment,
        "expected_value": round(expected_payment * win_score, 2),
        "deadline": deadline.isoformat(),
        "days_left": days_left,
        "urgent": 0 <= days_left <= 14,
        "expired": days_left < 0,
    })
    return decision


def _days(count):
    from datetime import timedelta
    return timedelta(days=count)


def manual_decision(case, policy, error):
    deadline = date.fromisoformat(case["denial_date"]) + _days(policy["appeal_window_days"])
    days_left = (deadline - TODAY).days
    return {
        "route": "MANUAL", "argument_path": [], "missing": [], "drop_reasons": [],
        "win_score": 0.0, "expected_payment": case["expected_payment"], "expected_value": 0.0,
        "deadline": deadline.isoformat(), "days_left": days_left,
        "urgent": 0 <= days_left <= 14, "expired": days_left < 0, "error": error,
    }


def queue_sort_key(result):
    """APPEAL first, not-expired before expired, urgent first, then value, then id."""
    return (
        ROUTE_ORDER.get(result.get("route"), 9),
        1 if result.get("expired") else 0,
        0 if result.get("urgent") else 1,
        -float(result.get("expected_value") or 0.0),
        result.get("claim_id", ""),
    )


# --------------------------------------------------------------------------
# per-case pipeline
# --------------------------------------------------------------------------

def update_case_status(claim_id, status, **extra):
    """Case status lives in data/cases/{id}.json and is written only from here."""
    case = load_case(claim_id)
    case["status"] = status
    case.update(extra)
    write_json_atomic(case_path(claim_id), case)
    return case


def process_case(claim_id, run_id=None):
    """Run one denial end to end. Never raises: a failure becomes a MANUAL route."""
    if run_id:
        set_run_id(run_id)
    started = time.time()
    case = load_case(claim_id)
    policy = policy_by_id(case["policy_id"])
    audit("case_start", claim_id, payer=case["payer"], policy_id=case["policy_id"])

    criteria_all = criteria_by_id()
    wanted = [criteria_all[cid] for cid in policy_criteria_ids(policy) if cid in criteria_all]

    result = {
        "claim_id": claim_id,
        "payer": case["payer"],
        "policy_id": case["policy_id"],
        "model": None,
        "error": None,
        "status": "draft",
        "approved_by": None,
        "approved_at": None,
    }

    try:
        assessment = assess(case, wanted, policy)
        calls = assessment["calls"]
        result["model"] = calls[0]["model"] if calls else (os.getenv("CG_LLM_MODEL") or None)

        if assessment["error"] or not assessment["final"]:
            error = assessment["error"] or "no final reply"
            criteria, stats, flags = verify(case, policy, None, assessment["shown_ids"])
            decision = manual_decision(case, policy, error)
            result["error"] = error
        else:
            criteria, stats, flags = verify(case, policy, assessment["final"], assessment["shown_ids"])
            decision = decide(case, policy, criteria)

        if decision.get("expired") and "expired" not in flags:
            flags.append("expired")

        outputs, quote_violations, output_meta = render_outputs(case, policy, decision,
                                                                 criteria, flags)

        prompt_tokens = sum(call["prompt_tokens"] for call in calls)
        completion_tokens = sum(call["completion_tokens"] for call in calls)
        llm_ms = sum(call["ms"] for call in calls)

        result.update({
            "route": decision["route"],
            "criteria": criteria,
            "argument_path": decision.get("argument_path") or [],
            "missing": decision.get("missing") or [],
            "drop_reasons": decision.get("drop_reasons") or [],
            "win_score": decision["win_score"],
            "expected_payment": decision["expected_payment"],
            "expected_value": decision["expected_value"],
            "deadline": decision["deadline"],
            "days_left": decision["days_left"],
            "urgent": decision["urgent"],
            "expired": decision["expired"],
            "flags": flags,
            "invalid_citations": stats["invalid_citations"],
            "downgrades": stats["downgrades"],
            "search_trail": assessment["search_trail"],
            "shown_ids": assessment["shown_ids"],
            "timings": {"seconds": round(time.time() - started, 2), "llm_ms": llm_ms},
            "tokens": {"prompt": prompt_tokens, "completion": completion_tokens},
            "llm_calls": len(calls),
            "outputs": outputs,
            "quote_violations": quote_violations,
            "argument_texts": output_meta.get("argument_texts"),
        })

        audit("route", claim_id, route=decision["route"],
              argument_path=decision.get("argument_path") or [],
              missing=decision.get("missing") or [],
              drop_reasons=decision.get("drop_reasons") or [],
              raw_final=assessment.get("raw_final", ""))

    except Exception as exc:                           # one bad case must not stop the batch
        decision = manual_decision(case, policy, repr(exc))
        result.update({
            "route": "MANUAL", "criteria": [], "argument_path": [], "missing": [],
            "drop_reasons": [], "win_score": 0.0,
            "expected_payment": case["expected_payment"], "expected_value": 0.0,
            "deadline": decision["deadline"], "days_left": decision["days_left"],
            "urgent": decision["urgent"], "expired": decision["expired"],
            "flags": ["error"], "invalid_citations": [], "downgrades": 0,
            "search_trail": [], "shown_ids": [],
            "timings": {"seconds": round(time.time() - started, 2), "llm_ms": 0},
            "tokens": {"prompt": 0, "completion": 0}, "llm_calls": 0,
            "outputs": {}, "quote_violations": 0, "error": repr(exc),
        })
        audit("route", claim_id, route="MANUAL", error=repr(exc))

    write_json_atomic(OUT / "results" / ("%s.json" % claim_id), result)
    update_case_status(claim_id, "draft")
    audit("case_end", claim_id, route=result["route"], seconds=result["timings"]["seconds"])
    return result


def load_result(claim_id):
    path = OUT / "results" / ("%s.json" % claim_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def all_results():
    folder = OUT / "results"
    if not folder.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]


# --------------------------------------------------------------------------
# letter writer (APPEAL only). The model writes the argument; code inserts quotes.
# --------------------------------------------------------------------------

NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20",
}
TEMPLATE_ARGUMENT = "The medical record documents this criterion, as shown below."
PLACEHOLDER_RE = re.compile(r"\[\[([A-Za-z0-9_-]+)\]\]")


def evidence_for_path(case, criteria, argument_path):
    """Verified evidence for each criterion in the argument path: ID and exact text."""
    index = sentence_index(case)
    by_criterion = {}
    for item in criteria:
        if item["id"] not in argument_path:
            continue
        rows = []
        for sid in item["evidence"]:
            record = index.get(sid)
            if record:
                rows.append({"id": sid, "text": record["text"], "title": record["title"],
                             "date": record["date"]})
        by_criterion[item["id"]] = rows
    return by_criterion


def check_arguments(args, evidence_by_criterion):
    """Code checks on the letter writer's reply. Returns a list of problems."""
    problems = []
    labels = criteria_by_id()
    by_id = {}
    for item in args or []:
        if isinstance(item, dict) and item.get("id"):
            by_id[str(item["id"]).strip().upper()] = str(item.get("text", "") or "")

    for cid, rows in evidence_by_criterion.items():
        text = by_id.get(cid)
        if text is None:
            problems.append("%s: no argument was written for this criterion" % cid)
            continue

        valid_ids = set(row["id"] for row in rows)
        for sid in PLACEHOLDER_RE.findall(text):
            if sid not in valid_ids:
                problems.append("%s: placeholder [[%s]] is not verified evidence for this "
                                "criterion; use one of %s"
                                % (cid, sid, ", ".join(sorted(valid_ids)) or "none"))

        bare = PLACEHOLDER_RE.sub(" ", text)
        if any(mark in bare for mark in ('"', "“", "”")):
            problems.append("%s: remove the quotation marks; the system inserts the quotes" % cid)
        for mark in ("'", "‘", "’"):
            for match in re.finditer(re.escape(mark), bare):
                start = match.start()
                before = bare[start - 1] if start > 0 else ""
                after = bare[start + 1] if start + 1 < len(bare) else ""
                if not (before.isalpha() and after.isalpha()):
                    problems.append("%s: remove the quotation marks; the system inserts the quotes"
                                    % cid)
                    break
            else:
                continue
            break

        evidence_text = " ".join(row["text"] for row in rows)
        evidence_lower = evidence_text.lower()

        def in_evidence(number):
            # whole-token match, so 20 does not count as found inside a date like 2026
            return re.search(r"(?<!\d)%s(?!\d)" % re.escape(number), evidence_text) is not None

        for digits in re.findall(r"\d+", bare):
            if not in_evidence(digits):
                problems.append("%s: the number %s does not appear in the cited evidence"
                                % (cid, digits))
        for word, digits in NUMBER_WORDS.items():
            if re.search(r"\b%s\b" % word, bare.lower()):
                if not re.search(r"\b%s\b" % word, evidence_lower) and not in_evidence(digits):
                    problems.append("%s: the number %s does not appear in the cited evidence"
                                    % (cid, word))

        if len(text) > 400:
            problems.append("%s: the text is %d characters; keep it under 400" % (cid, len(text)))

    extra = set(by_id) - set(evidence_by_criterion)
    for cid in sorted(extra):
        problems.append("%s: this criterion is not in the argument path" % cid)
    return problems


def write_arguments(case, policy, criteria, argument_path):
    """Ask the model for the argument text, check it, retry once, fall back to a template."""
    evidence_by_criterion = {cid: rows for cid, rows in
                             evidence_for_path(case, criteria, argument_path).items()}
    labels = criteria_by_id()

    lines = ["Payer: %s" % case["payer"],
             "Policy: %s, %s" % (policy["policy_id"], policy["title"]), ""]
    for cid in argument_path:
        crit = labels.get(cid, {})
        lines.append("Criterion %s (%s): %s" % (cid, crit.get("label", cid), crit.get("definition", "")))
        lines.append("Verified evidence:")
        for row in evidence_by_criterion.get(cid, []):
            lines.append("  [[%s]] %s" % (row["id"], row["text"]))
        lines.append("")
    lines.append("Write one argument object per criterion listed above.")
    user_message = "\n".join(lines)

    messages = [{"role": "system", "content": letter_system_prompt()},
                {"role": "user", "content": user_message}]

    args, calls = None, []
    for attempt in (0, 1):
        try:
            call = chat(messages)
        except Exception as exc:
            audit("letter_model_unavailable", case["claim_id"], error=repr(exc))
            break
        calls.append(call)
        audit("llm_call", case["claim_id"], model=call["model"], prompt_sha256=call["prompt_sha"],
              response_sha256=call["response_sha"], prompt_tokens=call["prompt_tokens"],
              completion_tokens=call["completion_tokens"], ms=call["ms"], purpose="letter")
        payload = first_json(clean(call["text"]))
        candidate = (payload or {}).get("arguments") if isinstance(payload, dict) else None
        problems = check_arguments(candidate, evidence_by_criterion)
        if not problems:
            args = candidate
            break
        audit("letter_checks_failed", case["claim_id"], problems=problems, attempt=attempt + 1)
        if attempt == 0:
            messages.append({"role": "assistant", "content": call["text"]})
            messages.append({"role": "user", "content":
                             "Your reply had these problems:\n- " + "\n- ".join(problems)
                             + "\nReply with one corrected JSON object only."})

    texts = {}
    for cid in argument_path:
        texts[cid] = TEMPLATE_ARGUMENT
    if args:
        for item in args:
            cid = str(item.get("id", "") or "").strip().upper()
            if cid in texts:
                texts[cid] = str(item.get("text", "") or "")
    return texts, evidence_by_criterion, calls


# --------------------------------------------------------------------------
# letter layout
# --------------------------------------------------------------------------

def _quote(row):
    return '"%s" (%s, %s, %s)' % (row["text"], row["title"], row["date"], row["id"])


def _fill_placeholders(text, rows, renderer):
    by_id = {row["id"]: row for row in rows}
    used = []

    def replace(match):
        sid = match.group(1)
        row = by_id.get(sid)
        if not row:
            return ""
        used.append(sid)
        return renderer(row)

    filled = PLACEHOLDER_RE.sub(replace, text)
    if not used and rows:
        # the model wrote no placeholder (or the template was used): append the evidence
        filled = filled.rstrip()
        for row in rows:
            filled += " " + renderer(row)
            used.append(row["id"])
    return filled.strip(), used


def letter_header_lines(case, policy):
    return [
        "Demo General Hospital",
        "Denial Management Office",
        "",
        TODAY.isoformat(),
        "",
        policy["appeals_address"],
        "",
        "RE: request for reconsideration of claim %s" % case["claim_id"],
        "",
        "Patient: %s" % case["patient"]["name"],
        "Member ID: %s" % case["patient"]["member_id"],
        "Date of service: %s" % case["date_of_service"],
        "Procedure: CPT %s, %s" % (case["cpt"], case["procedure"]),
        "ICD-10: %s" % ", ".join(case["icd10"]),
        "",
    ]


def letter_opening(case, policy):
    reason = (case["denial_reason"] or "").strip().rstrip(".")
    return ("We request reconsideration of the denial of this claim. The denial states: %s. "
            "The medical record shows that the MRI met %s policy %s, %s, as set out below."
            % (reason, case["payer"], policy["policy_id"], policy["title"]))


LETTER_CLOSING = ("Because the documented findings meet the policy, we ask that the denial be "
                  "reversed and the claim paid. The referenced records are enclosed.")


def signature_line(status, approved_by=None, approved_at=None):
    if status == "approved" and approved_by:
        return "Approved by %s on %s." % (approved_by, approved_at)
    return "Prepared by ClaimGuard for specialist review. Status: %s." % status


def render_letter_txt(case, policy, criteria, argument_path, texts, evidence_by_criterion,
                      status="draft", approved_by=None, approved_at=None):
    labels = criteria_by_id()
    lines = letter_header_lines(case, policy)
    lines.append(letter_opening(case, policy))
    lines.append("")
    cited = []
    for cid in argument_path:
        rows = evidence_by_criterion.get(cid, [])
        lines.append(labels.get(cid, {}).get("label", cid))
        body, used = _fill_placeholders(texts.get(cid, TEMPLATE_ARGUMENT), rows, _quote)
        cited.extend(used)
        lines.append(body)
        lines.append("")
    lines.append(LETTER_CLOSING)
    lines.append("")
    lines.append(signature_line(status, approved_by, approved_at))
    return "\n".join(lines) + "\n", cited


def render_letter_html(case, policy, criteria, argument_path, texts, evidence_by_criterion,
                       flags, status="draft", approved_by=None, approved_at=None):
    import html as html_mod

    def esc(value):
        return html_mod.escape(str(value), quote=True)

    labels = criteria_by_id()
    cited_ids = set()
    parts = []
    for cid in argument_path:
        rows = evidence_by_criterion.get(cid, [])

        def renderer(row):
            cited_ids.add(row["id"])
            return ('<mark><a href="#s-%s">&quot;%s&quot;</a></mark> '
                    '<span class="src">(%s, %s, %s)</span>'
                    % (esc(row["id"]), esc(row["text"]), esc(row["title"]), esc(row["date"]),
                       esc(row["id"])))

        raw = texts.get(cid, TEMPLATE_ARGUMENT)
        escaped = esc(raw)
        # the placeholders survive escaping, so fill them after escaping the model text
        body, _ = _fill_placeholders(escaped, rows, renderer)
        parts.append('<section><h3>%s</h3><p>%s</p></section>'
                     % (esc(labels.get(cid, {}).get("label", cid)), body))

    appendix = []
    for doc in case["docs"]:
        rows = []
        for number, text in enumerate(doc["sentences"], start=1):
            sid = "%s-%d" % (doc["doc_id"], number)
            mark = ' class="hit"' if sid in cited_ids else ""
            rows.append('<li id="s-%s"%s><span class="sid">%s</span> %s</li>'
                        % (esc(sid), mark, esc(sid), esc(text)))
        appendix.append('<article><h4>%s &mdash; %s, %s</h4><ol>%s</ol></article>'
                        % (esc(doc["doc_id"]), esc(doc["title"]), esc(doc["date"]), "".join(rows)))

    banner = ""
    if "injection" in (flags or []):
        banner = ('<div class="warn">Payer letter contained instructions; they were ignored.</div>')

    header = "<br>".join(esc(line) for line in letter_header_lines(case, policy) if line)

    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>ClaimGuard appeal %(claim)s</title>
<style>
 body{font-family:Georgia,serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#1a1a1a;line-height:1.5}
 .warn{background:#fff6d6;border:1px solid #d9b300;padding:.6rem .8rem;margin-bottom:1rem;font-family:system-ui,sans-serif;font-size:.9rem}
 .hdr{font-size:.9rem;margin-bottom:1.5rem}
 mark{background:#fdf2a8;padding:0 .1rem}
 mark a{color:inherit;text-decoration:none;border-bottom:1px dotted #8a7400}
 .src{font-size:.8rem;color:#555}
 h3{font-size:1rem;margin-bottom:.2rem}
 h4{font-size:.9rem;margin:.9rem 0 .2rem}
 .appendix{margin-top:2.5rem;border-top:1px solid #ccc;padding-top:1rem;font-size:.9rem}
 .appendix ol{padding-left:1.4rem}
 .sid{color:#777;font-size:.75rem;font-family:ui-monospace,monospace;margin-right:.3rem}
 li.hit{background:#fdf2a8}
 .sig{margin-top:2rem;font-style:italic}
</style></head><body>
%(banner)s
<div class="hdr">%(header)s</div>
<p>%(opening)s</p>
%(parts)s
<p>%(closing)s</p>
<p class="sig">%(signature)s</p>
<div class="appendix"><h2>Evidence appendix</h2>%(appendix)s</div>
</body></html>
""" % {
        "claim": esc(case["claim_id"]),
        "banner": banner,
        "header": header,
        "opening": esc(letter_opening(case, policy)),
        "parts": "\n".join(parts),
        "closing": esc(LETTER_CLOSING),
        "signature": esc(signature_line(status, approved_by, approved_at)),
        "appendix": "".join(appendix),
    }


def quote_check(letter_txt, case):
    """Every string between straight double quotes must be a chart sentence, exactly."""
    sentences = set()
    for doc in case["docs"]:
        for text in doc["sentences"]:
            sentences.add(text)
    violations = []
    for quoted in re.findall(r'"([^"]*)"', letter_txt):
        if quoted not in sentences:
            violations.append(quoted)
    return violations


# --------------------------------------------------------------------------
# FETCH request, DROP memo, MANUAL note (templates, no model call)
# --------------------------------------------------------------------------

def render_request_txt(case, policy, decision):
    labels = criteria_by_id()
    author = ""
    for doc in case["docs"]:
        if doc["doc_id"] == case.get("submitted_doc_id", "VN"):
            author = doc.get("author") or ""
    lines = [
        "Demo General Hospital",
        "Denial Management Office",
        "",
        TODAY.isoformat(),
        "",
        "To: %s, ordering clinician" % (author or "the ordering clinician"),
        "",
        "RE: document request for claim %s" % case["claim_id"],
        "",
        "This claim for MRI lumbar spine without contrast (CPT 72148), performed on %s, was denied "
        "as not medically necessary by %s under policy %s, %s."
        % (case["date_of_service"], case["payer"], policy["policy_id"], policy["title"]),
        "",
        "The chart does not document the following, which the policy requires. Please send it so "
        "the appeal can be filed:",
        "",
    ]
    for cid in decision.get("missing") or []:
        crit = labels.get(cid, {})
        lines.append("- %s: %s" % (crit.get("label", cid), crit.get("definition", "")))
    lines.append("")
    lines.append("Appeal deadline: %s (%d days left)." % (decision["deadline"], decision["days_left"]))
    lines.append("")
    lines.append("Prepared by ClaimGuard for specialist review.")
    return "\n".join(lines) + "\n"


def render_memo_txt(case, policy, decision, criteria):
    labels = criteria_by_id()
    index = sentence_index(case)
    lines = [
        "Demo General Hospital",
        "Denial Management Office",
        "",
        TODAY.isoformat(),
        "",
        "RE: claim %s, %s, policy %s" % (case["claim_id"], case["payer"], policy["policy_id"]),
        "",
        "No appeal is recommended.",
        "",
        "The record contradicts the following policy criteria:",
        "",
    ]
    by_id = {item["id"]: item for item in criteria}
    for cid in decision.get("drop_reasons") or []:
        crit = labels.get(cid, {})
        lines.append("%s" % crit.get("label", cid))
        item = by_id.get(cid, {})
        for sid in item.get("evidence") or []:
            record = index.get(sid)
            if record:
                lines.append('  "%s" (%s, %s, %s)'
                             % (record["text"], record["title"], record["date"], sid))
        if not (item.get("evidence") or []):
            lines.append("  (no sentence recorded)")
        lines.append("")
    lines.append("Appeal deadline: %s (%d days left)." % (decision["deadline"], decision["days_left"]))
    lines.append("")
    lines.append("The specialist may override this recommendation.")
    return "\n".join(lines) + "\n"


def render_manual_txt(case, decision, error):
    lines = [
        "Demo General Hospital",
        "Denial Management Office",
        "",
        TODAY.isoformat(),
        "",
        "RE: claim %s" % case["claim_id"],
        "",
        "ClaimGuard could not complete this case.",
        "Error: %s" % (error or "unknown"),
        "",
        "Handle this case by hand.",
    ]
    return "\n".join(lines) + "\n"


def _rel(path):
    """Path relative to the project root when it sits under it, else the full path."""
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def _write_output(path, text, claim_id):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(str(tmp), str(path))
    audit("output_written", claim_id, path=_rel(path), sha256=sha256(text))
    return _rel(path)


def render_outputs(case, policy, decision, criteria, flags, status="draft",
                   approved_by=None, approved_at=None, argument_texts=None):
    """Render the one output this route calls for.

    Returns (outputs, quote_violations, meta). When argument_texts is given, the
    letter is rebuilt from that stored text and the model is not called again, so an
    approval stamps exactly the letter the specialist read.
    """
    claim_id = case["claim_id"]
    route_name = decision["route"]
    outputs = {}
    meta = {"argument_texts": None}

    if route_name == "APPEAL":
        argument_path = decision["argument_path"]
        if argument_texts:
            texts = dict(argument_texts)
            evidence_by_criterion = evidence_for_path(case, criteria, argument_path)
        else:
            texts, evidence_by_criterion, _ = write_arguments(case, policy, criteria, argument_path)
        meta["argument_texts"] = dict(texts)
        letter_txt, _ = render_letter_txt(case, policy, criteria, argument_path, texts,
                                          evidence_by_criterion, status, approved_by, approved_at)
        violations = quote_check(letter_txt, case)
        if violations:
            audit("quote_violation", claim_id, count=len(violations), samples=violations[:3])
        letter_html = render_letter_html(case, policy, criteria, argument_path, texts,
                                         evidence_by_criterion, flags, status, approved_by,
                                         approved_at)
        outputs["letter_txt"] = _write_output(OUT / "letters" / ("%s.txt" % claim_id),
                                              letter_txt, claim_id)
        outputs["letter_html"] = _write_output(OUT / "letters" / ("%s.html" % claim_id),
                                               letter_html, claim_id)
        return outputs, len(violations), meta

    if route_name == "FETCH":
        text = render_request_txt(case, policy, decision)
        outputs["request_txt"] = _write_output(OUT / "requests" / ("%s.txt" % claim_id),
                                               text, claim_id)
        return outputs, 0, meta

    if route_name == "DROP":
        text = render_memo_txt(case, policy, decision, criteria)
        violations = quote_check(text, case)
        if violations:
            audit("quote_violation", claim_id, count=len(violations), samples=violations[:3])
        outputs["memo_txt"] = _write_output(OUT / "memos" / ("%s.txt" % claim_id), text, claim_id)
        return outputs, len(violations), meta

    text = render_manual_txt(case, decision, decision.get("error"))
    outputs["manual_txt"] = _write_output(OUT / "memos" / ("%s.manual.txt" % claim_id),
                                          text, claim_id)
    return outputs, 0, meta


# --------------------------------------------------------------------------
# approval (one function for the CLI and the review page)
# --------------------------------------------------------------------------

class ApprovalError(Exception):
    """Raised when an approval cannot be recorded."""


def approve(claim_id, by, decision="approved", reason=""):
    """Record a specialist's decision. Approved is only a status: nothing is sent anywhere."""
    if not (by or "").strip():
        raise ApprovalError("a name is required")
    if decision not in ("approved", "rejected"):
        raise ApprovalError("decision must be approved or rejected")

    result = load_result(claim_id)
    if result is None:
        raise ApprovalError("no result for %s; run it first" % claim_id)
    if result.get("status") != "draft":
        raise ApprovalError("%s is already %s" % (claim_id, result.get("status")))

    case = load_case(claim_id)
    policy = policy_by_id(case["policy_id"])
    stamp = time.strftime("%Y-%m-%d %H:%M")
    by = by.strip()

    result["status"] = decision
    if decision == "approved":
        result["approved_by"] = by
        result["approved_at"] = stamp
    else:
        result["rejected_by"] = by
        result["rejected_at"] = stamp
        result["reject_reason"] = reason or ""

    if result.get("route") == "APPEAL":
        # Re-render only to stamp the decision. The argument text comes from the stored
        # draft, so the specialist approves exactly the letter they read.
        outputs, violations, _ = render_outputs(
            case, policy, {"route": "APPEAL", "argument_path": result["argument_path"],
                           "deadline": result["deadline"], "days_left": result["days_left"]},
            result["criteria"], result.get("flags") or [], status=decision,
            approved_by=by if decision == "approved" else None, approved_at=stamp,
            argument_texts=result.get("argument_texts"))
        result["outputs"] = outputs
        result["quote_violations"] = violations

    write_json_atomic(OUT / "results" / ("%s.json" % claim_id), result)
    update_case_status(claim_id, decision)
    audit(decision, claim_id, by=by, **({"reason": reason} if decision == "rejected" else {}))
    return result


# --------------------------------------------------------------------------
# queue and digest
# --------------------------------------------------------------------------

QUEUE_FIELDS = ("rank", "claim_id", "payer", "route", "expected_payment", "win_score",
                "expected_value", "days_left", "urgent", "flags", "status")


def build_queue(results=None):
    """The ranked queue: winnable appeals first, largest expected value first."""
    rows = results if results is not None else all_results()
    ordered = sorted(rows, key=queue_sort_key)
    queue = []
    for rank, result in enumerate(ordered, start=1):
        queue.append({
            "rank": rank,
            "claim_id": result["claim_id"],
            "payer": result["payer"],
            "route": result["route"],
            "expected_payment": result["expected_payment"],
            "win_score": result["win_score"],
            "expected_value": result["expected_value"],
            "days_left": result["days_left"],
            "urgent": result["urgent"],
            "flags": result.get("flags") or [],
            "status": result.get("status"),
        })
    write_json_atomic(OUT / "queue.json", queue)
    return queue


def _human_duration(seconds):
    if seconds < 90:
        return "%d seconds" % int(round(seconds))
    return "%d minutes" % int(round(seconds / 60.0))


def build_digest(results=None, wall_seconds=None):
    """Run summary. digest.txt is the chat message and holds no patient data."""
    rows = results if results is not None else all_results()
    counts = {}
    for result in rows:
        counts[result["route"]] = counts.get(result["route"], 0) + 1

    appeals = [r for r in rows if r["route"] == "APPEAL"]
    appeals_payment = sum(r["expected_payment"] for r in appeals)
    appeals_value = round(sum(r["expected_value"] for r in appeals), 2)
    flagged = [r for r in rows if "injection" in (r.get("flags") or [])]
    urgent = [r for r in rows if r.get("urgent")]
    expired = [r for r in rows if r.get("expired")]

    seconds = [r["timings"]["seconds"] for r in rows if r.get("timings")]
    completion = sum((r.get("tokens") or {}).get("completion", 0) for r in rows)
    wall = wall_seconds if wall_seconds is not None else sum(seconds)
    mean_seconds = round(sum(seconds) / len(seconds), 2) if seconds else 0.0

    digest = {
        "model": next((r.get("model") for r in rows if r.get("model")), None),
        "base_url": BASE_URL,
        "cases_processed": len(rows),
        "counts": counts,
        "appeals_ready": len(appeals),
        "appeals_expected_payment": appeals_payment,
        "appeals_expected_value": appeals_value,
        "document_requests": counts.get("FETCH", 0),
        "no_appeal": counts.get("DROP", 0),
        "manual": counts.get("MANUAL", 0),
        "flagged_letters": len(flagged),
        "urgent_cases": len(urgent),
        "expired": len(expired),
        "mean_seconds_per_case": mean_seconds,
        "batch_wall_seconds": round(wall, 2),
        "output_tokens_per_second": round(completion / wall, 2) if wall else 0.0,
        "quote_violations": sum(r.get("quote_violations", 0) for r in rows),
        "assumptions": dict(ASSUMPTIONS),
    }
    write_json_atomic(OUT / "digest.json", digest)

    text = ("ClaimGuard overnight run: %d denials reviewed in %s on the local GB10. "
            "%d appeals ready (%s USD expected payment), %d need one document, "
            "%d not worth appealing, %d %s flagged, %d urgent. "
            "Open the review page to approve."
            % (digest["cases_processed"], _human_duration(wall), digest["appeals_ready"],
               "{:,}".format(appeals_payment), digest["document_requests"], digest["no_appeal"],
               digest["flagged_letters"],
               "letter" if digest["flagged_letters"] == 1 else "letters",
               digest["urgent_cases"]))
    if digest["manual"]:
        text += " %d need a person." % digest["manual"]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "digest.txt").write_text(text + "\n")
    return digest, text


def reset():
    """Every case back to new; clear out/ except the append-only audit log."""
    for claim_id in all_claim_ids():
        case = load_case(claim_id)
        if case.get("status") != "new":
            case["status"] = "new"
            write_json_atomic(case_path(claim_id), case)
    if OUT.exists():
        for path in sorted(OUT.rglob("*"), key=lambda p: -len(str(p))):
            if path.name in ("audit.jsonl", ".gitkeep"):
                continue
            if path.is_file():
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    for folder in ("results", "letters", "requests", "memos"):
        (OUT / folder).mkdir(parents=True, exist_ok=True)
    audit("reset", cases=len(all_claim_ids()))
