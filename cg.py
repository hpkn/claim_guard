#!/usr/bin/env python3
"""ClaimGuard command line. Standard library only.

Commands are listed in the specification's "Project layout and commands" section.
Exit codes: 0 ok, 2 unknown claim or bad status, 1 anything else.
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import engine


def cmd_selftest(args):
    """Check the local endpoint, then run Maria's case end to end."""
    print("base URL: %s" % engine.BASE_URL)
    try:
        models = engine.list_models()
    except Exception as exc:
        print("cannot reach the local model endpoint at %s: %r" % (engine.BASE_URL, exc))
        print("start the local server (for example: ollama serve) and try again.")
        return 1
    print("models available: %s" % ", ".join(models) if models else "models available: none")

    try:
        name = engine.model_name()
    except Exception as exc:
        print("cannot pick a model: %r" % exc)
        return 1
    print("model: %s" % name)
    print("max tokens: %d, workers: %d, today: %s"
          % (engine.MAX_TOKENS, engine.WORKERS, engine.TODAY.isoformat()))
    if engine.ALL_DOCS:
        print("CG_ALL_DOCS is on: the whole chart goes in the first message, no searches")

    claim_id = args.claim_id
    print("\nrunning %s ..." % claim_id)
    run_id = time.strftime("selftest-%Y%m%dT%H%M%S")
    engine.set_run_id(run_id)
    engine.audit("run_start", mode="selftest", ids=[claim_id], model=name)
    started = time.time()
    result = engine.process_case(claim_id, run_id=run_id)
    engine.audit("run_end", mode="selftest", seconds=round(time.time() - started, 2))

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["route"] != "MANUAL" else 1

    print_case_report(result)

    if result["route"] == "MANUAL":
        print("\nMANUAL: %s" % result.get("error"))
        return 1
    return 0


def print_case_report(result):
    index = engine.sentence_index(engine.load_case(result["claim_id"]))
    print("\n%s  route %s  (%.1fs, %d model calls)"
          % (result["claim_id"], result["route"], result["timings"]["seconds"], result["llm_calls"]))
    if result["route"] == "APPEAL":
        print("argument path: %s" % " + ".join(result["argument_path"]))
    if result["missing"]:
        print("missing: %s" % ", ".join(result["missing"]))
    if result["drop_reasons"]:
        print("contradicted: %s" % ", ".join(result["drop_reasons"]))

    print("\ncriteria")
    for item in result["criteria"]:
        print("  %-16s %-9s %s" % (item["id"], item["status"], ", ".join(item["evidence"]) or "-"))
        if item.get("reason"):
            print("      reason: %s" % item["reason"])
        for sid in item["evidence"]:
            record = index.get(sid)
            if record:
                print("      [%s] (%s, %s) %s" % (sid, record["title"], record["date"], record["text"]))

    if result["search_trail"]:
        print("\nsearch trail")
        for step in result["search_trail"]:
            print("  %s: %r -> %s" % (step["criterion"], step["query"],
                                      ", ".join(step["results"]) or "nothing"))

    print("\nwin score %.2f (assumption), expected payment %d USD, expected value %.2f USD"
          % (result["win_score"], result["expected_payment"], result["expected_value"]))
    print("deadline %s, %d days left%s%s"
          % (result["deadline"], result["days_left"],
             ", urgent" if result["urgent"] else "", ", expired" if result["expired"] else ""))
    if result["flags"]:
        print("flags: %s" % ", ".join(result["flags"]))
    if result["invalid_citations"]:
        print("invalid citations dropped: %s"
              % ", ".join("%s (%s)" % (c["id"], c["reason"]) for c in result["invalid_citations"]))
    if result["downgrades"]:
        print("status downgrades: %d" % result["downgrades"])
    if result.get("outputs"):
        print("outputs: %s" % ", ".join(sorted(result["outputs"].values())))


def parse_ids(spec):
    """A-1001,A-1013 or the range A-1001..A-1020."""
    ids = []
    for chunk in str(spec).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ".." in chunk:
            start, _, end = chunk.partition("..")
            start, end = start.strip(), end.strip()
            prefix = start.split("-")[0]
            first, last = int(start.split("-")[1]), int(end.split("-")[1])
            for number in range(first, last + 1):
                ids.append("%s-%d" % (prefix, number))
        else:
            ids.append(chunk)
    seen, unique = set(), []
    for claim_id in ids:
        if claim_id not in seen:
            seen.add(claim_id)
            unique.append(claim_id)
    return unique


def cmd_run(args):
    known = set(engine.all_claim_ids())
    if args.ids:
        wanted = parse_ids(args.ids)
        unknown = [c for c in wanted if c not in known]
        if unknown:
            print("unknown claim ids: %s" % ", ".join(unknown))
            return 2
    elif args.new:
        wanted = [c for c in sorted(known) if engine.load_case(c).get("status") == "new"]
    else:
        wanted = sorted(known)

    if not wanted:
        print("nothing to do: no cases with status new")
        return 0

    workers = args.workers or engine.WORKERS
    run_id = time.strftime("run-%Y%m%dT%H%M%S")
    engine.set_run_id(run_id)
    try:
        model = engine.model_name()
    except Exception as exc:
        print("cannot reach the local model endpoint at %s: %r" % (engine.BASE_URL, exc))
        return 1

    print("run %s: %d cases, %d workers, model %s, %s"
          % (run_id, len(wanted), workers, model, engine.BASE_URL))
    engine.audit("run_start", mode="run", ids=wanted, workers=workers, model=model)
    started = time.time()

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(engine.process_case, claim_id, run_id): claim_id
                   for claim_id in wanted}
        for future in futures:
            pass
        for future, claim_id in sorted(futures.items(), key=lambda kv: kv[1]):
            try:
                result = future.result()
            except Exception as exc:                  # pragma: no cover - defensive
                print("%s FAILED %r" % (claim_id, exc))
                continue
            results.append(result)
            flags = (",".join(result.get("flags") or [])) or ""
            print("%s %-7s %5.1fs%s" % (result["claim_id"], result["route"],
                                        result["timings"]["seconds"],
                                        "  [%s]" % flags if flags else ""))

    wall = time.time() - started
    all_results = engine.all_results()
    engine.build_queue(all_results)
    digest, text = engine.build_digest(all_results, wall_seconds=wall)
    engine.audit("run_end", mode="run", seconds=round(wall, 2), cases=len(results))

    print("\nbatch wall time %.1fs, mean %.1fs per case, %.1f output tokens/s"
          % (wall, digest["mean_seconds_per_case"], digest["output_tokens_per_second"]))
    print("quote violations: %d" % digest["quote_violations"])
    print("\n%s" % text)
    if args.json:
        print(json.dumps(digest, indent=2, sort_keys=True))
    return 0


def cmd_reset(args):
    engine.reset()
    print("every case set back to new; out/ cleared except audit.jsonl")
    return 0


def cmd_explain(args):
    """Route, criteria with their sentences, search trail, flags and outputs.

    Prints no patient name, member id or date of birth.
    """
    claim_id = args.claim_id
    if claim_id not in set(engine.all_claim_ids()):
        print("unknown claim %s" % claim_id)
        return 2
    result = engine.load_result(claim_id)
    if result is None:
        print("no result for %s yet; run python3 cg.py run --ids %s" % (claim_id, claim_id))
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    case = engine.load_case(claim_id)
    policy = engine.policy_by_id(case["policy_id"])
    print("%s  %s  policy %s, %s" % (claim_id, case["payer"], policy["policy_id"], policy["title"]))
    print("denial reason: %s" % case["denial_reason"])
    print("policy paths: %s" % " OR ".join(" + ".join(path) for path in policy["paths"]))
    print_case_report(result)
    print("status: %s" % result.get("status"))
    if result.get("approved_by"):
        print("approved by %s on %s" % (result["approved_by"], result["approved_at"]))
    return 0


def cmd_approve(args):
    return _record(args, "approved")


def cmd_reject(args):
    return _record(args, "rejected")


def _record(args, decision):
    try:
        result = engine.approve(args.claim_id, args.by, decision=decision,
                               reason=getattr(args, "reason", "") or "")
    except engine.ApprovalError as exc:
        print("cannot record: %s" % exc)
        return 2
    except FileNotFoundError:
        print("unknown claim %s" % args.claim_id)
        return 2
    print("%s %s by %s" % (args.claim_id, decision, args.by))
    if decision == "approved" and result.get("route") == "APPEAL":
        print("letter re-rendered with the approval line: %s"
              % result["outputs"].get("letter_txt"))
        print("quote violations: %d" % result.get("quote_violations", 0))
    print("Approved is only a status. Nothing was sent anywhere.")
    return 0


def cmd_digest(args):
    path = engine.OUT / "digest.txt"
    if not path.exists():
        print("no digest yet; run python3 cg.py run --new")
        return 2
    sys.stdout.write(path.read_text())
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="cg.py", description="ClaimGuard command line")
    sub = parser.add_subparsers(dest="command")

    selftest = sub.add_parser("selftest", help="check the model endpoint and run one case")
    selftest.add_argument("claim_id", nargs="?", default="A-1001")
    selftest.add_argument("--json", action="store_true", help="print the result as JSON")
    selftest.set_defaults(func=cmd_selftest)

    run = sub.add_parser("run", help="process denials")
    run.add_argument("--new", action="store_true", help="only cases with status new")
    run.add_argument("--ids", help="A-1001,A-1013 or the range A-1001..A-1020")
    run.add_argument("--workers", type=int, help="parallel workers (default CG_WORKERS)")
    run.add_argument("--json", action="store_true", help="also print the digest as JSON")
    run.set_defaults(func=cmd_run)

    reset = sub.add_parser("reset", help="every case back to new, clear out/ except audit.jsonl")
    reset.set_defaults(func=cmd_reset)

    explain = sub.add_parser("explain", help="why a case went the way it did")
    explain.add_argument("claim_id")
    explain.add_argument("--json", action="store_true")
    explain.set_defaults(func=cmd_explain)

    approve = sub.add_parser("approve", help="record a specialist's approval")
    approve.add_argument("claim_id")
    approve.add_argument("--by", required=True, help="the name of the person approving")
    approve.set_defaults(func=cmd_approve)

    reject = sub.add_parser("reject", help="record a specialist's rejection")
    reject.add_argument("claim_id")
    reject.add_argument("--by", required=True)
    reject.add_argument("--reason", default="")
    reject.set_defaults(func=cmd_reject)

    digest = sub.add_parser("digest", help="print the channel-safe summary")
    digest.set_defaults(func=cmd_digest)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("interrupted")
        return 1


if __name__ == "__main__":
    sys.exit(main())
