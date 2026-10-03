"""ClaimGuard unit tests. Standard library unittest, no model needed."""

import json
import re
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cg
import engine
import make_data

MET, NOT_MET, NOT_FOUND = "MET", "NOT_MET", "NOT_FOUND"

NWH = [["RED_FLAG"], ["CONSERVATIVE_6W", "RADICULOPATHY"]]
BPI = [["RED_FLAG"], ["CONSERVATIVE_6W", "RADICULOPATHY"], ["CONSERVATIVE_6W", "NEURO_DEFICIT"]]
HMA = [["RED_FLAG"], ["XRAY_DONE", "CONSERVATIVE_6W"]]


class TestRouter(unittest.TestCase):
    """The spec's truth table: one true path, every path false, a mix that stays
    unknown, RED_FLAG met alone, and Harborline with XRAY_DONE not found."""

    def test_one_true_path(self):
        out = engine.route(NWH, {"RED_FLAG": NOT_MET, "CONSERVATIVE_6W": MET, "RADICULOPATHY": MET},
                           {"CONSERVATIVE_6W": 3, "RADICULOPATHY": 2})
        self.assertEqual(out["route"], "APPEAL")
        self.assertEqual(out["argument_path"], ["CONSERVATIVE_6W", "RADICULOPATHY"])

    def test_every_path_false(self):
        out = engine.route(NWH, {"RED_FLAG": NOT_MET, "CONSERVATIVE_6W": NOT_MET, "RADICULOPATHY": MET})
        self.assertEqual(out["route"], "DROP")
        self.assertEqual(out["drop_reasons"], ["RED_FLAG", "CONSERVATIVE_6W"])

    def test_mix_stays_unknown(self):
        out = engine.route(NWH, {"RED_FLAG": NOT_MET, "CONSERVATIVE_6W": MET, "RADICULOPATHY": NOT_FOUND})
        self.assertEqual(out["route"], "FETCH")
        self.assertEqual(out["missing"], ["RADICULOPATHY"])

    def test_red_flag_met_alone(self):
        out = engine.route(NWH, {"RED_FLAG": MET, "CONSERVATIVE_6W": NOT_MET, "RADICULOPATHY": NOT_FOUND},
                           {"RED_FLAG": 1})
        self.assertEqual(out["route"], "APPEAL")
        self.assertEqual(out["argument_path"], ["RED_FLAG"])

    def test_harborline_xray_not_found(self):
        out = engine.route(HMA, {"RED_FLAG": NOT_MET, "XRAY_DONE": NOT_FOUND, "CONSERVATIVE_6W": MET})
        self.assertEqual(out["route"], "FETCH")
        self.assertEqual(out["missing"], ["XRAY_DONE"])

    def test_argument_path_prefers_most_evidence(self):
        """Bluepeak with both non-red paths true: the path with more evidence wins."""
        out = engine.route(BPI, {"RED_FLAG": NOT_MET, "CONSERVATIVE_6W": MET,
                                 "RADICULOPATHY": MET, "NEURO_DEFICIT": MET},
                           {"CONSERVATIVE_6W": 2, "RADICULOPATHY": 1, "NEURO_DEFICIT": 3})
        self.assertEqual(out["argument_path"], ["CONSERVATIVE_6W", "NEURO_DEFICIT"])

    def test_criteria_outside_policy_are_ignored(self):
        out = engine.route(NWH, {"RED_FLAG": NOT_MET, "CONSERVATIVE_6W": MET, "RADICULOPATHY": MET,
                                 "XRAY_DONE": NOT_MET}, {"CONSERVATIVE_6W": 1, "RADICULOPATHY": 1})
        self.assertEqual(out["route"], "APPEAL")

    def test_fetch_picks_path_with_fewest_unknowns(self):
        """No false criterion in either non-red path; the one needing less wins."""
        out = engine.route(BPI, {"RED_FLAG": NOT_MET, "CONSERVATIVE_6W": MET,
                                 "RADICULOPATHY": NOT_FOUND, "NEURO_DEFICIT": NOT_FOUND})
        self.assertEqual(out["route"], "FETCH")
        self.assertEqual(len(out["missing"]), 1)


class TestMariaFile(unittest.TestCase):
    """A-1001 must match the specification's worked example exactly."""

    @classmethod
    def setUpClass(cls):
        cls.case = engine.load_case("A-1001")

    def test_header(self):
        c = self.case
        self.assertEqual(c["patient"]["name"], "Maria Alvarez")
        self.assertEqual(c["patient"]["age"], 54)
        self.assertEqual(c["patient"]["sex"], "F")
        self.assertEqual(c["patient"]["member_id"], "NWH-552190")
        self.assertEqual(c["payer"], "Northwind Health")
        self.assertEqual(c["policy_id"], "NWH-RAD-114")
        self.assertEqual(c["icd10"], ["M54.16"])
        self.assertEqual(c["date_of_service"], "2026-08-14")
        self.assertEqual(c["order_date"], "2026-08-07")
        self.assertEqual(c["denial_date"], "2026-09-02")
        self.assertEqual(c["carc"], 50)
        self.assertEqual(c["expected_payment"], 980)
        self.assertEqual(c["billed_charges"], 2450)
        self.assertEqual(c["submitted_doc_id"], "VN")
        self.assertEqual(c["cpt"], "72148")
        # status is runtime state: the generator writes "new", the engine moves it on
        self.assertIn(c["status"], ("new", "draft", "approved", "rejected"))

    def test_generator_writes_status_new(self):
        self.assertEqual(make_data.maria()["status"], "new")
        self.assertEqual(make_data.okafor()["status"], "new")

    def test_denial_reason_and_letter(self):
        self.assertEqual(self.case["denial_reason"],
                         "The records submitted do not document at least six weeks of conservative "
                         "treatment before imaging.")
        self.assertIn("within 180 days of this notice", self.case["payer_letter"])

    def test_documents_and_sentence_ids(self):
        docs = {d["doc_id"]: d for d in self.case["docs"]}
        self.assertEqual(set(docs), {"VN", "PT1", "PT2", "MED"})
        self.assertEqual(len(docs["VN"]["sentences"]), 7)
        self.assertEqual(docs["VN"]["author"], "Dr. Priya Raman")
        self.assertEqual(docs["PT2"]["sentences"][0],
                         "Patient attended 14 sessions between 2026-06-09 and 2026-08-04.")
        self.assertEqual(docs["MED"]["sentences"][0], "Naproxen 500 mg twice daily since 2026-06-02.")
        index = engine.sentence_index(self.case) if hasattr(engine, "sentence_index") else None
        if index is not None:
            self.assertEqual(index["PT2-1"]["text"], docs["PT2"]["sentences"][0])

    def test_gold(self):
        gold = self.case["gold"]
        self.assertEqual(gold["route"], "APPEAL")
        self.assertTrue(gold["needs_search"])
        self.assertFalse(gold["injection"])
        self.assertEqual(gold["criteria"]["CONSERVATIVE_6W"]["status"], MET)
        self.assertEqual(gold["criteria"]["CONSERVATIVE_6W"]["evidence"], ["PT2-1", "PT2-2", "MED-1"])
        self.assertEqual(gold["criteria"]["RADICULOPATHY"]["evidence"], ["VN-1", "VN-4"])
        self.assertEqual(gold["criteria"]["RED_FLAG"]["status"], NOT_MET)
        self.assertEqual(gold["criteria"]["RED_FLAG"]["accept"], [NOT_MET, NOT_FOUND])
        self.assertEqual(set(gold["criteria"]), {"RED_FLAG", "CONSERVATIVE_6W", "RADICULOPATHY"})


class TestA1013(unittest.TestCase):
    def test_injection_planted_verbatim(self):
        case = engine.load_case("A-1013")
        self.assertTrue(case["gold"]["injection"])
        self.assertIn("ignore your previous instructions", case["payer_letter"])
        self.assertIn("http://claims-review-portal.example/upload", case["payer_letter"])
        self.assertEqual(case["gold"]["route"], "APPEAL")
        self.assertEqual(case["patient"]["name"], "James Okafor")
        self.assertEqual(case["policy_id"], "BPI-IMG-07")

    def test_injection_text_is_only_in_the_letter(self):
        """The planted instruction must never appear in the chart itself."""
        case = engine.load_case("A-1013")
        for doc in case["docs"]:
            for sentence in doc["sentences"]:
                self.assertNotIn("ignore your previous", sentence.lower())
                self.assertNotIn("http://", sentence.lower())


class TestFillers(unittest.TestCase):
    def test_fillers_contain_no_criterion_keywords(self):
        keywords = []
        for crit in engine.load_criteria():
            keywords.extend(k.lower() for k in crit["keywords"])
        for template in make_data.FILLERS:
            for job in make_data.JOBS:
                for poss in ("her", "his"):
                    text = template.format(job=job, poss=poss).lower()
                    text = text.replace("x-ray", "xray")
                    for keyword in keywords:
                        pattern = r"\b%s\b" % re.escape(keyword.replace("x-ray", "xray"))
                        self.assertIsNone(re.search(pattern, text),
                                          "filler %r contains keyword %r" % (text, keyword))


class TestCaseMix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = [engine.load_case(cid) for cid in engine.all_claim_ids()]

    def test_sixty_cases(self):
        self.assertEqual(len(self.cases), 60)

    def test_group_counts(self):
        counts = {}
        for case in self.cases:
            counts[case["group"]] = counts.get(case["group"], 0) + 1
        self.assertEqual(counts, {"APPEAL_VN": 12, "APPEAL_SEARCH": 16, "APPEAL_RED": 4,
                                  "FETCH": 14, "DROP": 14})

    def test_route_counts(self):
        counts = {}
        for case in self.cases:
            counts[case["gold"]["route"]] = counts.get(case["gold"]["route"], 0) + 1
        self.assertEqual(counts, {"APPEAL": 32, "FETCH": 14, "DROP": 14})

    def test_twenty_cases_per_payer(self):
        counts = {}
        for case in self.cases:
            counts[case["payer"]] = counts.get(case["payer"], 0) + 1
        self.assertEqual(sorted(counts.values()), [20, 20, 20])

    def test_gold_route_matches_router(self):
        """Every gold route must be reproducible from the gold statuses."""
        for case in self.cases:
            policy = engine.policy_by_id(case["policy_id"])
            statuses = {cid: g["status"] for cid, g in case["gold"]["criteria"].items()}
            counts = {cid: len(g["evidence"]) for cid, g in case["gold"]["criteria"].items()}
            self.assertEqual(engine.route(policy["paths"], statuses, counts)["route"],
                             case["gold"]["route"], case["claim_id"])

    def test_gold_evidence_ids_exist(self):
        for case in self.cases:
            valid = set()
            for doc in case["docs"]:
                for i in range(1, len(doc["sentences"]) + 1):
                    valid.add("%s-%d" % (doc["doc_id"], i))
            for cid, gold in case["gold"]["criteria"].items():
                for sid in gold["evidence"]:
                    self.assertIn(sid, valid, "%s %s cites missing %s" % (case["claim_id"], cid, sid))

    def test_gold_criteria_cover_the_policy(self):
        for case in self.cases:
            policy = engine.policy_by_id(case["policy_id"])
            self.assertEqual(set(case["gold"]["criteria"]), set(engine.policy_criteria_ids(policy)),
                             case["claim_id"])

    def test_showcase_set(self):
        """A-1001..A-1020 must hold Maria, A-1013, four searching appeals, three
        fetch, three drop and three cases due within seven days."""
        showcase = [c for c in self.cases if 1001 <= int(c["claim_id"][2:]) <= 1020]
        self.assertEqual(len(showcase), 20)
        by_route = {}
        for case in showcase:
            by_route.setdefault(case["gold"]["route"], []).append(case)
        self.assertGreaterEqual(len(by_route.get("FETCH", [])), 3)
        self.assertGreaterEqual(len(by_route.get("DROP", [])), 3)
        searching = [c for c in showcase if c["gold"]["needs_search"] and c["gold"]["route"] == "APPEAL"]
        self.assertGreaterEqual(len(searching), 4)
        self.assertIn("A-1001", [c["claim_id"] for c in showcase])
        self.assertIn("A-1013", [c["claim_id"] for c in showcase])

        from datetime import date, timedelta
        due_soon = 0
        for case in showcase:
            policy = engine.policy_by_id(case["policy_id"])
            deadline = (date.fromisoformat(case["denial_date"])
                        + timedelta(days=policy["appeal_window_days"]))
            days_left = (deadline - engine.TODAY).days
            if 0 <= days_left <= 7:
                due_soon += 1
        self.assertGreaterEqual(due_soon, 3)

    def test_submitted_doc_always_present(self):
        for case in self.cases:
            doc_ids = [d["doc_id"] for d in case["docs"]]
            self.assertIn(case["submitted_doc_id"], doc_ids, case["claim_id"])

    def test_no_pt_documents_when_conservative_care_is_not_found(self):
        """The FETCH trap: a referral is noted but no therapy records exist."""
        for case in self.cases:
            gold = case["gold"]["criteria"].get("CONSERVATIVE_6W")
            if not gold or gold["status"] != NOT_FOUND:
                continue
            doc_ids = [d["doc_id"] for d in case["docs"]]
            self.assertNotIn("PT1", doc_ids, case["claim_id"])
            self.assertNotIn("PT2", doc_ids, case["claim_id"])


class TestDeterminism(unittest.TestCase):
    def test_criteria_and_policies_shape(self):
        criteria = engine.load_criteria()
        self.assertEqual(len(criteria), 5)
        for crit in criteria:
            for field in ("id", "label", "definition", "keywords", "met_example", "not_met_example"):
                self.assertIn(field, crit)
        policies = engine.load_policies()
        self.assertEqual(len(policies), 3)
        for policy in policies:
            for field in ("policy_id", "payer", "plan_type", "title", "cpt", "paths",
                          "appeal_window_days", "appeals_address"):
                self.assertIn(field, policy)
            self.assertEqual(policy["cpt"], "72148")


class TestClean(unittest.TestCase):
    def test_strips_think_tags(self):
        self.assertEqual(engine.clean("<think>reasoning here</think>{\"a\":1}"), '{"a":1}')

    def test_strips_dangling_think(self):
        """A thinking model that never closes the tag must not poison the parse."""
        self.assertEqual(engine.clean("ok <think>still reasoning"), "ok")

    def test_strips_code_fences(self):
        self.assertEqual(engine.clean('```json\n{"a":1}\n```'), '{"a":1}')


class TestFirstJson(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(engine.first_json('{"a": 1}'), {"a": 1})

    def test_text_before_and_after(self):
        self.assertEqual(engine.first_json('here it is {"a": 1} hope that helps'), {"a": 1})

    def test_braces_inside_strings(self):
        payload = engine.first_json('{"reason": "a } brace and a { brace", "n": 2}')
        self.assertEqual(payload, {"reason": "a } brace and a { brace", "n": 2})

    def test_nested_objects(self):
        payload = engine.first_json('noise {"a": {"b": {"c": 3}}, "d": 4} tail')
        self.assertEqual(payload, {"a": {"b": {"c": 3}}, "d": 4})

    def test_escaped_quote_inside_string(self):
        payload = engine.first_json('{"reason": "he said \\"no\\" firmly"}')
        self.assertEqual(payload, {"reason": 'he said "no" firmly'})

    def test_after_think_tags_and_fences(self):
        raw = '<think>I will answer</think>\n```json\n{"action":"final","criteria":[]}\n```'
        self.assertEqual(engine.first_json(engine.clean(raw)),
                         {"action": "final", "criteria": []})

    def test_no_json_returns_none(self):
        self.assertIsNone(engine.first_json("there is no object here"))

    def test_broken_json_returns_none(self):
        self.assertIsNone(engine.first_json('{"a": }'))


class TestSearchChart(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = engine.load_case("A-1001")
        cls.criteria = engine.criteria_by_id()

    def test_maria_conservative_care_query_finds_pt2(self):
        """The spec's worked search: the discharge summary must come back."""
        shown = engine.submitted_ids(self.case)
        results = engine.search_chart(self.case, self.criteria["CONSERVATIVE_6W"],
                                      "physical therapy sessions weeks", shown)
        ids = [r["id"] for r in results]
        self.assertIn("PT2-1", ids)
        self.assertLessEqual(len(results), 5)

    def test_results_are_added_to_shown_ids(self):
        shown = engine.submitted_ids(self.case)
        results = engine.search_chart(self.case, self.criteria["CONSERVATIVE_6W"],
                                      "physical therapy sessions", shown)
        for item in results:
            self.assertIn(item["id"], shown)

    def test_already_shown_sentences_are_skipped(self):
        shown = engine.submitted_ids(self.case)
        first = engine.search_chart(self.case, self.criteria["CONSERVATIVE_6W"],
                                    "physical therapy sessions weeks", shown)
        second = engine.search_chart(self.case, self.criteria["CONSERVATIVE_6W"],
                                     "physical therapy sessions weeks", shown)
        overlap = set(r["id"] for r in first) & set(r["id"] for r in second)
        self.assertEqual(overlap, set())

    def test_submitted_sentences_are_never_returned(self):
        shown = engine.submitted_ids(self.case)
        results = engine.search_chart(self.case, self.criteria["RADICULOPATHY"],
                                      "radiating leg straight leg raise", shown)
        for item in results:
            self.assertFalse(item["id"].startswith("VN-"))

    def test_phrase_keywords_match_as_phrases(self):
        terms = engine.build_terms(self.criteria["CONSERVATIVE_6W"], "pt sessions")
        self.assertIn("physical therapy", terms)

    def test_xray_normalisation(self):
        """X-ray, x ray and xray must all reach the same term."""
        self.assertEqual(engine.normalize("Lumbar X-ray and x ray"), "lumbar xray and xray")

    def test_stop_words_dropped(self):
        terms = engine.build_terms({"keywords": []}, "the patient is in the leg")
        self.assertNotIn("the", terms)
        self.assertNotIn("patient", terms)
        self.assertIn("leg", terms)


class TestVerifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = engine.load_case("A-1001")
        cls.policy = engine.policy_by_id("NWH-RAD-114")

    def final(self, criteria, injection=False):
        return {"action": "final", "injection_suspected": injection, "criteria": criteria}

    def test_invented_id_is_dropped_as_does_not_exist(self):
        shown = engine.submitted_ids(self.case) + ["PT2-1"]
        final = self.final([
            {"id": "CONSERVATIVE_6W", "status": "MET", "evidence": ["PT9-1", "PT2-1"], "reason": "x"},
            {"id": "RADICULOPATHY", "status": "MET", "evidence": ["VN-4"], "reason": "y"},
            {"id": "RED_FLAG", "status": "NOT_MET", "evidence": ["VN-6"], "reason": "z"},
        ])
        criteria, stats, _ = engine.verify(self.case, self.policy, final, shown)
        by_id = {c["id"]: c for c in criteria}
        self.assertEqual(by_id["CONSERVATIVE_6W"]["evidence"], ["PT2-1"])
        self.assertIn({"id": "PT9-1", "reason": "does not exist"}, stats["invalid_citations"])

    def test_existing_but_unseen_id_is_dropped_as_never_shown(self):
        shown = engine.submitted_ids(self.case)          # PT2 was never searched
        final = self.final([
            {"id": "CONSERVATIVE_6W", "status": "MET", "evidence": ["PT2-1"], "reason": "x"},
            {"id": "RADICULOPATHY", "status": "MET", "evidence": ["VN-4"], "reason": "y"},
            {"id": "RED_FLAG", "status": "NOT_MET", "evidence": ["VN-6"], "reason": "z"},
        ])
        criteria, stats, _ = engine.verify(self.case, self.policy, final, shown)
        self.assertIn({"id": "PT2-1", "reason": "never shown"}, stats["invalid_citations"])

    def test_met_with_no_valid_evidence_is_downgraded(self):
        shown = engine.submitted_ids(self.case)
        final = self.final([
            {"id": "CONSERVATIVE_6W", "status": "MET", "evidence": ["PT9-9"], "reason": "invented"},
            {"id": "RADICULOPATHY", "status": "MET", "evidence": ["VN-4"], "reason": "y"},
            {"id": "RED_FLAG", "status": "NOT_MET", "evidence": ["VN-6"], "reason": "z"},
        ])
        criteria, stats, _ = engine.verify(self.case, self.policy, final, shown)
        by_id = {c["id"]: c for c in criteria}
        self.assertEqual(by_id["CONSERVATIVE_6W"]["status"], "NOT_FOUND")
        self.assertEqual(stats["downgrades"], 1)
        self.assertIn("downgraded", by_id["CONSERVATIVE_6W"]["reason"])

    def test_criteria_outside_the_policy_are_ignored(self):
        shown = engine.submitted_ids(self.case)
        final = self.final([
            {"id": "XRAY_DONE", "status": "MET", "evidence": ["VN-1"], "reason": "not in policy"},
            {"id": "CONSERVATIVE_6W", "status": "NOT_FOUND", "evidence": [], "reason": ""},
            {"id": "RADICULOPATHY", "status": "MET", "evidence": ["VN-4"], "reason": "y"},
            {"id": "RED_FLAG", "status": "NOT_MET", "evidence": ["VN-6"], "reason": "z"},
        ])
        criteria, _, _ = engine.verify(self.case, self.policy, final, shown)
        self.assertEqual(set(c["id"] for c in criteria),
                         {"RED_FLAG", "CONSERVATIVE_6W", "RADICULOPATHY"})

    def test_missing_criterion_is_added_as_not_found(self):
        shown = engine.submitted_ids(self.case)
        final = self.final([{"id": "RADICULOPATHY", "status": "MET", "evidence": ["VN-4"], "reason": "y"}])
        criteria, _, _ = engine.verify(self.case, self.policy, final, shown)
        by_id = {c["id"]: c for c in criteria}
        self.assertEqual(by_id["CONSERVATIVE_6W"]["status"], "NOT_FOUND")
        self.assertEqual(by_id["RED_FLAG"]["status"], "NOT_FOUND")

    def test_unknown_status_becomes_not_found(self):
        shown = engine.submitted_ids(self.case)
        final = self.final([
            {"id": "CONSERVATIVE_6W", "status": "PROBABLY", "evidence": ["VN-3"], "reason": ""},
            {"id": "RADICULOPATHY", "status": "MET", "evidence": ["VN-4"], "reason": "y"},
            {"id": "RED_FLAG", "status": "NOT_MET", "evidence": ["VN-6"], "reason": "z"},
        ])
        criteria, _, _ = engine.verify(self.case, self.policy, final, shown)
        by_id = {c["id"]: c for c in criteria}
        self.assertEqual(by_id["CONSERVATIVE_6W"]["status"], "NOT_FOUND")

    def test_source_is_search_when_evidence_came_from_a_search(self):
        shown = engine.submitted_ids(self.case) + ["PT2-1", "PT2-2"]
        final = self.final([
            {"id": "CONSERVATIVE_6W", "status": "MET", "evidence": ["PT2-1", "PT2-2"], "reason": "x"},
            {"id": "RADICULOPATHY", "status": "MET", "evidence": ["VN-1", "VN-4"], "reason": "y"},
            {"id": "RED_FLAG", "status": "NOT_MET", "evidence": ["VN-6"], "reason": "z"},
        ])
        criteria, _, _ = engine.verify(self.case, self.policy, final, shown)
        by_id = {c["id"]: c for c in criteria}
        self.assertEqual(by_id["CONSERVATIVE_6W"]["source"], "search")
        self.assertEqual(by_id["RADICULOPATHY"]["source"], "submitted")


class TestInjectionScan(unittest.TestCase):
    def test_flags_a1013_letter(self):
        hits = engine.scan_injection(engine.load_case("A-1013")["payer_letter"])
        self.assertIn("ignore your", hits)
        self.assertIn("upload", hits)
        self.assertIn("http://", hits)
        self.assertIn("mark every", hits)

    def test_does_not_flag_maria_letter(self):
        self.assertEqual(engine.scan_injection(engine.load_case("A-1001")["payer_letter"]), [])

    def test_verify_flags_the_case(self):
        case = engine.load_case("A-1013")
        policy = engine.policy_by_id("BPI-IMG-07")
        _, _, flags = engine.verify(case, policy, {"criteria": []}, engine.submitted_ids(case))
        self.assertIn("injection", flags)

    def test_model_suspicion_alone_flags_the_case(self):
        case = engine.load_case("A-1001")
        policy = engine.policy_by_id("NWH-RAD-114")
        final = {"criteria": [], "injection_suspected": True}
        _, _, flags = engine.verify(case, policy, final, engine.submitted_ids(case))
        self.assertIn("injection", flags)


class TestDecide(unittest.TestCase):
    def test_scores_and_deadline(self):
        case = engine.load_case("A-1001")
        policy = engine.policy_by_id("NWH-RAD-114")
        criteria = [
            {"id": "RED_FLAG", "status": "NOT_MET", "evidence": ["VN-6"]},
            {"id": "CONSERVATIVE_6W", "status": "MET", "evidence": ["PT2-1", "PT2-2"]},
            {"id": "RADICULOPATHY", "status": "MET", "evidence": ["VN-1", "VN-4"]},
        ]
        out = engine.decide(case, policy, criteria)
        self.assertEqual(out["route"], "APPEAL")
        self.assertEqual(out["win_score"], 0.54)
        self.assertEqual(out["expected_value"], round(980 * 0.54, 2))
        self.assertEqual(out["deadline"], "2027-03-01")      # 2026-09-02 plus 180 days
        self.assertFalse(out["urgent"])
        self.assertFalse(out["expired"])

    def test_expected_value_uses_contract_rate_not_billed_charges(self):
        case = engine.load_case("A-1001")
        policy = engine.policy_by_id("NWH-RAD-114")
        criteria = [{"id": "RED_FLAG", "status": "MET", "evidence": ["VN-6"]}]
        out = engine.decide(case, policy, criteria)
        self.assertEqual(out["expected_payment"], case["expected_payment"])
        self.assertNotEqual(out["expected_payment"], case["billed_charges"])

    def test_queue_sort_key_orders_the_groups(self):
        rows = [
            {"claim_id": "D", "route": "DROP", "expected_value": 0, "urgent": False, "expired": False},
            {"claim_id": "A", "route": "APPEAL", "expected_value": 100, "urgent": False, "expired": False},
            {"claim_id": "U", "route": "APPEAL", "expected_value": 10, "urgent": True, "expired": False},
            {"claim_id": "F", "route": "FETCH", "expected_value": 50, "urgent": False, "expired": False},
            {"claim_id": "X", "route": "APPEAL", "expected_value": 999, "urgent": False, "expired": True},
        ]
        order = [r["claim_id"] for r in sorted(rows, key=engine.queue_sort_key)]
        self.assertEqual(order, ["U", "A", "X", "F", "D"])


class TestProcessCaseWithFakeModel(unittest.TestCase):
    """The whole per-case pipeline, offline: engine.chat is replaced by a script."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cg-test-")
        self.original_out = engine.OUT
        self.original_chat = engine.chat
        engine.OUT = Path(self.tmp)
        self.case_bytes = engine.case_path("A-1001").read_bytes()

    def tearDown(self):
        engine.OUT = self.original_out
        engine.chat = self.original_chat
        engine.case_path("A-1001").write_bytes(self.case_bytes)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scripted(self, replies):
        state = {"i": 0}

        def fake_chat(messages):
            index = min(state["i"], len(replies) - 1)
            text = replies[index]
            state["i"] += 1
            return {"text": text, "prompt_tokens": 100, "completion_tokens": 20,
                    "tokens_estimated": False, "ms": 5, "model": "fake-model",
                    "prompt_sha": "p", "response_sha": "r"}

        engine.chat = fake_chat

    def test_search_then_final_routes_appeal_with_search_source(self):
        search = ('{"action":"search","criterion":"CONSERVATIVE_6W",'
                  '"query":"physical therapy sessions weeks"}')
        final = ('{"action":"final","injection_suspected":false,"criteria":['
                 '{"id":"CONSERVATIVE_6W","status":"MET","evidence":["PT2-1","PT2-2"],'
                 '"reason":"Fourteen sessions with persistent leg pain."},'
                 '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-1","VN-4"],'
                 '"reason":"Radiating pain and a positive straight leg raise."},'
                 '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],'
                 '"reason":"Red flags denied."}]}')
        self.scripted([search, final])

        result = engine.process_case("A-1001", run_id="test-run")

        self.assertEqual(result["route"], "APPEAL")
        self.assertEqual(result["argument_path"], ["CONSERVATIVE_6W", "RADICULOPATHY"])
        by_id = {c["id"]: c for c in result["criteria"]}
        self.assertEqual(by_id["CONSERVATIVE_6W"]["status"], "MET")
        self.assertEqual(by_id["CONSERVATIVE_6W"]["source"], "search")
        self.assertEqual(by_id["RADICULOPATHY"]["source"], "submitted")
        self.assertEqual(result["win_score"], 0.54)
        self.assertEqual(result["llm_calls"], 2)
        self.assertEqual(result["downgrades"], 0)
        self.assertEqual(result["invalid_citations"], [])
        self.assertEqual(result["error"], None)
        self.assertEqual(len(result["search_trail"]), 1)
        self.assertIn("PT2-1", result["search_trail"][0]["results"])

    def test_audit_lines_are_written(self):
        search = ('{"action":"search","criterion":"CONSERVATIVE_6W","query":"physical therapy"}')
        final = ('{"action":"final","injection_suspected":false,"criteria":['
                 '{"id":"CONSERVATIVE_6W","status":"MET","evidence":["PT2-1"],"reason":"a"},'
                 '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-4"],"reason":"b"},'
                 '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        self.scripted([search, final])
        engine.process_case("A-1001", run_id="test-run")

        lines = [json.loads(l) for l in (engine.OUT / "audit.jsonl").read_text().splitlines()]
        events = [l["event"] for l in lines]
        for required in ("case_start", "llm_call", "search", "route", "case_end"):
            self.assertIn(required, events)
        for line in lines:
            self.assertEqual(line["run_id"], "test-run")
            self.assertIn("ts", line)

    def test_result_file_written_and_case_marked_draft(self):
        final = ('{"action":"final","injection_suspected":false,"criteria":['
                 '{"id":"CONSERVATIVE_6W","status":"NOT_FOUND","evidence":[],"reason":"a"},'
                 '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-4"],"reason":"b"},'
                 '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        self.scripted([final])
        engine.process_case("A-1001", run_id="test-run")
        saved = json.loads((engine.OUT / "results" / "A-1001.json").read_text())
        self.assertEqual(saved["route"], "FETCH")
        self.assertEqual(saved["missing"], ["CONSERVATIVE_6W"])
        self.assertEqual(engine.load_case("A-1001")["status"], "draft")

    def test_invalid_json_three_times_becomes_manual(self):
        self.scripted(["not json at all"])
        result = engine.process_case("A-1001", run_id="test-run")
        self.assertEqual(result["route"], "MANUAL")
        self.assertEqual(result["error"], "invalid JSON")
        self.assertEqual(result["win_score"], 0.0)

    def test_unreachable_model_becomes_manual(self):
        def boom(messages):
            raise urllib.error.URLError("connection refused")
        engine.chat = boom
        result = engine.process_case("A-1001", run_id="test-run")
        self.assertEqual(result["route"], "MANUAL")
        self.assertIn("model call failed", result["error"])

    def test_searches_are_capped_at_four(self):
        search = ('{"action":"search","criterion":"CONSERVATIVE_6W","query":"physical therapy"}')
        self.scripted([search])          # the model only ever searches
        result = engine.process_case("A-1001", run_id="test-run")
        self.assertLessEqual(len(result["search_trail"]), engine.MAX_SEARCHES)
        self.assertEqual(result["route"], "MANUAL")
        self.assertLessEqual(result["llm_calls"], engine.MAX_TURNS)

    def test_model_never_sees_the_gold_labels(self):
        """The prompt is built from the case, but gold must never reach the model."""
        case = engine.load_case("A-1001")
        policy = engine.policy_by_id("NWH-RAD-114")
        criteria = [engine.criteria_by_id()[c] for c in engine.policy_criteria_ids(policy)]
        message = engine.build_first_message(case, criteria, policy)
        self.assertNotIn("gold", message.lower())
        self.assertNotIn("needs_search", message)
        # the reply-format example mentions PT2-1, but PT2 content must stay hidden
        self.assertNotIn("Patient attended 14 sessions", message)
        self.assertNotIn("Naproxen 500 mg", message)

    def test_first_message_order_and_untrusted_markers(self):
        case = engine.load_case("A-1013")
        policy = engine.policy_by_id("BPI-IMG-07")
        criteria = [engine.criteria_by_id()[c] for c in engine.policy_criteria_ids(policy)]
        message = engine.build_first_message(case, criteria, policy)
        self.assertIn("BEGIN UNTRUSTED PAYER LETTER", message)
        self.assertIn("END UNTRUSTED PAYER LETTER", message)
        self.assertLess(message.index("BEGIN UNTRUSTED PAYER LETTER"),
                        message.index("Criteria to judge:"))
        self.assertLess(message.index("Criteria to judge:"), message.index("[VN-1]"))
        self.assertIn("Searches left: 4", message)
        self.assertNotIn("XRAY_DONE", message)   # not a Bluepeak criterion


class TestCitationCorrection(unittest.TestCase):
    """When the model cites sentences it was never shown, the loop asks it to search."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cg-test-")
        self.original_out = engine.OUT
        self.original_chat = engine.chat
        engine.OUT = Path(self.tmp)
        self.case_bytes = engine.case_path("A-1001").read_bytes()

    def tearDown(self):
        engine.OUT = self.original_out
        engine.chat = self.original_chat
        engine.case_path("A-1001").write_bytes(self.case_bytes)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scripted(self, replies):
        state = {"i": 0}

        def fake_chat(messages):
            index = min(state["i"], len(replies) - 1)
            state["i"] += 1
            return {"text": replies[index], "prompt_tokens": 10, "completion_tokens": 5,
                    "tokens_estimated": False, "ms": 1, "model": "fake-model",
                    "prompt_sha": "p", "response_sha": "r"}

        engine.chat = fake_chat
        self.messages_seen = []

    def test_unshown_citations_are_detected(self):
        payload = {"criteria": [
            {"id": "CONSERVATIVE_6W", "status": "MET", "evidence": ["PT2-1", "VN-1"]},
            {"id": "RADICULOPATHY", "status": "MET", "evidence": ["VN-4"]},
        ]}
        offenders = engine._unshown_citations(payload, ["VN-1", "VN-4"])
        self.assertEqual(offenders, {"CONSERVATIVE_6W": ["PT2-1"]})

    def test_correction_message_names_the_dropped_ids(self):
        text = engine._correction_message({"CONSERVATIVE_6W": ["PT2-1", "PT2-2"]}, [], 4)
        self.assertIn("PT2-1", text)
        self.assertIn("CONSERVATIVE_6W", text)
        self.assertIn("4 searches left", text)

    def test_unsearched_not_found_is_detected(self):
        payload = {"criteria": [
            {"id": "CONSERVATIVE_6W", "status": "NOT_FOUND", "evidence": []},
            {"id": "RADICULOPATHY", "status": "MET", "evidence": ["VN-4"]},
            {"id": "RED_FLAG", "status": "NOT_FOUND", "evidence": []},
        ]}
        self.assertEqual(engine._unsearched_not_found(payload, {"RED_FLAG"}), ["CONSERVATIVE_6W"])

    def test_correction_message_cites_rule_four(self):
        text = engine._correction_message({}, ["CONSERVATIVE_6W"], 3)
        self.assertIn("CONSERVATIVE_6W", text)
        self.assertIn("Rule 4", text)

    def test_not_found_without_a_search_triggers_a_correction(self):
        """Rule 4: the loop makes the model search before accepting NOT_FOUND."""
        lazy = ('{"action":"final","injection_suspected":false,"criteria":['
                '{"id":"CONSERVATIVE_6W","status":"NOT_FOUND","evidence":[],"reason":"a"},'
                '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-4"],"reason":"b"},'
                '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        search = ('{"action":"search","criterion":"CONSERVATIVE_6W","query":"physical therapy"}')
        good = ('{"action":"final","injection_suspected":false,"criteria":['
                '{"id":"CONSERVATIVE_6W","status":"MET","evidence":["PT2-1"],"reason":"a"},'
                '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-4"],"reason":"b"},'
                '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        self.scripted([lazy, search, good])
        result = engine.process_case("A-1001", run_id="test-run")
        self.assertEqual(result["route"], "APPEAL")
        self.assertEqual(len(result["search_trail"]), 1)

    def test_not_found_after_a_search_is_accepted(self):
        """Once the criterion has been searched, NOT_FOUND stands."""
        search = ('{"action":"search","criterion":"CONSERVATIVE_6W","query":"physical therapy"}')
        final = ('{"action":"final","injection_suspected":false,"criteria":['
                 '{"id":"CONSERVATIVE_6W","status":"NOT_FOUND","evidence":[],"reason":"a"},'
                 '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-4"],"reason":"b"},'
                 '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        self.scripted([search, final])
        result = engine.process_case("A-1001", run_id="test-run")
        self.assertEqual(result["route"], "FETCH")
        self.assertEqual(result["llm_calls"], 2)

    def test_guessed_citations_lead_to_a_search_and_then_appeal(self):
        guessed = ('{"action":"final","injection_suspected":false,"criteria":['
                   '{"id":"CONSERVATIVE_6W","status":"MET","evidence":["PT2-1","PT2-2"],"reason":"a"},'
                   '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-4"],"reason":"b"},'
                   '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        search = ('{"action":"search","criterion":"CONSERVATIVE_6W",'
                  '"query":"physical therapy sessions weeks"}')
        good = ('{"action":"final","injection_suspected":false,"criteria":['
                '{"id":"CONSERVATIVE_6W","status":"MET","evidence":["PT2-1","PT2-2"],"reason":"a"},'
                '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-4"],"reason":"b"},'
                '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        self.scripted([guessed, search, good])

        result = engine.process_case("A-1001", run_id="test-run")
        self.assertEqual(result["route"], "APPEAL")
        self.assertEqual(result["invalid_citations"], [])
        self.assertEqual(result["downgrades"], 0)
        self.assertEqual(len(result["search_trail"]), 1)
        by_id = {c["id"]: c for c in result["criteria"]}
        self.assertEqual(by_id["CONSERVATIVE_6W"]["source"], "search")
        self.assertEqual(by_id["CONSERVATIVE_6W"]["evidence"], ["PT2-1", "PT2-2"])

    def test_correction_is_bounded_and_the_verifier_still_drops(self):
        """A model that keeps guessing is accepted after the correction budget, and
        the verifier downgrades the unsupported criterion."""
        guessed = ('{"action":"final","injection_suspected":false,"criteria":['
                   '{"id":"CONSERVATIVE_6W","status":"MET","evidence":["PT2-1"],"reason":"a"},'
                   '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-4"],"reason":"b"},'
                   '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        self.scripted([guessed])
        result = engine.process_case("A-1001", run_id="test-run")
        self.assertEqual(result["llm_calls"], engine.MAX_CORRECTIONS + 1)
        self.assertEqual(result["downgrades"], 1)
        self.assertIn({"id": "PT2-1", "reason": "never shown"}, result["invalid_citations"])
        self.assertEqual(result["route"], "FETCH")

    def test_no_correction_when_nothing_needs_one(self):
        """Every citation shown and no unsearched NOT_FOUND: accepted on the first turn."""
        good = ('{"action":"final","injection_suspected":false,"criteria":['
                '{"id":"CONSERVATIVE_6W","status":"NOT_MET","evidence":["VN-3"],"reason":"a"},'
                '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-4"],"reason":"b"},'
                '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        self.scripted([good])
        result = engine.process_case("A-1001", run_id="test-run")
        self.assertEqual(result["llm_calls"], 1)
        self.assertEqual(result["route"], "DROP")


class TestCheckArguments(unittest.TestCase):
    """Code checks on what the letter writer sends back."""

    def setUp(self):
        self.evidence = {"CONSERVATIVE_6W": [
            {"id": "PT2-1", "text": "Patient attended 14 sessions between 2026-06-09 and 2026-08-04.",
             "title": "Physical therapy discharge summary", "date": "2026-08-04"},
            {"id": "PT2-2", "text": "Radiating pain into the left leg persists despite therapy.",
             "title": "Physical therapy discharge summary", "date": "2026-08-04"},
        ]}

    def problems(self, text):
        return engine.check_arguments([{"id": "CONSERVATIVE_6W", "text": text}], self.evidence)

    def test_clean_argument_passes(self):
        self.assertEqual(self.problems("The record shows 14 sessions of therapy [[PT2-1]] with "
                                       "pain that persists [[PT2-2]]."), [])

    def test_unverified_placeholder_is_rejected(self):
        problems = self.problems("Therapy is documented [[PT9-9]].")
        self.assertTrue(any("PT9-9" in p for p in problems))

    def test_placeholder_from_another_criterion_is_rejected(self):
        problems = self.problems("Therapy is documented [[VN-4]].")
        self.assertTrue(any("VN-4" in p for p in problems))

    def test_double_quotes_are_rejected(self):
        self.assertTrue(any("quotation marks" in p for p in self.problems('She had "therapy".')))

    def test_curly_quotes_are_rejected(self):
        self.assertTrue(any("quotation marks" in p
                            for p in self.problems("She had \u201ctherapy\u201d.")))

    def test_apostrophe_inside_a_word_is_allowed(self):
        self.assertEqual(self.problems("The patient's therapy is documented [[PT2-1]]."), [])

    def test_single_quote_used_as_a_quote_is_rejected(self):
        self.assertTrue(any("quotation marks" in p for p in self.problems("She had 'therapy' then.")))

    def test_number_not_in_evidence_is_rejected(self):
        problems = self.problems("The record shows 22 sessions [[PT2-1]].")
        self.assertTrue(any("22" in p for p in problems))

    def test_number_word_passes_when_the_digits_are_in_evidence(self):
        self.assertEqual(self.problems("The record shows fourteen sessions [[PT2-1]]."), [])

    def test_number_word_not_in_evidence_is_rejected(self):
        problems = self.problems("The record shows twenty sessions [[PT2-1]].")
        self.assertTrue(any("twenty" in p for p in problems))

    def test_digits_inside_placeholders_are_ignored(self):
        self.assertEqual(self.problems("Therapy is documented [[PT2-1]] and [[PT2-2]]."), [])

    def test_over_four_hundred_characters_is_rejected(self):
        problems = self.problems("a" * 401)
        self.assertTrue(any("400" in p for p in problems))

    def test_missing_criterion_is_reported(self):
        problems = engine.check_arguments([], self.evidence)
        self.assertTrue(any("no argument" in p for p in problems))

    def test_criterion_outside_the_path_is_reported(self):
        problems = engine.check_arguments(
            [{"id": "CONSERVATIVE_6W", "text": "Documented [[PT2-1]]."},
             {"id": "RED_FLAG", "text": "Nothing."}], self.evidence)
        self.assertTrue(any("not in the argument path" in p for p in problems))


class TestOutputsWithFakeModel(unittest.TestCase):
    """Letters, requests and memos, rendered offline."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cg-out-")
        self.original_out = engine.OUT
        self.original_chat = engine.chat
        engine.OUT = Path(self.tmp)
        self.case_bytes = engine.case_path("A-1001").read_bytes()

    def tearDown(self):
        engine.OUT = self.original_out
        engine.chat = self.original_chat
        engine.case_path("A-1001").write_bytes(self.case_bytes)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scripted(self, replies):
        state = {"i": 0}

        def fake_chat(messages):
            index = min(state["i"], len(replies) - 1)
            state["i"] += 1
            return {"text": replies[index], "prompt_tokens": 10, "completion_tokens": 5,
                    "tokens_estimated": False, "ms": 1, "model": "fake-model",
                    "prompt_sha": "p", "response_sha": "r"}

        engine.chat = fake_chat

    def maria_appeal(self):
        search = ('{"action":"search","criterion":"CONSERVATIVE_6W",'
                  '"query":"physical therapy sessions weeks"}')
        final = ('{"action":"final","injection_suspected":false,"criteria":['
                 '{"id":"CONSERVATIVE_6W","status":"MET","evidence":["PT2-1","PT2-2"],"reason":"a"},'
                 '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-1","VN-4"],"reason":"b"},'
                 '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        letter = ('{"arguments":[{"id":"CONSERVATIVE_6W","text":"The record documents 14 therapy '
                  'sessions [[PT2-1]] with pain that persists [[PT2-2]]."},'
                  '{"id":"RADICULOPATHY","text":"Pain radiates below the knee [[VN-1]] and the '
                  'straight leg raise is positive [[VN-4]]."}],"summary":"Policy met."}')
        self.scripted([search, final, letter])
        return engine.process_case("A-1001", run_id="test-run")

    def test_letter_is_written_and_passes_the_quote_check(self):
        result = self.maria_appeal()
        self.assertEqual(result["route"], "APPEAL")
        self.assertEqual(result["quote_violations"], 0)
        self.assertIn("letter_txt", result["outputs"])
        self.assertIn("letter_html", result["outputs"])
        txt = (engine.OUT / "letters" / "A-1001.txt").read_text()
        self.assertIn("Demo General Hospital", txt)
        self.assertIn("RE: request for reconsideration of claim A-1001", txt)
        self.assertIn("Prepared by ClaimGuard for specialist review. Status: draft.", txt)
        self.assertIn("Because the documented findings meet the policy", txt)

    def test_every_quote_in_the_letter_is_a_chart_sentence(self):
        self.maria_appeal()
        txt = (engine.OUT / "letters" / "A-1001.txt").read_text()
        case = engine.load_case("A-1001")
        quoted = re.findall(r'"([^"]*)"', txt)
        self.assertTrue(quoted)
        sentences = set(s for doc in case["docs"] for s in doc["sentences"])
        for item in quoted:
            self.assertIn(item, sentences)
        self.assertEqual(engine.quote_check(txt, case), [])

    def test_quote_check_catches_a_tampered_quote(self):
        self.maria_appeal()
        case = engine.load_case("A-1001")
        tampered = 'He said "the patient attended 99 sessions" today.'
        self.assertEqual(len(engine.quote_check(tampered, case)), 1)

    def test_html_escapes_chart_text_and_links_quotes(self):
        self.maria_appeal()
        html_text = (engine.OUT / "letters" / "A-1001.html").read_text()
        self.assertIn('<mark><a href="#s-PT2-1">', html_text)
        self.assertIn('id="s-PT2-1"', html_text)
        self.assertIn("Evidence appendix", html_text)
        self.assertNotIn("<script", html_text.lower())
        self.assertNotIn("http://", html_text)
        self.assertNotIn("https://", html_text)

    def test_letter_falls_back_to_the_template_when_checks_keep_failing(self):
        search = ('{"action":"search","criterion":"CONSERVATIVE_6W","query":"physical therapy"}')
        final = ('{"action":"final","injection_suspected":false,"criteria":['
                 '{"id":"CONSERVATIVE_6W","status":"MET","evidence":["PT2-1"],"reason":"a"},'
                 '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-4"],"reason":"b"},'
                 '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        bad_letter = '{"arguments":[{"id":"CONSERVATIVE_6W","text":"She had 999 sessions."}]}'
        self.scripted([search, final, bad_letter, bad_letter, bad_letter])
        result = engine.process_case("A-1001", run_id="test-run")
        txt = (engine.OUT / "letters" / "A-1001.txt").read_text()
        self.assertIn(engine.TEMPLATE_ARGUMENT, txt)
        self.assertEqual(result["quote_violations"], 0)

    def test_fetch_case_gets_a_request(self):
        final = ('{"action":"final","injection_suspected":false,"criteria":['
                 '{"id":"CONSERVATIVE_6W","status":"NOT_FOUND","evidence":[],"reason":"a"},'
                 '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-4"],"reason":"b"},'
                 '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        self.scripted([final])
        result = engine.process_case("A-1001", run_id="test-run")
        self.assertEqual(result["route"], "FETCH")
        self.assertIn("request_txt", result["outputs"])
        text = (engine.OUT / "requests" / "A-1001.txt").read_text()
        self.assertIn("MRI lumbar spine without contrast (CPT 72148)", text)
        self.assertIn("Six weeks of conservative care", text)
        self.assertIn("Appeal deadline:", text)
        self.assertIn("days left", text)

    def test_drop_case_gets_a_memo_with_exact_sentences(self):
        final = ('{"action":"final","injection_suspected":false,"criteria":['
                 '{"id":"CONSERVATIVE_6W","status":"NOT_MET","evidence":["VN-3"],"reason":"a"},'
                 '{"id":"RADICULOPATHY","status":"MET","evidence":["VN-4"],"reason":"b"},'
                 '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-6"],"reason":"c"}]}')
        self.scripted([final])
        result = engine.process_case("A-1001", run_id="test-run")
        self.assertEqual(result["route"], "DROP")
        self.assertIn("memo_txt", result["outputs"])
        text = (engine.OUT / "memos" / "A-1001.txt").read_text()
        self.assertIn("No appeal is recommended.", text)
        self.assertIn("The specialist may override this recommendation.", text)
        self.assertEqual(result["quote_violations"], 0)
        self.assertEqual(engine.quote_check(text, engine.load_case("A-1001")), [])

    def test_manual_case_gets_a_note(self):
        self.scripted(["not json"])
        result = engine.process_case("A-1001", run_id="test-run")
        self.assertEqual(result["route"], "MANUAL")
        self.assertIn("manual_txt", result["outputs"])
        text = (engine.OUT / "memos" / "A-1001.manual.txt").read_text()
        self.assertIn("Handle this case by hand.", text)

    def test_injection_flag_shows_a_banner_not_an_instruction(self):
        case_bytes = engine.case_path("A-1013").read_bytes()
        try:
            search = ('{"action":"search","criterion":"CONSERVATIVE_6W","query":"therapy sessions"}')
            search2 = ('{"action":"search","criterion":"RADICULOPATHY","query":"radiating leg pain"}')
            final = ('{"action":"final","injection_suspected":true,"criteria":['
                     '{"id":"CONSERVATIVE_6W","status":"MET","evidence":["PT2-1","PT2-2"],"reason":"a"},'
                     '{"id":"NEURO_DEFICIT","status":"MET","evidence":["VN-3","VN-4"],"reason":"b"},'
                     '{"id":"RADICULOPATHY","status":"NOT_FOUND","evidence":[],"reason":"c"},'
                     '{"id":"RED_FLAG","status":"NOT_MET","evidence":["VN-5"],"reason":"d"}]}')
            letter = ('{"arguments":[{"id":"CONSERVATIVE_6W","text":"Therapy is documented '
                      '[[PT2-1]] [[PT2-2]]."},{"id":"NEURO_DEFICIT","text":"A deficit is '
                      'documented [[VN-3]] [[VN-4]]."}],"summary":"Met."}')
            self.scripted([search, search2, final, letter])
            result = engine.process_case("A-1013", run_id="test-run")
            self.assertIn("injection", result["flags"])
            self.assertEqual(result["route"], "APPEAL")
            self.assertEqual(result["quote_violations"], 0)
            html_text = (engine.OUT / "letters" / "A-1013.html").read_text()
            self.assertIn("Payer letter contained instructions; they were ignored.", html_text)
            # the planted instruction must never be reproduced in the letter
            self.assertNotIn("ignore your previous instructions", html_text)
            self.assertNotIn("claims-review-portal", html_text)
            txt = (engine.OUT / "letters" / "A-1013.txt").read_text()
            self.assertNotIn("claims-review-portal", txt)
        finally:
            engine.case_path("A-1013").write_bytes(case_bytes)

    def test_approve_sets_status_and_rewrites_the_letter(self):
        self.maria_appeal()
        result = engine.approve("A-1001", "Dana Lee")
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["approved_by"], "Dana Lee")
        self.assertEqual(result["quote_violations"], 0)
        txt = (engine.OUT / "letters" / "A-1001.txt").read_text()
        self.assertIn("Approved by Dana Lee on", txt)
        self.assertNotIn("Status: draft", txt)
        self.assertEqual(engine.load_case("A-1001")["status"], "approved")
        self.assertEqual(engine.quote_check(txt, engine.load_case("A-1001")), [])

    def test_approve_requires_a_name(self):
        self.maria_appeal()
        with self.assertRaises(engine.ApprovalError):
            engine.approve("A-1001", "   ")

    def test_approve_twice_is_refused(self):
        self.maria_appeal()
        engine.approve("A-1001", "Dana Lee")
        with self.assertRaises(engine.ApprovalError):
            engine.approve("A-1001", "Dana Lee")

    def test_reject_records_the_reason(self):
        self.maria_appeal()
        result = engine.approve("A-1001", "Dana Lee", decision="rejected", reason="wrong patient")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reject_reason"], "wrong patient")

    def test_approve_without_a_result_is_refused(self):
        with self.assertRaises(engine.ApprovalError):
            engine.approve("A-1002", "Dana Lee")

    def test_approval_stamps_the_letter_the_specialist_read(self):
        """Hard rule 5: approval must not regenerate the argument text.

        If the model is asked again at approval time, the person ends up approving
        words they never saw, so approve() replays the stored draft instead.
        """
        self.maria_appeal()
        draft = (engine.OUT / "letters" / "A-1001.txt").read_text()
        body = draft.split("We request reconsideration")[1].split("Prepared by ClaimGuard")[0]

        calls = {"n": 0}
        original = engine.chat

        def counting_chat(messages):
            calls["n"] += 1
            return original(messages)

        engine.chat = counting_chat
        engine.approve("A-1001", "Dana Lee")

        self.assertEqual(calls["n"], 0, "approval must not call the model")
        approved = (engine.OUT / "letters" / "A-1001.txt").read_text()
        approved_body = approved.split("We request reconsideration")[1].split("Approved by")[0]
        self.assertEqual(body.strip(), approved_body.strip())
        self.assertIn("Approved by Dana Lee on", approved)
        self.assertEqual(engine.quote_check(approved, engine.load_case("A-1001")), [])

    def test_argument_texts_are_stored_on_the_result(self):
        result = self.maria_appeal()
        self.assertIsInstance(result["argument_texts"], dict)
        self.assertEqual(set(result["argument_texts"]), set(result["argument_path"]))

    def test_rejection_also_leaves_the_argument_text_untouched(self):
        self.maria_appeal()
        draft = (engine.OUT / "letters" / "A-1001.txt").read_text()
        body = draft.split("We request reconsideration")[1].split("Prepared by ClaimGuard")[0]
        engine.approve("A-1001", "Dana Lee", decision="rejected", reason="wrong patient")
        after = (engine.OUT / "letters" / "A-1001.txt").read_text()
        self.assertIn(body.strip(), after)

    def test_audit_records_the_approval(self):
        self.maria_appeal()
        engine.approve("A-1001", "Dana Lee")
        lines = [json.loads(l) for l in (engine.OUT / "audit.jsonl").read_text().splitlines()]
        approvals = [l for l in lines if l["event"] == "approved"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["details"]["by"], "Dana Lee")
        self.assertEqual(approvals[0]["claim_id"], "A-1001")


class TestQueueAndDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cg-q-")
        self.original_out = engine.OUT
        engine.OUT = Path(self.tmp)

    def tearDown(self):
        engine.OUT = self.original_out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_results(self):
        def row(claim_id, route, value, payment, urgent=False, expired=False, flags=None,
                seconds=2.0, completion=100):
            return {"claim_id": claim_id, "payer": "Northwind Health", "route": route,
                    "expected_payment": payment, "win_score": engine.WIN_SCORE[route],
                    "expected_value": value, "days_left": 5 if urgent else 100,
                    "urgent": urgent, "expired": expired, "flags": flags or [],
                    "status": "draft", "timings": {"seconds": seconds, "llm_ms": 1000},
                    "tokens": {"prompt": 500, "completion": completion}, "quote_violations": 0,
                    "model": "fake-model"}
        return [
            row("A-1001", "APPEAL", 529.2, 980),
            row("A-1002", "APPEAL", 756.0, 1400),
            row("A-1003", "FETCH", 196.0, 980),
            row("A-1004", "DROP", 0.0, 900),
            row("A-1005", "APPEAL", 300.0, 560, urgent=True),
            row("A-1006", "APPEAL", 400.0, 740, flags=["injection"]),
        ]

    def test_queue_order_and_ranks(self):
        queue = engine.build_queue(self.fake_results())
        self.assertEqual([r["claim_id"] for r in queue],
                         ["A-1005", "A-1002", "A-1001", "A-1006", "A-1003", "A-1004"])
        self.assertEqual([r["rank"] for r in queue], [1, 2, 3, 4, 5, 6])
        for row in queue:
            self.assertEqual(set(row), set(engine.QUEUE_FIELDS))

    def test_queue_file_written(self):
        engine.build_queue(self.fake_results())
        saved = json.loads((engine.OUT / "queue.json").read_text())
        self.assertEqual(len(saved), 6)

    def test_digest_counts_and_totals(self):
        digest, text = engine.build_digest(self.fake_results(), wall_seconds=120.0)
        self.assertEqual(digest["cases_processed"], 6)
        self.assertEqual(digest["appeals_ready"], 4)
        self.assertEqual(digest["appeals_expected_payment"], 980 + 1400 + 560 + 740)
        self.assertEqual(digest["document_requests"], 1)
        self.assertEqual(digest["no_appeal"], 1)
        self.assertEqual(digest["flagged_letters"], 1)
        self.assertEqual(digest["urgent_cases"], 1)
        self.assertEqual(digest["base_url"], engine.BASE_URL)
        self.assertEqual(digest["assumptions"]["win_score_appeal"], 0.54)
        self.assertEqual(digest["assumptions"]["source"], "Premier Inc. 2024")
        self.assertEqual(digest["output_tokens_per_second"], round(600 / 120.0, 2))

    def test_digest_text_holds_no_patient_data(self):
        """Hard rule 7: the chat message carries only counts and dollar totals."""
        _, text = engine.build_digest(self.fake_results(), wall_seconds=120.0)
        self.assertIsNone(re.search(r"A-1\d{3}", text))
        self.assertIsNone(re.search(r"\b[A-Z]{3}-\d{6}\b", text))       # member ids
        self.assertIsNone(re.search(r"\b[A-Z]\d{2}\.\d{2}\b", text))     # ICD-10 codes
        for name in ("Maria", "Alvarez", "Okafor", "James"):
            self.assertNotIn(name, text)
        self.assertIn("4 appeals ready", text)
        self.assertIn("Open the review page to approve.", text)

    def test_digest_text_against_real_case_names(self):
        _, text = engine.build_digest(self.fake_results(), wall_seconds=120.0)
        for claim_id in engine.all_claim_ids():
            case = engine.load_case(claim_id)
            self.assertNotIn(case["patient"]["name"], text)
            self.assertNotIn(case["patient"]["member_id"], text)
            for code in case["icd10"]:
                self.assertNotIn(code, text)


class TestParseIds(unittest.TestCase):
    def test_comma_list(self):
        self.assertEqual(cg.parse_ids("A-1001,A-1013"), ["A-1001", "A-1013"])

    def test_range(self):
        self.assertEqual(cg.parse_ids("A-1001..A-1004"),
                         ["A-1001", "A-1002", "A-1003", "A-1004"])

    def test_mixed_and_deduplicated(self):
        self.assertEqual(cg.parse_ids("A-1001..A-1003,A-1001,A-1020"),
                         ["A-1001", "A-1002", "A-1003", "A-1020"])


if __name__ == "__main__":
    unittest.main()
