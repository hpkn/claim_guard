"""ClaimGuard review page (Streamlit).

Reads out/ and data/ only. The only write is a specialist's decision, which goes
through engine.approve, the same function `cg.py approve` uses. Every string that
comes from a case or a payer letter is escaped before it is put into HTML.

Run: cd claimguard && streamlit run review_app.py
"""

import html
import json
import os
import platform
import re
import subprocess
import sys
from datetime import date
from urllib.parse import urlencode, urlparse

import streamlit as st
import streamlit.components.v1 as components

import engine

st.set_page_config(page_title="ClaimGuard", page_icon="🛡️", layout="wide",
                   initial_sidebar_state="collapsed")

NAVY = "#12284C"
FOOTER = ("Synthetic data: Demo General Hospital, fictional payers and patients. "
          "Nothing leaves this machine.")
ASSUMPTION_NOTE = ("Win scores are assumptions: 0.54 = share of private-payer denials eventually "
                   "overturned (Premier Inc., 2024); 0.20 = team assumption. Dollar math uses the "
                   "contract rate.")
BENCH_WORDING = ("Simulation on synthetic denials; answer labels fixed before the run. Every order "
                 "files the same number of appeals, so the time ClaimGuard saves is not counted.")

ROUTES = ("APPEAL", "FETCH", "DROP", "MANUAL")
GROUP_LABEL = {"APPEAL": "Ready to appeal", "FETCH": "Need one document",
               "DROP": "Not worth appealing", "MANUAL": "Needs a person"}
STATUS_LABEL = {"APPEAL": "Ready to appeal", "FETCH": "Request drafted",
                "DROP": "Memo ready", "MANUAL": "Needs a person"}
SHOW_ALL = {"APPEAL": "appeals", "FETCH": "requests", "DROP": "memos", "MANUAL": "cases"}
GROUP_PREVIEW = 6

E = lambda value: html.escape("" if value is None else str(value), quote=True)  # noqa: E731


def raw(markup, target=st):
    """Emit trusted HTML built from escaped parts. Strip indentation so markdown keeps it as HTML."""
    target.markdown("".join(line.strip() for line in markup.splitlines()), unsafe_allow_html=True)


def link(**params):
    return "?" + urlencode({k: v for k, v in params.items() if v is not None})


# --------------------------------------------------------------------------
# icons (inline SVG, no outside resources)
# --------------------------------------------------------------------------

def square(kind):
    if kind == "FETCH":
        body = ('<rect x="0.75" y="0.75" width="8.5" height="8.5" rx="2" '
                'style="fill:none;stroke:%s;stroke-width:1.5"></rect>' % NAVY)
    else:
        fill = {"APPEAL": NAVY, "DROP": "#9CA3AF", "MANUAL": "#B42318", "FLAG": "#B42318"}[kind]
        body = '<rect width="10" height="10" rx="2" style="fill:%s"></rect>' % fill
    return '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">%s</svg>' % body


CHECK = ('<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" style="flex:none">'
         '<path d="M2.5 7.5l3 3 6-7" style="fill:none;stroke:%s;stroke-width:2;'
         'stroke-linecap:round;stroke-linejoin:round"></path></svg>' % NAVY)
CROSS = ('<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" style="flex:none">'
         '<path d="M3 3l8 8M11 3l-8 8" style="fill:none;stroke:#9CA3AF;stroke-width:2;'
         'stroke-linecap:round"></path></svg>')
DASH = ('<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" style="flex:none">'
        '<path d="M3 7h8" style="fill:none;stroke:#9CA3AF;stroke-width:2;stroke-linecap:round">'
        '</path></svg>')


# --------------------------------------------------------------------------
# styles
# --------------------------------------------------------------------------

raw("""
<style>
.stApp{background:#FFFFFF;}
.stApp, .stApp *:not([data-testid="stIconMaterial"]):not(code):not(pre){
font-family:system-ui,-apple-system,"Segoe UI",sans-serif !important;}
.stApp{color:#111827;font-size:14px;line-height:1.45;}
header[data-testid="stHeader"],div[data-testid="stToolbar"],
section[data-testid="stSidebar"],div[data-testid="stSidebarCollapsedControl"]{display:none;}
[data-testid="stMain"]{padding-left:220px;box-sizing:border-box;}
.block-container{padding:28px 32px 48px !important;max-width:none !important;}
.stMarkdown p{font-size:14px;}
.cg-rail{position:fixed;top:0;left:0;bottom:0;width:220px;background:#12284C;color:#FFFFFF;
display:flex;flex-direction:column;padding:20px 14px;box-sizing:border-box;z-index:999990;}
.cg-rail *{color:#FFFFFF;}
.cg-brand{display:flex;align-items:center;gap:10px;padding:0 8px 18px;border-bottom:1px solid rgba(255,255,255,.16);}
.cg-logo{flex:none;width:30px;height:30px;border:1.5px solid #FFFFFF;border-radius:4px;display:inline-flex;
align-items:center;justify-content:center;font-size:12px;font-weight:700;}
.cg-brand b{display:block;font-size:16px;}
.cg-brand small{font-size:11.5px;color:rgba(255,255,255,.72) !important;}
.cg-nav{display:flex;flex-direction:column;gap:2px;margin-top:14px;}
.cg-nav a{display:flex;align-items:center;height:44px;padding:0 10px;border-radius:4px;
color:rgba(255,255,255,.8) !important;text-decoration:none !important;}
.cg-nav a:hover{background:rgba(255,255,255,.08);}
.cg-nav a.on{background:rgba(255,255,255,.14);color:#FFFFFF !important;font-weight:600;}
.cg-nav a span{margin-left:auto;font-size:12px;}
.cg-local{margin-top:auto;padding:14px 8px 0;border-top:1px solid rgba(255,255,255,.16);font-size:12px;
line-height:1.7;}
.cg-local div{color:rgba(255,255,255,.8) !important;}
.cg-local div:first-child{color:#FFFFFF !important;font-weight:600;}
@media (max-width:760px){.cg-rail{position:static;width:auto;}[data-testid="stMain"]{padding-left:0;}}
.stMarkdown a{color:#12284C;}
.stMarkdown a:hover{color:#0A1830;}
.stApp .stMarkdown h1.h1,.stApp h1.h1{margin:0 !important;font-size:24px !important;font-weight:700 !important;letter-spacing:-.01em;color:#111827;padding:0 !important;line-height:1.25 !important;scroll-margin:0;}
.stApp .stMarkdown h2.h2,.stApp h2.h2{margin:0 !important;font-size:15px !important;font-weight:700 !important;padding:0 !important;line-height:1.4 !important;color:#111827;}
.stApp .stMarkdown h2.h2b{margin:0 !important;font-size:17px !important;font-weight:700 !important;padding:0 !important;line-height:1.4 !important;}
.stApp h1 a.anchor-link,.stApp h2 a.anchor-link,[data-testid="stHeaderActionElements"]{display:none !important;}
.sub{margin:6px 0 0;color:#4B5563;}
.s{font-size:12.5px;color:#4B5563;}
.urg{color:#B42318;font-weight:600;}
.flagbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;padding:12px 16px;border:1px solid #F1C8C4;
border-radius:4px;margin:4px 0 6px;}
.flagbar a{margin-left:auto;font-weight:600;}
.sum{display:flex;flex-wrap:wrap;border:1px solid #E5E7EB;border-radius:4px;margin:8px 0 12px;}
.sum>div{flex:1 1 180px;padding:16px 20px;border-right:1px solid #E5E7EB;}
.sum>div:last-child{border-right:0;}
.sum .big{margin-top:4px;font-size:30px;font-weight:700;color:#12284C;}
.tabs{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;border-bottom:1px solid #E5E7EB;}
.tab{display:inline-flex;align-items:center;height:40px;padding:0 2px;margin-right:18px;color:#4B5563 !important;
text-decoration:none !important;border-bottom:2px solid transparent;}
.tab.on{color:#12284C !important;font-weight:600;border-bottom-color:#12284C;}
.twrap{overflow-x:auto;}
.t{width:100%;min-width:900px;border-collapse:collapse;font-size:14px;border:0;}
.t th{text-align:left;font-size:12px;font-weight:600;color:#4B5563;padding:10px 12px;border:0;
border-bottom:1px solid #D1D5DB;white-space:nowrap;background:#FFFFFF;}
.t td{padding:12px;border:0;border-bottom:1px solid #EEF0F2;vertical-align:middle;}
.t tr.grp td{padding:18px 12px 8px;font-size:12.5px;font-weight:700;color:#12284C;border-bottom:1px solid #D1D5DB;}
.t .n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;}
.t a{font-weight:600;}
.st{display:inline-flex;align-items:center;gap:7px;white-space:nowrap;}
.btn{display:inline-flex;align-items:center;height:40px;padding:0 14px;border:1px solid #12284C;border-radius:4px;
color:#12284C !important;font-weight:600;text-decoration:none !important;white-space:nowrap;background:#FFFFFF;}
.casenav{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;font-size:13px;}
.casenav .r{margin-left:auto;display:flex;gap:16px;}
.casehead{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px;padding:14px 0 18px;
border-bottom:1px solid #E5E7EB;margin-bottom:16px;}
.casehead>div{flex:1 1 420px;}
.casehead dl{margin:0;display:flex;gap:32px;flex:none;}
.casehead dd{margin:2px 0 0;font-size:20px;font-weight:700;}
.casehead dd.s{margin:0;font-size:12.5px;font-weight:400;}
.pair{display:flex;flex-wrap:wrap;gap:16px;margin-bottom:16px;}
.pair>div{flex:1 1 360px;min-width:0;padding:14px 16px;border:1px solid #E5E7EB;border-radius:4px;}
.pair>div.shows{border-color:#12284C;background:#EEF1F6;}
.pair .lbl{font-size:12.5px;font-weight:700;}
.pair p{margin:6px 0 0;}
.col{border:1px solid #E5E7EB;border-radius:4px;padding:18px 18px 20px;min-width:0;margin-bottom:16px;}
div[data-testid="stVerticalBlockBorderWrapper"]{border-color:#E5E7EB !important;border-radius:4px !important;}
.crit{list-style:none;margin:16px 0 0;padding:0;display:flex;flex-direction:column;gap:18px;}
.crit>li{margin:0;}
.crit .ttl{display:flex;align-items:center;gap:8px;font-weight:600;}
.crit .ttl.off{color:#4B5563;}
.crit .why{margin:2px 0 8px 22px;}
.crit ul{list-style:none;margin:0 0 0 22px;padding:0;display:flex;flex-direction:column;gap:6px;}
.crit ul li{margin:0;}
.q{padding:8px 10px;border-radius:3px;background:#EEF1F6;font-size:13.5px;}
.qa{padding:8px 10px;border-radius:3px;background:#F3F4F6;font-size:13.5px;}
.found{margin-top:18px;padding-top:14px;border-top:1px solid #E5E7EB;}
.found summary{cursor:pointer;font-weight:700;}
.found ol{margin:10px 0 0;padding-left:20px;display:flex;flex-direction:column;gap:6px;font-size:13px;}
.found ol li{margin:0;}
.doc{border-top:1px solid #E5E7EB;padding-top:12px;margin-top:14px;}
.doc .top{display:flex;flex-wrap:wrap;justify-content:space-between;gap:4px 8px;}
.doc .prov{color:#12284C;font-weight:600;}
.doc ol{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-direction:column;gap:2px;}
.sen{display:flex;gap:8px;padding:5px 6px;border-radius:3px;font-size:13px;margin:0;}
.sid{flex:0 0 44px;color:#6B7280;font-size:12px;font-variant-numeric:tabular-nums;padding-top:1px;}
.hi{background:#EEF1F6;}
.con{background:#F3F4F6;}
[id^="s-"]:target{outline:2px solid #12284C;outline-offset:1px;}
.letter{line-height:1.6;}
.letter p{margin:0 0 8px;}
.missing{border:1px solid #12284C;border-radius:4px;padding:12px 14px;margin-top:8px;}
.against{background:#F3F4F6;border-radius:3px;padding:10px 12px;margin-top:8px;}
mark.f{background:#FBEAE8;color:inherit;border-bottom:1.5px solid #B42318;padding:0 1px;}
.flagletter{padding:10px 12px;border:1px solid #F1C8C4;border-radius:3px;white-space:pre-wrap;}
.secflag{border:1px solid #F1C8C4;border-radius:4px;padding:18px 20px;margin-bottom:16px;}
.secflag .st{color:#B42318;font-weight:600;}
.full summary{cursor:pointer;font-weight:600;margin-top:10px;}
.full pre{white-space:pre-wrap;font-size:13px;background:#F9FAFB;padding:10px 12px;border-radius:3px;}
.st-key-decide{position:sticky;bottom:0;z-index:5;background:#FFFFFF;border-top:1px solid #D1D5DB;padding:14px 0 4px;}
.done{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;}
.done a{margin-left:auto;font-weight:700;}
.row{display:flex;justify-content:space-between;align-items:baseline;gap:16px;padding:9px 0;border-top:1px solid #EEF0F2;}
.row dt{color:#4B5563;font-weight:400;}
.row dd{margin:0;font-weight:600;text-align:right;font-variant-numeric:tabular-nums;}
.grid2{margin:10px 0 0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:28px;}
.bars{display:grid;grid-template-columns:minmax(0,190px) minmax(0,1fr);column-gap:14px;margin-top:18px;}
.track{display:flex;align-items:center;gap:10px;border-left:1px solid #9CA3AF;padding:12px 0;min-height:44px;box-sizing:border-box;}
.track .bar{height:20px;border-radius:0 3px 3px 0;}
.name{padding:12px 0;display:flex;flex-direction:column;justify-content:center;}
.tbl{width:100%;border-collapse:collapse;font-size:13px;}
.tbl th{text-align:left;font-weight:600;color:#4B5563;padding:8px 10px;border:0;border-bottom:1px solid #D1D5DB;white-space:nowrap;}
.tbl td{padding:8px 10px;border:0;border-bottom:1px solid #EEF0F2;font-variant-numeric:tabular-nums;}
.split{margin-top:14px;display:flex;gap:2px;height:28px;font-size:12.5px;font-weight:600;}
.split div{display:flex;align-items:center;justify-content:center;}
/* Streamlit widgets in the ClaimGuard style */
div[data-testid="stButton"] button,div[data-testid="stPopover"] button{min-height:44px;border-radius:4px;
font-weight:600;box-shadow:none;}
div[data-testid="stButton"] button[kind="primary"]{background:#12284C;border:0;color:#FFFFFF;}
div[data-testid="stButton"] button[kind="primary"]:hover{background:#0A1830;}
div[data-testid="stButton"] button[kind="primary"] *{color:#FFFFFF !important;}
div[data-testid="stButton"] button[kind="secondary"]{background:#FFFFFF;border:1px solid #12284C;color:#12284C;}
div[data-testid="stButton"] button[kind="secondary"] *{color:#12284C !important;}
div[data-testid="stPopover"] button{background:#FFFFFF;border:1px solid #D1D5DB;color:#111827;font-weight:400;}
div[data-testid="stTextInput"] input{min-height:42px;}
div[data-baseweb="input"]{border-radius:4px;}
button[data-testid^="stBaseButton-segmented_control"]{min-height:36px;}
button[data-testid="stBaseButton-segmented_controlActive"]{background:#12284C !important;border-color:#12284C !important;}
button[data-testid="stBaseButton-segmented_controlActive"] *{color:#FFFFFF !important;}
</style>
""")


# --------------------------------------------------------------------------
# data (cached for 5 s so results appear while a run is going)
# --------------------------------------------------------------------------

def group_key(result):
    """Within a route group: soonest deadline first, then the larger payment."""
    return (ROUTES.index(result["route"]) if result["route"] in ROUTES else 9,
            result.get("days_left", 9999), -(result.get("expected_payment") or 0))


@st.cache_data(ttl=5)
def load_results():
    return sorted(engine.all_results(), key=group_key)


@st.cache_data(ttl=5)
def load_case(claim_id):
    return engine.load_case(claim_id)


@st.cache_data(ttl=5)
def load_out_json(name):
    path = engine.OUT / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


@st.cache_data(ttl=5)
def load_text(rel_path):
    path = engine.ROOT / rel_path
    return path.read_text() if path.exists() else None


@st.cache_data(ttl=5)
def load_audit(limit=300):
    path = engine.OUT / "audit.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


@st.cache_data(ttl=60)
def total_cases():
    return len(engine.all_claim_ids())


def run_state():
    """Is a batch running right now? Read from the audit log: run_start without run_end."""
    for event in reversed(load_audit(2000)):
        if event.get("event") == "run_end":
            return None
        if event.get("event") == "run_start":
            return event
    return None


def money(value):
    return "${:,.0f}".format(value or 0)


def due_date(iso):
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return d.strftime("%b %-d") if d.year == engine.TODAY.year else d.strftime("%b %-d, %Y")


def long_date(iso):
    try:
        return date.fromisoformat(iso).strftime("%b %-d, %Y")
    except (TypeError, ValueError):
        return iso or ""


def days_left(days):
    return "%d day%s left" % (days, "" if days == 1 else "s")


def is_local(url):
    host = urlparse(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")


def label_of(cid):
    return engine.criteria_by_id().get(cid, {}).get("label", cid)


def lower_first(text):
    return text[:1].lower() + text[1:] if text else text


def flagged(result):
    return "injection" in (result.get("flags") or [])


def from_search(result):
    """True when a deciding criterion was proven by a record the payer never saw."""
    by_id = {c["id"]: c for c in result.get("criteria") or []}
    return any(by_id.get(cid, {}).get("source") == "search" for cid in result.get("argument_path") or [])


def status_html(result):
    status = result.get("status")
    if status == "approved":
        return '<span class="st">%s Approved</span>' % CHECK
    if status == "rejected":
        return '<span class="st">%s Rejected</span>' % CROSS
    route = result["route"] if result["route"] in ROUTES else "MANUAL"
    return '<span class="st">%s%s</span>' % (square(route), STATUS_LABEL[route])


# --------------------------------------------------------------------------
# left rail (fixed, same on every page)
# --------------------------------------------------------------------------

params = st.query_params
view = params.get("view", "case" if params.get("case") else "queue")
digest = load_out_json("digest.json") or {}
results_all = load_results()
model = digest.get("model") or next((r.get("model") for r in results_all if r.get("model")), None) \
    or os.getenv("CG_LLM_MODEL") or "not set"
host = urlparse(engine.BASE_URL).netloc or engine.BASE_URL
local = is_local(engine.BASE_URL)

active = "queue" if view == "case" else view
nav = [("queue", "Queue", str(len(results_all))), ("results", "Results", ""),
       ("audit", "Audit log", ""), ("settings", "Settings", "")]
raw("""
<div class="cg-rail">
<div class="cg-brand"><span class="cg-logo">CG</span><div><b>ClaimGuard</b><small>Demo General Hospital</small></div></div>
<nav class="cg-nav" aria-label="Main">%s</nav>
<div class="cg-local"><div>%s</div><div>Model: %s</div><div>%s</div><div>%s</div></div>
</div>
""" % ("".join('<a class="%s" href="%s" target="_self">%s%s</a>'
               % ("on" if key == active else "", link(view=key), text,
                  "<span>%s</span>" % count if count else "") for key, text, count in nav),
       "Local only" if local else "Remote endpoint", E(model), E(host),
       "0 cloud calls" if local else "Check the endpoint"))


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------

def start_run():
    """Kick off `cg.py run --new` in the background. cg.py is the only writer of results."""
    subprocess.Popen([sys.executable, "cg.py", "run", "--new"], cwd=str(engine.ROOT),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def reason_html(r):
    route = r["route"]
    if route == "APPEAL":
        path = r.get("argument_path") or []
        by_id = {c["id"]: c for c in r.get("criteria") or []}
        proven = sum(1 for cid in path if by_id.get(cid, {}).get("status") == engine.MET)
        text = "%d of %d criteria proven" % (proven, len(path))
        if path == ["RED_FLAG"]:
            text += ", red flag route"
        where = "Found in records the payer never saw" if from_search(r) else "In the note the payer saw"
        return '%s<div class="s">%s</div>' % (text, where)
    if route == "FETCH":
        return "Missing: " + E("; ".join(lower_first(label_of(c)) for c in r.get("missing") or []))
    if route == "DROP":
        return "Contradicted: " + E("; ".join(lower_first(label_of(c)) for c in r.get("drop_reasons") or []))
    return "ClaimGuard could not finish" + ('<div class="s">%s</div>' % E(r.get("error")) if r.get("error") else "")


def due_html(r):
    if r.get("expired"):
        return ('<div class="urg">%s</div><div class="urg" style="font-size:12.5px;font-weight:400">expired</div>'
                % E(due_date(r.get("deadline"))))
    days = r.get("days_left", 0)
    if days <= 7:
        return ('<div class="urg">%s</div><div class="urg" style="font-size:12.5px;font-weight:400">%s</div>'
                % (E(due_date(r.get("deadline"))), days_left(days)))
    return '<div>%s</div><div class="s">%s</div>' % (E(due_date(r.get("deadline"))), days_left(days))


def view_queue():
    results = results_all
    running = run_state()
    seconds = [r["timings"]["seconds"] for r in results if r.get("timings")]

    head, act = st.columns([5, 1.2], vertical_alignment="bottom")
    with head:
        if running:
            sub = "Working the queue now: %d of %d cases have results." % (len(results), total_cases())
        elif results:
            sub = ("Watching for new denials. %d worked, each ready about %d seconds after it arrived."
                   % (len(results), round(sum(seconds) / len(seconds)) if seconds else 0))
        else:
            sub = "No denials worked yet."
        raw('<h1 class="h1">Denial queue</h1><p class="sub">%s</p>' % sub)
    with act:
        if st.button("Check inbox now", type="primary", width="stretch", disabled=bool(running),
                     help="Runs python3 cg.py run --new on every case with status new"):
            start_run()
            st.toast("Run started. New results appear here as each case finishes.")

    if not results:
        st.info("No results yet. Run `python3 cg.py run --new` or press Check inbox now.")
        return

    flags = [r for r in results if flagged(r)]
    if flags:
        raw('<div class="flagbar" role="status"><span class="urg">Security flag</span>'
            '<span>%d payer letter%s tried to give the AI instructions. ClaimGuard ignored them and '
            'nothing left this computer.</span><a href="%s" target="_self">See %s</a></div>'
            % (len(flags), "" if len(flags) == 1 else "s", link(view="case", case=flags[0]["claim_id"]),
               E(flags[0]["claim_id"])))

    by_route = {k: [r for r in results if r["route"] == k] for k in ROUTES}
    ready = sum(r["expected_payment"] for r in by_route["APPEAL"])
    soon = [r for r in results if not r.get("expired") and r.get("days_left", 99) <= 7]
    raw("""
    <section class="sum" aria-label="Summary">
    <div><div class="s">Ready to appeal</div><div class="big">%d</div><div class="s">%s in contract payments</div></div>
    <div><div class="s">Due within 7 days</div><div class="big">%d</div>%s</div>
    <div><div class="s">Need one document</div><div class="big">%d</div><div class="s">A request is drafted for each clinic</div></div>
    <div><div class="s">Not worth appealing</div><div class="big">%d</div><div class="s">The chart contradicts the payer's rule</div></div>
    </section>
    """ % (len(by_route["APPEAL"]), money(ready), len(soon),
           '<div class="urg" style="font-size:12.5px">Start with these</div>' if soon
           else '<div class="s">Nothing urgent</div>',
           len(by_route["FETCH"]), len(by_route["DROP"])))

    wanted = params.get("f")
    wanted = wanted if wanted in ROUTES else None
    tabs = [("All (%d)" % len(results), None)] + [
        ("%s (%d)" % (GROUP_LABEL[k], len(by_route[k])), k) for k in ROUTES]
    raw('<div class="tabs"><nav aria-label="Filter">%s</nav><span class="s">Sorted by deadline, '
        'then payment</span></div>'
        % "".join('<a class="tab%s" href="%s" target="_self">%s</a>'
                  % (" on" if key == wanted else "", link(view="queue", f=key), text) for text, key in tabs))

    rows = []
    for route in ([wanted] if wanted else ROUTES):
        group = by_route[route]
        if not group:
            continue
        rows.append('<tr class="grp"><td colspan="8">%s · %d</td></tr>' % (GROUP_LABEL[route], len(group)))
        shown = group if wanted else group[:GROUP_PREVIEW]
        for i, r in enumerate(shown, start=1):
            case = load_case(r["claim_id"])
            name = case["patient"]["name"]
            rows.append(
                '<tr><td class="n s">%d</td><td><div style="font-weight:600">%s</div><div class="s">%s%s</div></td>'
                '<td>%s</td><td>%s</td><td class="n">%s</td><td>%s</td><td>%s</td>'
                '<td><a class="btn" href="%s" target="_self" aria-label="Review %s, claim %s">Review</a></td></tr>'
                % (i, E(name), E(r["claim_id"]), ' · <span class="urg">flagged</span>' if flagged(r) else "",
                   E(r["payer"]), reason_html(r), money(r["expected_payment"]), due_html(r), status_html(r),
                   link(view="case", case=r["claim_id"]), E(name), E(r["claim_id"])))
        if not wanted and len(group) > GROUP_PREVIEW:
            rows.append('<tr><td colspan="8"><a href="%s" target="_self">Show all %d %s</a></td></tr>'
                        % (link(view="queue", f=route), len(group), SHOW_ALL[route]))
    raw('<div class="twrap"><table class="t"><thead><tr><th class="n">#</th><th>Patient</th><th>Payer</th>'
        '<th>Reason</th><th class="n">Payment</th><th>Appeal due</th><th>Status</th><th aria-label="Action">'
        '</th></tr></thead><tbody>%s</tbody></table></div>' % "".join(rows))
    raw('<p class="s" style="margin:14px 0 0">Every approval, rejection and override is written to the '
        'audit log with your name and the time.</p>')
    st.caption(ASSUMPTION_NOTE + " " + FOOTER)


# --------------------------------------------------------------------------
# case
# --------------------------------------------------------------------------

def quote_li(index, sid, cls):
    record = index.get(sid)
    if not record:
        return ""
    return '<li class="%s">“%s” <a href="#s-%s">%s</a></li>' % (cls, E(record["text"]), E(sid), E(sid))


def criteria_section(result, policy, case):
    index = engine.sentence_index(case)
    by_id = {c["id"]: c for c in result.get("criteria") or []}
    path = result.get("argument_path") or []
    appeal = result["route"] == "APPEAL"
    items = []
    for cid in engine.policy_criteria_ids(policy):
        item = by_id.get(cid, {"status": engine.NOT_FOUND, "evidence": []})
        evidence = item.get("evidence") or []
        needed = not appeal or cid in path
        if item["status"] == engine.MET:
            icon, cls = CHECK, "q"
            why = "Proven by %d sentence%s" % (len(evidence), "" if len(evidence) == 1 else "s")
            if not needed:
                why += ", not needed: the other route is proven"
        elif item["status"] == engine.NOT_MET:
            icon, cls = CROSS, "qa"
            why = "Contradicted, and not needed: the other route is proven" if not needed else "Contradicted"
        else:
            icon, cls = DASH, "qa"
            why = "Not found in the chart" + (", and not needed" if not needed else "")
        quotes = "".join(quote_li(index, sid, cls) for sid in evidence)
        items.append('<li><div class="ttl%s">%s%s</div><div class="s why">%s</div>%s</li>'
                     % ("" if needed and item["status"] == engine.MET else " off", icon,
                        E(label_of(cid)), E(why), "<ul>%s</ul>" % quotes if quotes else ""))

    if appeal:
        summary = "Met through %s." % " plus ".join(lower_first(label_of(c)) for c in path)
    elif result["route"] == "FETCH":
        summary = "No route is proven yet; one record would complete it."
    elif result["route"] == "DROP":
        summary = "The chart contradicts every route in the policy."
    else:
        summary = "ClaimGuard could not finish this case."

    submitted = next((d for d in case["docs"] if d["doc_id"] == case.get("submitted_doc_id")), None)
    cited = sum(len(c.get("evidence") or []) for c in result.get("criteria") or [])
    dropped = len(result.get("invalid_citations") or [])
    steps = []
    if submitted:
        steps.append("Read the %s the payer saw: %d sentences." % (lower_first(submitted["title"]),
                                                                 len(submitted["sentences"])))
    for step in result.get("search_trail") or []:
        n = len(step.get("results") or [])
        steps.append("Searched the rest of the chart for “%s”: %d sentence%s back."
                     % (step.get("query"), n, "" if n == 1 else "s"))
    steps.append("Checked all %d citations against the chart: %d kept, %d dropped."
                 % (cited + dropped, cited, dropped))
    if result.get("downgrades"):
        steps.append("The checker downgraded %d status%s." % (result["downgrades"],
                                                            "" if result["downgrades"] == 1 else "es"))
    steps.append({"APPEAL": "Code applied the payer's rule: one route fully proven, so appeal.",
                  "FETCH": "Code applied the payer's rule: no route proven or contradicted, so ask for the missing record.",
                  "DROP": "Code applied the payer's rule: every route contradicted, so no appeal.",
                  }.get(result["route"], "Stopped: %s" % (result.get("error") or "unknown error")))
    if appeal:
        quotes = sum(len(by_id.get(c, {}).get("evidence") or []) for c in path)
        steps.append("Drafted the letter; code inserted all %d quotes word for word." % quotes
                     if not result.get("quote_violations")
                     else "Drafted the letter; %d quote check violation(s)." % result["quote_violations"])
    timing = result.get("timings") or {}
    raw("""
    <section class="col"><h2 class="h2">Policy criteria</h2>
    <p class="s" style="margin:4px 0 0">%s %s. %s</p>
    <ol class="crit">%s</ol>
    <details class="found" open><summary>How ClaimGuard found it</summary><ol>%s</ol>
    <p class="s" style="margin:10px 0 0">%s model calls · %s seconds · on this computer</p></details>
    </section>
    """ % (E(policy["payer"]), E(policy["policy_id"]), E(summary), "".join(items),
           "".join("<li>%s</li>" % E(s) for s in steps), E(result.get("llm_calls", "—")),
           E(timing.get("seconds", "—"))))


LETTER_CSS = ("<style>body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;font-size:14px;"
              "line-height:1.6;max-width:none;margin:0;padding:0 2px;color:#111827;background:#fff}"
              "h3{font-size:14px;margin:16px 0 0}mark{background:#EEF1F6;color:inherit;"
              "border-bottom:1.5px solid #12284C;padding:0 1px}mark a{color:inherit;text-decoration:none;"
              "border:0}.src{font-size:11px;color:#4B5563}.hdr{font-size:14px}.appendix,.warn{display:none}"
              ".sig{font-style:normal;font-size:12.5px;color:#4B5563}</style>")


def full_text(text, label):
    if text:
        return '<details class="full"><summary>%s</summary><pre>%s</pre></details>' % (label, E(text))
    return ""


def output_section(result, case):
    outputs = result.get("outputs") or {}
    route = result["route"]
    index = engine.sentence_index(case)
    if route == "APPEAL":
        with st.container(border=True):
            raw('<div style="display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;'
                'gap:4px 12px"><h2 class="h2">Appeal letter, %s</h2><span class="s">Quotes copied from the '
                'chart by code</span></div>' % E(result.get("status") or "draft"))
            doc = load_text(outputs["letter_html"]) if outputs.get("letter_html") else None
            if doc:
                # engine.render_letter_html escapes every case string; only our own CSS is added
                components.html(doc.replace("</head>", LETTER_CSS + "</head>", 1), height=640, scrolling=True)
            else:
                raw('<p class="s">No letter file yet.</p>')
        return

    if route == "FETCH":
        text = load_text(outputs["request_txt"]) if outputs.get("request_txt") else None
        to = next((line[3:].strip() for line in (text or "").splitlines() if line.startswith("To:")), None)
        criteria = engine.criteria_by_id()
        searched = {}
        for step in result.get("search_trail") or []:
            searched.setdefault(step.get("criterion"), []).append(step.get("query"))
        boxes = "".join(
            '<div class="missing"><div style="font-weight:700;color:#12284C">Missing: %s</div>'
            '<div class="s" style="margin-top:4px">%s%s</div></div>'
            % (E(lower_first(label_of(cid))), E(criteria.get(cid, {}).get("definition", "")),
               E(" Searched the chart for “%s”: no matching sentence." % "”, “".join(searched[cid]))
                 if searched.get(cid) else "")
            for cid in result.get("missing") or [])
        raw("""
        <section class="col"><div style="display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:6px 12px">
        <h2 class="h2">Document request</h2><span class="st" style="font-weight:600;color:#12284C">%sNeeds one document</span></div>
        <div class="letter" style="border-top:1px solid #E5E7EB;padding-top:12px;margin-top:12px">
        <p>%sFrom: Demo General Hospital, Denial Management Office<br>Re: Claim %s, %s, %s</p>
        <p>We are preparing an appeal for this claim and need one more record to meet the payer's criteria.</p>%s</div>%s
        </section>
        """ % (square("FETCH"), "To: %s<br>" % E(to) if to else "", E(case["claim_id"]), E(case["procedure"]),
               E(long_date(case["date_of_service"])), boxes, full_text(text, "Show the full request")))
        return

    if route == "DROP":
        text = load_text(outputs["memo_txt"]) if outputs.get("memo_txt") else None
        by_id = {c["id"]: c for c in result.get("criteria") or []}
        boxes = []
        for cid in result.get("drop_reasons") or []:
            quotes = "".join('<div style="margin-top:4px">“%s” <span class="s">%s, %s, %s</span></div>'
                             % (E(index[sid]["text"]), E(sid), E(lower_first(index[sid]["title"])),
                                E(long_date(index[sid]["date"])))
                             for sid in by_id.get(cid, {}).get("evidence") or [] if sid in index)
            boxes.append('<div class="against"><div style="font-weight:700">%s</div>%s</div>'
                         % (E(label_of(cid)), quotes or '<div class="s">No sentence recorded.</div>'))
        raw("""
        <section class="col"><div style="display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:6px 12px">
        <h2 class="h2">No-appeal memo</h2><span class="st" style="font-weight:600;color:#4B5563">%sNot worth appealing</span></div>
        <div class="letter" style="border-top:1px solid #E5E7EB;padding-top:12px;margin-top:12px">
        <p>After reviewing the medical record, we do not recommend appealing this denial. The record contradicts
        every route in the payer's policy.</p>%s</div>%s</section>
        """ % (square("DROP"), "".join(boxes), full_text(text, "Show the full memo")))
        return

    text = load_text(outputs["manual_txt"]) if outputs.get("manual_txt") else None
    raw('<section class="col"><h2 class="h2">Needs a person</h2><div class="against">%s</div>%s</section>'
        % (E(result.get("error") or "ClaimGuard could not finish this case."), full_text(text, "Show the note")))


def chart_section(case, result):
    cited = {}
    for c in result.get("criteria") or []:
        for sid in c.get("evidence") or []:
            cited[sid] = "hi" if c["status"] == engine.MET else "con"
    with st.container(border=True):
        raw('<h2 class="h2">Patient chart</h2>')
        mode = st.segmented_control("Chart", ["All documents", "Cited only"], default="All documents",
                                    key="chart_mode", label_visibility="collapsed", width="stretch")
        docs = []
        for doc in case["docs"]:
            items = []
            for n, text in enumerate(doc["sentences"], start=1):
                sid = "%s-%d" % (doc["doc_id"], n)
                if mode == "Cited only" and sid not in cited:
                    continue
                items.append('<li id="s-%s" class="sen %s"><span class="sid">%s</span><span>%s</span></li>'
                             % (E(sid), cited.get(sid, ""), E(sid), E(text)))
            if not items:
                continue
            uses = any(sid.startswith(doc["doc_id"] + "-") for sid in cited)
            if doc["doc_id"] == case.get("submitted_doc_id"):
                prov = '<div class="s">Sent to payer%s</div>' % (" · " + E(doc["author"]) if doc.get("author") else "")
            elif uses:
                prov = '<div class="s prov">Found by search, not sent to payer</div>'
            else:
                prov = '<div class="s">Not sent to payer</div>'
            docs.append('<div class="doc"><div class="top"><span style="font-weight:600">%s</span>'
                        '<span class="s">%s</span></div>%s<ol>%s</ol></div>'
                        % (E(doc["title"]), E(long_date(doc["date"])), prov, "".join(items)))
        raw("".join(docs) or '<p class="s">No cited sentences.</p>')


def mark_injection(text):
    """Escape the payer letter, then highlight the phrases that look like instructions."""
    escaped = E(text)
    escaped = re.sub(r"https?://[^\s<]+", lambda m: '<mark class="f">%s</mark>' % m.group(0), escaped)
    pattern = "|".join(re.escape(E(p)) for p in engine.INJECTION_PATTERNS if not p.startswith("http"))
    if pattern:
        escaped = re.sub("(%s)" % pattern, r'<mark class="f">\1</mark>', escaped, flags=re.I)
    return escaped


def view_case():
    order = [r["claim_id"] for r in results_all]
    claim_id = params.get("case") or (order[0] if order else None)
    if not claim_id:
        st.info("No results yet.")
        return
    result = engine.load_result(claim_id)  # uncached, so a decision shows at once
    if result is None:
        st.warning("No result for %s yet." % claim_id)
        return
    case = load_case(claim_id)
    policy = engine.policy_by_id(case["policy_id"])
    pos = order.index(claim_id) if claim_id in order else -1
    prev_id = order[pos - 1] if pos > 0 else None
    next_id = order[pos + 1] if 0 <= pos < len(order) - 1 else None
    group = [r["claim_id"] for r in results_all if r["route"] == result["route"]]
    where = ("%s, case %d of %d" % (GROUP_LABEL.get(result["route"], result["route"]),
                                     group.index(claim_id) + 1, len(group)) if claim_id in group else "")

    def nav_link(text, target):
        return ('<a href="%s" target="_self">%s</a>' % (link(view="case", case=target), text)
                if target else '<span class="s">%s</span>' % text)

    raw('<nav class="casenav" aria-label="Case navigation"><a href="%s" target="_self" style="font-weight:600">'
        'Back to queue</a><span class="s">%s</span><span class="r">%s%s</span></nav>'
        % (link(view="queue"), E(where), nav_link("Previous", prev_id), nav_link("Next", next_id)))

    p = case["patient"]
    days = result.get("days_left", 0)
    late = result.get("expired") or days <= 7
    raw("""
    <div class="casehead"><div style="min-width:0">
    <div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px"><h1 class="h1">%s</h1>
    <span style="font-weight:600;color:#12284C">%s</span></div>
    <p class="sub">%s · %s years old · Member %s · %s · %s (CPT %s) · Service %s</p></div>
    <dl><div><dt class="s">Contract payment</dt><dd>%s</dd></div>
    <div><dt class="s">Appeal due</dt><dd>%s</dd><dd class="s"%s>%s</dd></div></dl></div>
    """ % (E(p["name"]), status_html(result), E(claim_id), E(p["age"]), E(p["member_id"]), E(case["payer"]),
           E(case["procedure"]), E(case["cpt"]), E(long_date(case["date_of_service"])),
           money(result["expected_payment"]), E(long_date(result.get("deadline"))),
           ' style="color:#B42318;font-weight:600"' if late else "",
           "expired" if result.get("expired") else days_left(days)))

    rows = engine.evidence_for_path(case, result.get("criteria") or [], result.get("argument_path") or [])
    first = next((row for cid in result.get("argument_path") or [] for row in rows.get(cid, [])), None)
    if first:
        doc_id = first["id"].rsplit("-", 1)[0]
        shows = ('<div class="shows"><div class="lbl" style="color:#12284C">Chart shows</div><p>“%s”</p>'
                 '<p class="s" style="margin:4px 0 0">%s, %s · <a href="#s-%s">%s</a>%s</p></div>'
                 % (E(first["text"]), E(first["title"]), E(long_date(first["date"])), E(first["id"]),
                    E(first["id"]), " · not sent to the payer" if doc_id != case.get("submitted_doc_id") else ""))
    elif result["route"] == "FETCH":
        shows = ('<div class="shows"><div class="lbl" style="color:#12284C">Chart is missing</div><p>%s</p></div>'
                 % E("; ".join(label_of(c) for c in result.get("missing") or [])))
    elif result["route"] == "DROP":
        shows = ('<div class="shows"><div class="lbl" style="color:#12284C">Chart contradicts</div><p>%s</p></div>'
                 % E("; ".join(label_of(c) for c in result.get("drop_reasons") or [])))
    else:
        shows = '<div><div class="lbl">ClaimGuard stopped</div><p>%s</p></div>' % E(result.get("error") or "")
    raw('<div class="pair"><div><div class="lbl">Payer says</div><p>%s</p></div>%s</div>'
        % (E(case["denial_reason"]), shows))

    if flagged(result):
        raw('<section class="secflag"><div style="display:flex;flex-wrap:wrap;align-items:baseline;'
            'justify-content:space-between;gap:6px 12px"><h2 class="h2">Security flag</h2><span class="st">%s'
            'Payer letter flagged</span></div><p style="margin:10px 0 8px">This payer letter tries to give the AI '
            'instructions. ClaimGuard ignored them: payer letters are never used as evidence, and nothing left '
            'this computer. Read the flagged text before you approve.</p><div class="flagletter">%s</div></section>'
            % (square("FLAG"), mark_injection(case.get("payer_letter", ""))))
    else:
        with st.expander("Payer letter (shown as plain text, never used as evidence)"):
            st.text(case.get("payer_letter", ""))

    c1, c2, c3 = st.columns([1, 1.6, 1])
    with c1:
        criteria_section(result, policy, case)
    with c2:
        output_section(result, case)
    with c3:
        chart_section(case, result)

    decision_bar(result, next_id)
    st.caption(FOOTER)


PRIMARY = {"APPEAL": "Approve letter", "FETCH": "Mark request sent", "DROP": "Agree and close",
           "MANUAL": "Mark handled"}
REJECT = {"APPEAL": "Reject", "FETCH": "Reject", "DROP": "Override", "MANUAL": "Reject"}


def decision_bar(result, next_id):
    claim_id = result["claim_id"]
    with st.container(key="decide"):
        if result.get("status") in ("approved", "rejected"):
            approved = result["status"] == "approved"
            who = result.get("approved_by") if approved else result.get("rejected_by")
            when = result.get("approved_at") if approved else result.get("rejected_at")
            extra = "" if approved else " Reason: %s." % (result.get("reject_reason") or "none given")
            raw('<div class="done" role="status">%s<span style="font-weight:700">%s by %s at %s.</span>'
                '<span class="s">Written to the audit log.%s Nothing is sent to the payer from here.</span>%s</div>'
                % (CHECK if approved else CROSS, "Approved" if approved else "Rejected", E(who), E(when), E(extra),
                   '<a href="%s" target="_self">Next case</a>' % link(view="case", case=next_id) if next_id else ""))
            return
        draft = result.get("status") == "draft"
        route = result["route"] if result["route"] in ROUTES else "MANUAL"
        who, b1, b2, b3, note = st.columns([1.5, 1.1, 1.3, 0.8, 1.8], vertical_alignment="bottom")
        name = who.text_input("Reviewed by", key="reviewer", placeholder="Your name")

        def decide(decision, reason="", go_next=False):
            if not name.strip():
                st.error("Enter your name first.")
                return
            try:
                engine.approve(claim_id, name, decision=decision, reason=reason)
            except engine.ApprovalError as exc:
                st.error(str(exc))
                return
            st.cache_data.clear()
            if go_next and next_id:
                st.query_params["case"] = next_id
            st.rerun()

        if b1.button(PRIMARY[route], type="primary", disabled=not draft, width="stretch"):
            decide("approved")
        if b2.button("Approve and open next", type="secondary", disabled=not draft or not next_id,
                     width="stretch"):
            decide("approved", go_next=True)
        with b3.popover(REJECT[route], disabled=not draft, width="stretch"):
            reason = st.text_input("Reason", key="reason_%s" % claim_id)
            if st.button("Confirm", key="confirm_reject", type="primary"):
                decide("rejected", reason=reason)
        raw('<p class="s" style="margin:0 0 10px">Your name and the time go to the audit log. Nothing is sent '
            'to the payer from here.</p>', note)


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

def bench_strategies(bench, k=None):
    """Normalise out/benchmark.json into [(name, dollars, filed, won, upheld)] for one K."""
    if not bench:
        return [], None, None
    runs = bench.get("runs") or bench.get("by_k")
    if isinstance(runs, dict):
        runs = [dict(v, k=int(key)) if str(key).isdigit() else v for key, v in runs.items()]
    block = bench
    if isinstance(runs, list) and runs:
        block = next((r for r in runs if k is None or r.get("k") == k), runs[0])
    strategies = block.get("strategies") or block.get("results") or []
    if isinstance(strategies, dict):
        strategies = [dict(v, name=name) for name, v in strategies.items()]
    rows = []
    for s in strategies:
        name = s.get("name") or s.get("strategy") or "?"
        dollars = s.get("dollars_recovered", s.get("dollars", 0)) or 0
        won = s.get("won", s.get("appeals_won"))
        filed = s.get("filed", s.get("appeals_filed"))
        upheld = s.get("wasted", s.get("wasted_appeals"))
        if upheld is None and won is not None and filed is not None:
            upheld = filed - won
        rows.append((name, dollars, filed, won, upheld))
    return rows, block.get("k", bench.get("k")), block.get("n", bench.get("n"))


def bench_ks(bench):
    runs = (bench or {}).get("runs") or (bench or {}).get("by_k")
    if isinstance(runs, dict):
        return sorted(int(k) for k in runs if str(k).isdigit())
    if isinstance(runs, list):
        return [r["k"] for r in runs if "k" in r]
    return [bench["k"]] if bench and "k" in bench else []


def dash(value):
    return "—" if value is None else str(value)


def view_results():
    results = results_all
    bench = load_out_json("benchmark.json")
    raw('<div style="padding-bottom:18px;border-bottom:1px solid #E5E7EB;margin-bottom:16px">'
        '<h1 class="h1">Results</h1><p class="sub">Today, %s. %d synthetic denials, all processed on this '
        'computer.</p></div>' % (engine.TODAY.strftime("%b %-d, %Y"), len(results)))

    left, right = st.columns([2, 1])
    with left, st.container(border=True):
        ks = bench_ks(bench)
        h, sel = st.columns([3, 1.3], vertical_alignment="top")
        k = (sel.segmented_control("Appeals the team can file", ks, default=ks[0], width="stretch",
                                   format_func=lambda v: "%d appeals" % v, label_visibility="collapsed")
             if len(ks) > 1 else (ks[0] if ks else None))
        rows, k, n = bench_strategies(bench, k)
        raw('<h2 class="h2b">Same %sappeals, more money back</h2>'
            '<p class="s" style="margin:4px 0 0">Dollars recovered when the team files %s appeals, by the order '
            'it works the queue</p>' % ("%d " % k if k else "", k or "the same number of"), h)
        if rows:
            top = max(r[1] for r in rows) or 1
            bars = []
            for name, dollars, filed, won, upheld in rows:
                me = "claimguard" in name.lower()
                style = "font-weight:700;color:#12284C" if me else "font-weight:600"
                bars.append('<div class="name"><span style="%s">%s</span><span class="s">%s won, %s upheld</span>'
                            '</div><div class="track"><div class="bar" style="width:%.1f%%;background:%s"></div>'
                            '<span style="%s">%s</span></div>'
                            % (style, E(name), dash(won), dash(upheld), max(2, 74 * dollars / top),
                               NAVY if me else "#9CA3AF", style, money(dollars)))
            table = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                            % (E(name), dash(filed), dash(won), dash(upheld), money(dollars))
                            for name, dollars, filed, won, upheld in rows)
            raw('<div class="bars">%s</div><details style="margin-top:14px"><summary style="cursor:pointer;'
                'font-weight:600">Show the numbers as a table</summary><div class="twrap" style="margin-top:10px">'
                '<table class="tbl"><thead><tr><th>Order</th><th>Appeals filed</th><th>Won</th><th>Upheld</th>'
                '<th>Dollars recovered</th></tr></thead><tbody>%s</tbody></table></div></details>'
                % ("".join(bars), table))
            raw('<p class="s" style="margin:14px 0 0;padding-top:12px;border-top:1px solid #E5E7EB">%s</p>'
                % E(bench.get("wording") or BENCH_WORDING))
        else:
            raw('<p class="s" style="margin:18px 0 0;padding:14px 16px;border:1px solid #E5E7EB;border-radius:4px">'
                'No benchmark yet. Run <code>python3 cg.py bench --k 15</code> after the batch.</p>')

    with right, st.container(border=True):
        route_ok = sum(1 for r in results if r["route"] == (load_case(r["claim_id"]).get("gold") or {}).get("route"))
        gold_inj = [r for r in results if (load_case(r["claim_id"]).get("gold") or {}).get("injection")]
        caught = sum(1 for r in gold_inj if flagged(r))
        appeals = [r for r in results if r["route"] == "APPEAL"]
        trust = [("Correct route", "%d of %d" % (route_ok, len(results))),
                 ("Quotes not found in the chart", sum(r.get("quote_violations", 0) for r in results)),
                 ("Citations the checker dropped", sum(len(r.get("invalid_citations") or []) for r in results)),
                 ("Injected instructions caught", "%d of %d" % (caught, len(gold_inj))),
                 ("Wins needing records the payer never saw",
                  "%d of %d" % (sum(1 for r in appeals if from_search(r)), len(appeals)))]
        raw('<h2 class="h2">Can you trust it?</h2><dl style="margin:10px 0 0">%s</dl>'
            '<p class="s" style="margin:10px 0 0">Scored against answer labels fixed before the run.</p>'
            % "".join('<div class="row"><dt>%s</dt><dd>%s</dd></div>' % (E(a), E(b)) for a, b in trust))

    left, right = st.columns([1, 2])
    with left, st.container(border=True):
        counts = [(k, sum(1 for r in results if r["route"] == k)) for k in ROUTES]
        counts = [(k, v) for k, v in counts if v]
        style = {"APPEAL": "background:#12284C;color:#FFFFFF",
                 "FETCH": "background:#FFFFFF;border:1.5px solid #12284C;box-sizing:border-box;color:#12284C",
                 "DROP": "background:#9CA3AF;color:#111827", "MANUAL": "background:#B42318;color:#FFFFFF"}
        segs = "".join('<div style="flex:%d 1 0;%s">%d</div>' % (v, style[k], v) for k, v in counts)
        legend = ", ".join(lower_first(GROUP_LABEL[k]) if i else GROUP_LABEL[k] for i, (k, _) in enumerate(counts))
        raw('<h2 class="h2">Where the %d denials went</h2><div class="split" role="img">%s</div>'
            '<p class="s" style="margin:10px 0 0">%s</p>' % (len(results), segs, legend))

    with right, st.container(border=True):
        seconds = [r["timings"]["seconds"] for r in results if r.get("timings")]
        completion = sum((r.get("tokens") or {}).get("completion", 0) for r in results)
        wall = digest.get("batch_wall_seconds") or sum(seconds)
        kv = [("Model", model), ("Seconds per case", "%.1f" % (sum(seconds) / len(seconds)) if seconds else "—"),
              ("Hardware", "%s %s" % (platform.system(), platform.machine())),
              ("Tokens per second", "%.1f" % (completion / wall) if wall else "—"),
              ("Cloud calls", "0" if local else "endpoint is not local"),
              ("Cases with results", "%d of %d" % (len(results), total_cases()))]
        raw('<h2 class="h2">Runs on this computer</h2><dl class="grid2">%s</dl>'
            % "".join('<div class="row"><dt>%s</dt><dd>%s</dd></div>' % (E(a), E(b)) for a, b in kv))
    st.caption(FOOTER)


# --------------------------------------------------------------------------
# audit log and settings
# --------------------------------------------------------------------------

def view_audit():
    raw('<div style="padding-bottom:18px;border-bottom:1px solid #E5E7EB;margin-bottom:16px">'
        '<h1 class="h1">Audit log</h1><p class="sub">Last 300 events from out/audit.jsonl. Append-only; prompts '
        'and replies are stored as SHA-256 hashes.</p></div>')
    rows = "".join("<tr><td class='s'>%s</td><td style='font-weight:600'>%s</td><td>%s</td><td class='s'>%s</td>"
                   "<td class='s' style='word-break:break-all'>%s</td></tr>"
                   % (E(e.get("ts")), E(e.get("event")), E(e.get("claim_id") or ""), E(e.get("run_id")),
                      E(json.dumps(e.get("details") or {})[:300])) for e in reversed(load_audit()))
    raw('<div class="twrap"><table class="t"><thead><tr><th>Time</th><th>Event</th><th>Claim</th><th>Run</th>'
        '<th>Details</th></tr></thead><tbody>%s</tbody></table></div>' % rows)
    st.caption(FOOTER)


def view_settings():
    raw('<div style="padding-bottom:18px;border-bottom:1px solid #E5E7EB;margin-bottom:16px">'
        '<h1 class="h1">Settings</h1><p class="sub">Read from the environment and .env. Change them there and '
        'restart.</p></div>')
    kv = [("CG_LLM_BASE_URL", engine.BASE_URL), ("CG_LLM_MODEL", os.getenv("CG_LLM_MODEL", "") or model),
          ("CG_WORKERS", engine.WORKERS), ("CG_MAX_TOKENS", engine.MAX_TOKENS),
          ("CG_TODAY", engine.TODAY.isoformat())] + [("Assumption: %s" % k, v) for k, v in engine.ASSUMPTIONS.items()]
    raw('<dl style="max-width:720px;margin:0">%s</dl>'
        % "".join('<div class="row"><dt>%s</dt><dd>%s</dd></div>' % (E(a), E(b)) for a, b in kv))
    st.caption(ASSUMPTION_NOTE + " " + FOOTER)


{"queue": view_queue, "case": view_case, "results": view_results,
 "audit": view_audit, "settings": view_settings}.get(view, view_queue)()
