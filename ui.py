"""Shared UI helpers: theme CSS and small HTML components.

Colour-blind safety: the palette is the Okabe-Ito set (distinguishable under
protanopia, deuteranopia and tritanopia). Meaning is NEVER carried by colour
alone: every status badge also has an icon glyph and a text label.

Any dynamic value rendered through HTML is escaped to avoid injecting
untrusted document or AI content into the page.
"""
from __future__ import annotations

import re
from html import escape
from typing import Dict

import streamlit as st

# Populated by app.py with the st.Page objects so pages can link to each other.
PAGES: Dict[str, "st.Page"] = {}

# Okabe-Ito colour-blind-safe palette
BLUE, SKY, GREEN = "#0072B2", "#56B4E9", "#009E73"
ORANGE, VERMILLION, PURPLE = "#E69F00", "#D55E00", "#CC79A7"
CHART_PALETTE = [BLUE, ORANGE, GREEN, PURPLE, SKY, VERMILLION, "#7A7A7A", "#F0E442"]

CSS = """
<style>
:root {
  --blue:#0072B2; --sky:#56B4E9; --green:#009E73; --orange:#E69F00; --verm:#D55E00; --purple:#CC79A7;
  --ink:#0f1f33; --muted:#586577; --line:#dde5ee; --card:#ffffff; --wash:#f4f8fc;
}
html, body, [class*="css"] { font-family: "Segoe UI", system-ui, -apple-system, sans-serif; }
.block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1180px; }
h1, h2, h3, h4 { color: var(--ink); letter-spacing: -0.01em; }
h2, h3 { font-family: Georgia, "Times New Roman", serif; font-weight: 600; }

/* Sidebar */
section[data-testid="stSidebar"] { background: linear-gradient(180deg,#eef5fb 0%,#f8fbfe 100%); border-right:1px solid var(--line); }
.n2a-brand { display:flex; align-items:center; gap:.6rem; margin:.3rem 0 .2rem 0; }
.n2a-logo { width:34px; height:34px; border-radius:9px; background:linear-gradient(135deg,var(--blue),var(--sky));
  color:#fff; font-weight:700; display:flex; align-items:center; justify-content:center; font-size:1.05rem; }
.n2a-brand-name { font-family:Georgia,serif; font-size:1.2rem; font-weight:700; color:var(--ink); line-height:1.1; }
.n2a-brand-sub { font-size:.75rem; color:var(--muted); margin:.1rem 0 1rem 0; }

/* Headers + hero */
.n2a-header h1 { margin:0 0 .15rem 0; font-family:Georgia,serif; font-size:2rem; font-weight:600; }
.n2a-header p { margin:0 0 1.2rem 0; color:var(--muted); font-size:1rem; }
.n2a-header .rule { width:56px; height:4px; border-radius:4px; margin:.5rem 0 .6rem 0;
  background:linear-gradient(90deg,var(--blue),var(--orange)); }
.n2a-hero { background:linear-gradient(120deg,#0b3c5d 0%,#0072B2 70%,#2a8fc7 100%); color:#fff; border-radius:16px;
  padding:1.6rem 1.8rem; margin-bottom:1.2rem; box-shadow:0 6px 20px rgba(0,114,178,.18); }
.n2a-hero h1 { color:#fff; font-family:Georgia,serif; font-size:1.9rem; margin:0 0 .3rem 0; font-weight:600; }
.n2a-hero p { margin:0; color:#e6f1f8; font-size:1.02rem; }

/* Metric cards */
.n2a-metric { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:1rem 1.1rem;
  box-shadow:0 1px 3px rgba(15,31,51,.06); display:flex; gap:.9rem; align-items:center; }
.n2a-metric .ico { flex:0 0 46px; height:46px; border-radius:12px; display:flex; align-items:center; justify-content:center;
  font-size:1.35rem; font-weight:700; }
.n2a-metric .label { font-size:.74rem; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); font-weight:600; }
.n2a-metric .value { font-size:2rem; font-weight:700; color:var(--ink); line-height:1.15; }
.n2a-metric .hint { font-size:.8rem; color:var(--muted); }
.n2a-metric.blue .ico { background:#e1f0f9; color:#00507a; }
.n2a-metric.ok .ico { background:#dff4ec; color:#006b4e; }
.n2a-metric.warn .ico { background:#fdf0d0; color:#7a5000; }
.n2a-metric.danger .ico { background:#fae3d4; color:#8a3900; }

/* Badges: tinted background + dark text + icon (never colour alone) */
.n2a-badge { display:inline-flex; align-items:center; gap:.3rem; padding:.14rem .6rem; border-radius:999px;
  font-size:.74rem; font-weight:600; border:1px solid transparent; margin:0 .3rem .2rem 0; white-space:nowrap; }
.n2a-badge.blue { background:#e1f0f9; color:#00507a; border-color:#9ccbe6; }
.n2a-badge.green { background:#dff4ec; color:#006b4e; border-color:#8fd6bd; }
.n2a-badge.orange { background:#fdf0d0; color:#7a5000; border-color:#efcf80; }
.n2a-badge.verm { background:#fae3d4; color:#8a3900; border-color:#eeb08c; }
.n2a-badge.purple { background:#f7e6f0; color:#702a53; border-color:#e3b3cf; }
.n2a-badge.gray { background:#eef1f5; color:#3f4a5a; border-color:#cdd5df; }

/* Facts, summary, empty states */
.n2a-fact { background:var(--card); border:1px solid var(--line); border-left:4px solid var(--blue); border-radius:10px;
  padding:.7rem .9rem; height:100%; }
.n2a-fact .k { font-size:.7rem; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); font-weight:600; }
.n2a-fact .v { font-size:.98rem; color:var(--ink); font-weight:500; word-break:break-word; }
.n2a-summary { background:#eef6fc; border:1px solid #bddcf0; border-left:5px solid var(--blue); border-radius:12px;
  padding:1rem 1.2rem; color:var(--ink); line-height:1.6; }
.n2a-empty { text-align:center; padding:2.4rem 1rem; border:2px dashed #b9d4e8; border-radius:14px; background:var(--wash); }
.n2a-empty .big { font-size:2rem; color:var(--blue); }
.n2a-empty h4 { margin:.2rem 0 .3rem 0; color:var(--ink); } .n2a-empty p { margin:0; color:var(--muted); }
.n2a-accent { height:4px; border-radius:4px; margin:-.2rem 0 .6rem 0; }
.n2a-accent.pending { background:var(--blue); } .n2a-accent.completed { background:var(--green); }
.n2a-accent.overdue { background:repeating-linear-gradient(45deg,var(--verm),var(--verm) 8px,#f0a37a 8px,#f0a37a 16px); }

/* Controls */
div[data-testid="stVerticalBlockBorderWrapper"] { border-radius:12px; }
.stButton > button { border-radius:10px; font-weight:600; }
:focus-visible { outline:3px solid var(--orange) !important; outline-offset:2px; }

/* ---------- Polish layer ---------- */
.stApp { background:
  radial-gradient(1200px 380px at 85% -80px, rgba(86,180,233,.18), transparent 70%),
  radial-gradient(900px 320px at -10% -60px, rgba(230,159,0,.10), transparent 70%), #ffffff; }
header[data-testid="stHeader"] { background:transparent; }

/* Section headings get a coloured marker */
.stMarkdown h3 { position:relative; padding-left:.85rem; margin-top:1.4rem; }
.stMarkdown h3::before { content:""; position:absolute; left:0; top:.22em; bottom:.22em; width:5px; border-radius:4px;
  background:linear-gradient(180deg,var(--blue),var(--orange)); }
.stMarkdown h4 { color:#12395a; font-weight:700; margin-top:1.2rem; }
.stMarkdown h2 { border-bottom:2px solid var(--line); padding-bottom:.4rem; }

/* Sidebar navigation */
[data-testid="stSidebarNav"] a, [data-testid="stSidebarNavLink"] { border-radius:10px; margin:2px 6px; padding:.45rem .7rem;
  color:#1c3550; font-weight:500; transition:background .15s ease, transform .15s ease; }
[data-testid="stSidebarNav"] a:hover, [data-testid="stSidebarNavLink"]:hover { background:#dcecf8; transform:translateX(2px); }
[data-testid="stSidebarNav"] a[aria-current="page"], [data-testid="stSidebarNavLink"][aria-current="page"] {
  background:linear-gradient(90deg,#0072B2,#2a8fc7); color:#fff; font-weight:700; box-shadow:0 3px 10px rgba(0,114,178,.28); }
[data-testid="stSidebarNav"] a[aria-current="page"] *, [data-testid="stSidebarNavLink"][aria-current="page"] * { color:#fff !important; }

/* Buttons */
.stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
  border-radius:10px; font-weight:600; padding:.5rem 1.1rem; transition:all .15s ease; border:1px solid #b9d4e8; }
.stButton > button:hover, .stFormSubmitButton > button:hover { border-color:var(--blue); color:var(--blue);
  transform:translateY(-1px); box-shadow:0 4px 12px rgba(0,114,178,.15); }
button[kind="primary"], button[data-testid="stBaseButton-primary"], button[data-testid="stBaseButton-primaryFormSubmit"] {
  background:linear-gradient(135deg,#005f94,#0072B2 55%,#2a8fc7); color:#fff !important; border:none;
  box-shadow:0 4px 14px rgba(0,114,178,.32); }
button[kind="primary"]:hover, button[data-testid="stBaseButton-primary"]:hover,
button[data-testid="stBaseButton-primaryFormSubmit"]:hover { color:#fff !important; filter:brightness(1.07);
  box-shadow:0 7px 18px rgba(0,114,178,.38); }
button:disabled { opacity:.55; box-shadow:none !important; transform:none !important; }

/* Inputs, selects, text areas */
div[data-baseweb="input"], div[data-baseweb="select"] > div, div[data-baseweb="textarea"] {
  border-radius:10px !important; border-color:#c9d9e6 !important; background:#fbfdff !important; }
div[data-baseweb="input"]:focus-within, div[data-baseweb="select"] > div:focus-within,
div[data-baseweb="textarea"]:focus-within { border-color:var(--blue) !important; box-shadow:0 0 0 3px rgba(0,114,178,.18) !important; }
label[data-testid="stWidgetLabel"] p { font-weight:600; color:#1c3550; font-size:.88rem; }

/* File uploader drop zone */
[data-testid="stFileUploaderDropzone"] { border:2px dashed #7fb6d9; border-radius:16px; background:
  linear-gradient(135deg,#f3f9fe,#fff8ea); padding:1.6rem; transition:all .2s ease; }
[data-testid="stFileUploaderDropzone"]:hover { border-color:var(--blue); background:linear-gradient(135deg,#e8f4fc,#fff3d8);
  box-shadow:0 6px 20px rgba(0,114,178,.12); }

/* Cards: bordered containers, forms, expanders */
div[data-testid="stVerticalBlockBorderWrapper"]:has(> div > div[data-testid="stVerticalBlock"]) {
  background:#fff; box-shadow:0 2px 8px rgba(15,31,51,.05); transition:box-shadow .2s ease, border-color .2s ease; }
div[data-testid="stVerticalBlockBorderWrapper"]:hover { box-shadow:0 6px 18px rgba(15,31,51,.09); }
[data-testid="stForm"] { border:1px solid var(--line); border-radius:14px; background:var(--wash); padding:1.1rem; }
[data-testid="stExpander"] { border:1px solid var(--line); border-radius:12px; background:#fff; overflow:hidden; }
[data-testid="stExpander"] summary { font-weight:600; color:#12395a; }
[data-testid="stExpander"] summary:hover { background:var(--wash); }

/* Alerts */
[data-testid="stAlert"] { border-radius:12px; border:1px solid var(--line); border-left-width:5px; }

/* Tables */
[data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:12px; overflow:hidden;
  box-shadow:0 2px 8px rgba(15,31,51,.05); }

/* Dialog */
div[role="dialog"] { border-radius:18px; border-top:6px solid var(--verm); }

/* Native metric (Settings) */
[data-testid="stMetric"] { background:#fff; border:1px solid var(--line); border-top:4px solid var(--blue);
  border-radius:12px; padding:.8rem 1rem; box-shadow:0 1px 3px rgba(15,31,51,.06); }

/* Gentle entrance, disabled for users who prefer reduced motion */
@keyframes n2a-rise { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:none; } }
.n2a-hero, .n2a-header { animation: n2a-rise .35s ease both; }
@media (prefers-reduced-motion: reduce) { * { animation:none !important; transition:none !important; } }
hr { border-color:var(--line); }

/* Custom sidebar nav (st.page_link) */
[data-testid="stPageLink-NavLink"] { border-radius:12px; margin:3px 0; padding:.55rem .8rem; color:#1c3550;
  font-weight:500; transition:background .15s ease, transform .15s ease; }
[data-testid="stPageLink-NavLink"]:hover { background:#dcecf8; transform:translateX(3px); }
section[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][aria-current="page"] {
  background:linear-gradient(90deg,#0b3c5d,#0072B2 70%,#2a8fc7) !important; box-shadow:0 4px 12px rgba(0,114,178,.3); }
section[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][aria-current="page"] * { color:#fff !important; font-weight:700; }
.n2a-metric { min-height:112px; height:100%; }
.n2a-hero { position:relative; overflow:hidden; }
.n2a-hero::after { content:""; position:absolute; right:-60px; top:-70px; width:260px; height:260px; border-radius:50%;
  background:radial-gradient(circle at 30% 30%, rgba(255,255,255,.28), rgba(255,255,255,0) 70%); }
.n2a-hero::before { content:""; position:absolute; right:150px; bottom:-90px; width:200px; height:200px; border-radius:50%;
  background:radial-gradient(circle, rgba(230,159,0,.35), rgba(230,159,0,0) 70%); }
.n2a-hero h1 { position:relative; z-index:1; } .n2a-hero p { position:relative; z-index:1; }
.n2a-empty { border:1px solid #cfe0ee; background:linear-gradient(180deg,#f7fbff,#fff); box-shadow:0 2px 10px rgba(15,31,51,.04); }

/* ================= v3: navy sidebar, scroll panels, equal heights ================= */
section[data-testid="stSidebar"] { background:linear-gradient(185deg,#0a2540 0%,#0c3a60 58%,#0b4a73 100%) !important;
  border-right:none; box-shadow:4px 0 24px rgba(10,37,64,.20); }
section[data-testid="stSidebar"] > div { background:transparent !important; }
section[data-testid="stSidebar"] button[kind="headerNoPadding"], section[data-testid="stSidebar"] [data-testid="stSidebarCollapseButton"] button
  { color:#cfe0f0 !important; }
section[data-testid="stSidebar"] .n2a-brand { padding:.5rem .3rem .1rem .3rem; }
section[data-testid="stSidebar"] .n2a-logo { width:40px; height:40px; font-size:1.2rem; border-radius:12px;
  background:linear-gradient(135deg,#E69F00,#f3c34d); color:#0a2540; box-shadow:0 6px 16px rgba(230,159,0,.40); }
section[data-testid="stSidebar"] .n2a-brand-name { color:#ffffff; font-size:1.22rem; }
section[data-testid="stSidebar"] .n2a-brand-sub { color:#a9c4dc; margin:.25rem 0 .2rem .35rem; padding-bottom:.9rem;
  border-bottom:1px solid rgba(255,255,255,.14); }
.n2a-side-label { font-size:.68rem; letter-spacing:.16em; text-transform:uppercase; color:#8fb3d1; font-weight:700;
  margin:1rem 0 0 .45rem; padding-bottom:.7rem; line-height:1.2; }
section[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"] { color:#d6e6f5 !important; border:1px solid transparent;
  border-radius:12px; padding:.62rem .9rem; margin:2px 0; font-weight:500; background:transparent; }
section[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"] * { color:inherit !important; }
section[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"]:hover { background:rgba(255,255,255,.09);
  border-color:rgba(255,255,255,.14); transform:translateX(3px); }
.n2a-side-card { background:rgba(255,255,255,.07); border:1px solid rgba(255,255,255,.14); border-radius:14px;
  padding:.8rem .9rem; color:#dbe9f6; font-size:.82rem; line-height:1.5; margin-bottom:1.1rem; }  /* offsets Streamlit's negative markdown margin */
.n2a-side-card .ttl { font-size:.68rem; letter-spacing:.14em; text-transform:uppercase; color:#8fb3d1; font-weight:700; }
.n2a-side-card .st { display:flex; align-items:center; gap:.5rem; margin:.35rem 0 .15rem 0; font-weight:700; color:#fff; }
.n2a-side-card .glyph { display:inline-flex; width:20px; height:20px; border-radius:50%; align-items:center;
  justify-content:center; font-size:.78rem; font-weight:800; }
.n2a-side-card .glyph.ok { background:#009E73; color:#fff; } .n2a-side-card .glyph.bad { background:#D55E00; color:#fff; }
.n2a-side-card .glyph.warn { background:#E69F00; color:#0a2540; }
.n2a-side-card .mono { font-family:Consolas,monospace; font-size:.76rem; color:#b9d2e8; word-break:break-all; }
.n2a-side-foot { text-align:center; color:#a9c4dc; font-size:.78rem; padding-top:.8rem;
  border-top:1px solid rgba(255,255,255,.14); }
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] { display:flex; flex-direction:column;
  min-height:calc(100vh - 2.5rem); }
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] > div { flex:1 1 auto; display:flex; flex-direction:column; }
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] [data-testid="stVerticalBlock"] { flex:1 1 auto; gap:.3rem; }
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] { display:flex; flex-direction:column; }
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] > [data-testid="stSidebarUserContent"] { flex:1 1 auto; }
section[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.n2a-side-foot) { margin-top:auto; }

/* Scrollbars: visible, themed, thin */
* { scrollbar-width:thin; scrollbar-color:#6aa9d0 #e8f1f8; }
*::-webkit-scrollbar { width:11px; height:11px; }
*::-webkit-scrollbar-track { background:#e8f1f8; border-radius:8px; }
*::-webkit-scrollbar-thumb { background:linear-gradient(180deg,#0072B2,#56B4E9); border-radius:8px; border:2px solid #e8f1f8; }
*::-webkit-scrollbar-thumb:hover { background:#005f94; }
[data-testid="stVerticalBlockBorderWrapper"] > div { overflow-x:auto; }

/* Equal-size boxes */
.n2a-metric { height:116px; min-height:116px; overflow:hidden; }
.n2a-metric .label, .n2a-metric .hint { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%; }
.n2a-metric > div:last-child { min-width:0; }
.n2a-fact { height:92px; overflow:hidden; }
.n2a-clamp { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; min-height:2.9em;
  line-height:1.45; color:var(--ink); word-break:break-word; }
.n2a-clamp { margin-bottom:.45rem; }
.n2a-clamp.one { -webkit-line-clamp:1; min-height:1.45em; }
.n2a-clamp.muted { color:var(--muted); }
.n2a-title { font-weight:700; color:var(--ink); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-size:1.02rem; }
.n2a-meta { margin:.15rem 0 .3rem 0; min-height:1.7em; color:var(--muted); font-size:.8rem; }
.n2a-panel-title { font-weight:700; color:#12395a; margin:0 0 .4rem 0; }

/* Delete-style buttons: icon + label + vermillion accent (not colour alone) */
[class*="st-key-del_"] button, .st-key-detail_delete button { color:#8a3900; border-color:#eeb08c; background:#fff8f3; }
[class*="st-key-del_"] button:hover, .st-key-detail_delete button:hover { color:#fff; background:#D55E00; border-color:#D55E00; }

/* Temporary flash message (fades out by itself, pure CSS) */
.n2a-flash { display:flex; align-items:center; gap:.7rem; padding:.85rem 1.2rem; border-radius:14px;
  font-weight:600; border:1px solid; border-left-width:6px; overflow:hidden;
  position:fixed; top:68px; left:300px; right:0; margin:0 auto; width:max-content; max-width:min(560px, 85vw);
  z-index:1000001; pointer-events:none;
  animation:n2a-flash 6s ease forwards; box-shadow:0 12px 32px rgba(15,31,51,.22); }
.n2a-flash .g { display:inline-flex; width:26px; height:26px; border-radius:50%; align-items:center; justify-content:center;
  font-weight:800; color:#fff; flex:0 0 26px; }
.n2a-flash.success { background:#e6f6f0; color:#00553f; border-color:#8fd6bd; } .n2a-flash.success .g { background:#009E73; }
.n2a-flash.warning { background:#fdf0d0; color:#6b4500; border-color:#efcf80; } .n2a-flash.warning .g { background:#b87f00; }
.n2a-flash.info { background:#e1f0f9; color:#00405f; border-color:#9ccbe6; } .n2a-flash.info .g { background:#0072B2; }
@keyframes n2a-flash { 0% { opacity:0; transform:translateY(-8px); max-height:90px; } 6% { opacity:1; transform:none; }
  85% { opacity:1; max-height:90px; } 100% { opacity:0; max-height:0; padding-top:0; padding-bottom:0; margin:0; border-width:0; } }
@keyframes n2a-flash-fade { 0%,85% { opacity:1; max-height:90px; } 100% { opacity:0; max-height:0; padding:0; margin:0; border-width:0; } }
@media (prefers-reduced-motion: reduce) { .n2a-flash { animation:n2a-flash-fade 6s forwards !important; } }
</style>
"""

# colour class + icon glyph per semantic key (icon keeps meaning without colour)
BADGE_STYLES = {
    "high": ("verm", "▲"), "medium": ("orange", "◆"), "low": ("blue", "▼"),
    "pending": ("blue", "○"), "completed": ("green", "✓"), "overdue": ("verm", "!"),
    "explicit": ("green", "✓"), "manual": ("purple", "✎"), "inferred": ("orange", "~"),
    "unspecified": ("gray", "?"), "none": ("gray", "–"),
    "indigo": ("blue", ""), "gray": ("gray", ""),
}


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def highlight_active_nav(url_path: str) -> None:
    """Style the sidebar link of the current page (Streamlit exposes no active attribute).

    The active link is a white pill with dark text and an orange edge: high contrast and a
    shape cue, so it never relies on hue alone.
    """
    safe = "".join(ch for ch in (url_path or "") if ch.isalnum() or ch in "-_")
    sel = f'section[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"][href="{safe}"]'
    st.markdown(
        f"<style>{sel} {{ background:#ffffff !important; border-color:#ffffff !important; color:#0a2540 !important;"
        f" box-shadow:inset 5px 0 0 #E69F00, 0 8px 18px rgba(0,0,0,.28); transform:none !important; }}"
        f" {sel} * {{ color:#0a2540 !important; font-weight:700; }}</style>",
        unsafe_allow_html=True,
    )


def scroll_panel(height: int):
    """Fixed-height bordered container: scrolls vertically (and horizontally) instead of growing the page."""
    return st.container(height=height, border=True)


def clamp(text, lines: int = 2, muted: bool = False) -> str:
    """HTML snippet clamped to a fixed number of lines so sibling cards keep equal heights."""
    cls = "n2a-clamp" + (" one" if lines == 1 else "") + (" muted" if muted else "")
    shown = safe_html(text) if text else "&nbsp;"
    return f'<div class="{cls}" title="{safe_html(text or "")}">{shown}</div>'


def title_html(text) -> str:
    return f'<div class="n2a-title" title="{safe_html(text)}">{safe_html(text)}</div>'


def flash(message: str, kind: str = "success") -> None:
    """Queue a temporary message to show (once) on the next render."""
    st.session_state["_flash"] = (kind, message)


def render_flash() -> None:
    """Show and clear the queued message. The banner fades out by itself after a few seconds."""
    item = st.session_state.pop("_flash", None)
    if not item:
        return
    kind, message = item
    glyph = {"success": "✓", "warning": "!", "info": "i"}.get(kind, "✓")
    st.markdown(
        f'<div class="n2a-flash {escape(kind)}" role="status" aria-live="polite">'
        f'<span class="g" aria-hidden="true">{glyph}</span><span>{escape(message)}</span></div>',
        unsafe_allow_html=True,
    )


def sidebar_status(api_ok: bool, model: str, db_label: str = "", db_cloud: bool = False) -> None:
    glyph, cls, text = ("✓", "ok", "Connected") if api_ok else ("✕", "bad", "Not configured")
    db_html = ""
    if db_label:
        # cloud database = permanent storage (✓); local file = development fallback (!)
        d_glyph, d_cls = ("✓", "ok") if db_cloud else ("!", "warn")
        db_html = (f'<div class="ttl" style="margin-top:.7rem">Database</div>'
                   f'<div class="st"><span class="glyph {d_cls}" aria-hidden="true">{d_glyph}</span>{escape(db_label)}</div>')
    st.markdown(
        '<div class="n2a-side-card"><div class="ttl">Gemini AI</div>'
        f'<div class="st"><span class="glyph {cls}" aria-hidden="true">{glyph}</span>API key: {text}</div>'
        f'<div class="mono">{escape(model)}</div>{db_html}</div>',
        unsafe_allow_html=True,
    )


_HTML_ENTITIES = {
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;",
    # Markdown-significant characters become numeric entities so they render as text but can never
    # form links, images, headings, emphasis or math, whatever parser sits between us and the browser.
    "[": "&#91;", "]": "&#93;", "(": "&#40;", ")": "&#41;", "!": "&#33;", "*": "&#42;", "_": "&#95;",
    "`": "&#96;", "$": "&#36;", "#": "&#35;", "~": "&#126;", "|": "&#124;", "\\": "&#92;",
}


def safe_html(text) -> str:
    """Make untrusted text safe to place inside an HTML block rendered by st.markdown(unsafe_allow_html=True)."""
    raw = str(text if text is not None else "")
    return "".join(_HTML_ENTITIES.get(ch, ch) for ch in raw).replace("\n", "<br>")


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|<>~$&])")


def md_escape(text) -> str:
    """Escape Markdown so untrusted text (AI output, file names) renders literally.

    Prevents injected links, images (tracking pixels), headings, HTML and LaTeX.
    """
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(text if text is not None else "").split()))


def database_error(exc: Exception) -> None:
    """Friendly full-page notice when the database cannot be reached. The message is secret-free."""
    with st.container(border=True):
        st.markdown("#### Database unavailable")
        st.error(str(exc))
        st.caption("Your data is safe. This is a connection problem, not data loss. Check DATABASE_URL in "
                   ".streamlit/secrets.toml (or Streamlit Cloud > Settings > Secrets), then retry.")
        if st.button("Retry connection", key="db_retry", type="primary", icon=":material/refresh:"):
            st.cache_resource.clear()
            st.rerun()


def side_label(text: str) -> None:
    st.markdown(f'<div class="n2a-side-label">{escape(text)}</div>', unsafe_allow_html=True)


def side_footer(text: str) -> None:
    st.markdown(f'<div class="n2a-side-foot">{escape(text)}</div>', unsafe_allow_html=True)


def brand() -> None:
    st.markdown(
        '<div class="n2a-brand"><div class="n2a-logo">N</div>'
        '<div class="n2a-brand-name">Notice2Action AI</div></div>'
        '<div class="n2a-brand-sub">Smart notice &amp; deadline extractor</div>',
        unsafe_allow_html=True,
    )


def page_header(title: str, subtitle: str = "") -> None:
    sub = f"<p>{escape(subtitle)}</p>" if subtitle else ""
    st.markdown(f'<div class="n2a-header"><h1>{escape(title)}</h1><div class="rule"></div>{sub}</div>',
                unsafe_allow_html=True)


def hero(title: str, subtitle: str) -> None:
    st.markdown(f'<div class="n2a-hero"><h1>{escape(title)}</h1><p>{escape(subtitle)}</p></div>',
                unsafe_allow_html=True)


def metric_card(label: str, value, hint: str = "", tone: str = "", icon: str = "●") -> None:
    cls = tone or "blue"
    st.markdown(
        f'<div class="n2a-metric {escape(cls)}"><div class="ico" aria-hidden="true">{escape(icon)}</div>'
        f'<div><div class="label">{escape(label)}</div><div class="value">{escape(str(value))}</div>'
        f'<div class="hint">{escape(hint)}</div></div></div>',
        unsafe_allow_html=True,
    )


def badge_html(text: str, tone: str = "blue") -> str:
    color, icon = BADGE_STYLES.get(tone, (tone, ""))
    glyph = f'<span aria-hidden="true">{icon}</span>' if icon else ""
    return f'<span class="n2a-badge {escape(color)}">{glyph}{safe_html(text)}</span>'


def badges(*items) -> None:
    """Render (text, tone) pairs as a row of badges."""
    st.markdown("".join(badge_html(t, tone) for t, tone in items), unsafe_allow_html=True)


def accent_bar(state: str) -> None:
    st.markdown(f'<div class="n2a-accent {escape(state)}"></div>', unsafe_allow_html=True)


def fact(label: str, value) -> str:
    shown = safe_html(value) if value else '<span style="color:#6b7686">Not specified</span>'
    return (f'<div class="n2a-fact"><div class="k">{escape(label)}</div>'
            f'<div class="v n2a-clamp" title="{safe_html(value or "")}" style="min-height:0">{shown}</div></div>')


def empty_state(title: str, message: str) -> None:
    st.markdown(
        f'<div class="n2a-empty"><div class="big" aria-hidden="true">◇</div><h4>{escape(title)}</h4>'
        f'<p>{escape(message)}</p></div>',
        unsafe_allow_html=True,
    )
