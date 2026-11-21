# ---------- page rendering (shared by both front doors) ----------
_VERSION_BADGE = """
<style>
#mp-version-badge{position:fixed;right:10px;bottom:9px;z-index:2147483000;
 font:400 10.5px/1 'DM Mono',ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.04em;
 color:rgba(240,240,232,.3);pointer-events:none;user-select:none}
@media print{#mp-version-badge{display:none}}
</style>
<div id="mp-version-badge" title="MyPlow version serving this page">__MP_VERSION__</div>
"""

# Every first-class surface, reachable from every other one. Injected at this seam for the same
# reason as the badge: a page added later is linked by construction, and no page can drift into
# being a dead end you can only reach by typing a URL. There are exactly two surfaces -- Board
# (the priorities) and Graph -- and the bar sits at the TOP of every page. Individual terminals
# are not a surface: the Graph is how you reach a terminal. The HUD page (/dashboard) still
# serves by URL but is off the bar: the CEO never used it.
_NAV = """
<style>
/* Brand tokens, read off the page itself. todos/dashboard and the graph name them differently
   (--muted-dark vs --muted, --text-dark vs --ink), so each one falls back to the other and then
   to a literal -- the nav must look native on a page that defines neither. */
/* A segmented control, the height of the board's add bar beside it (44px), in product-UI radii
   (10px, not marketing pills). */
#mp-nav{position:fixed;right:22px;top:18px;z-index:2147483000;display:flex;gap:2px;padding:3px;
 --mp-volt:var(--volt,#D5EF8A);
 --mp-ink:var(--text-dark,var(--ink,#F0F0E8));
 --mp-muted:var(--muted-dark,var(--muted,rgba(240,240,232,.45)));
 --mp-line:var(--dark-border,rgba(255,255,255,.09));
 border-radius:12px;background:rgba(26,26,24,.82);border:1px solid var(--mp-line);
 -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);box-shadow:0 8px 30px rgba(0,0,0,.28);
 font-family:'DM Sans',system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased}
#mp-nav a{display:flex;align-items:center;gap:7px;height:36px;padding:0 13px 0 11px;border-radius:9px;
 text-decoration:none;font-size:13px;font-weight:500;line-height:1;color:var(--mp-muted);
 transition:color .15s,background .15s}
#mp-nav a svg{flex:0 0 auto;opacity:.75;transition:opacity .15s,color .15s}
#mp-nav a:hover{color:var(--mp-ink);background:rgba(255,255,255,.05)}
#mp-nav a:focus-visible{outline:2px solid var(--mp-volt);outline-offset:2px}
#mp-nav a[aria-current="page"]{color:var(--mp-ink);background:rgba(255,255,255,.09);
 box-shadow:inset 0 0 0 1px rgba(255,255,255,.05)}
#mp-nav a[aria-current="page"] svg{color:var(--mp-volt);opacity:1}
@media(max-width:700px){#mp-nav{right:12px;top:12px}#mp-nav a{height:32px;padding:0 11px 0 9px}}
@media print{#mp-nav{display:none}}
</style>
<nav id="mp-nav" aria-label="MyPlow surfaces">
 <a href="/" data-mp-path="/"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/></svg>Board</a>
 <a href="/terminal-graph" data-mp-path="/terminal-graph"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="16" y="16" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/><rect x="9" y="2" width="6" height="6" rx="1"/><path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3"/><path d="M12 12V8"/></svg>Graph</a>
 <a href="/plugins" data-mp-path="/plugins"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/></svg>Plugins</a>
 <a href="/access" data-mp-path="/access"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8" cy="12" r="4"/><path d="M12 12h9M18 12v3"/></svg>Access</a>
</nav>
<script>
(function(){
  var here=location.pathname.replace(/\\/+$/,"")||"/";
  if(here==="/todos"){here="/";}
  document.querySelectorAll("#mp-nav a[data-mp-path]").forEach(function(a){
    if(a.getAttribute("data-mp-path")===here){a.setAttribute("aria-current","page");}
  });
  // The login banner is sticky at the very top; measured rather than hardcoded because its
  // height depends on how many login steps this node prints. Found by attribute, not by id, so
  // this script never carries the banner's id into a page that has no banner.
  var nav=document.getElementById("mp-nav");
  function place(){
    var b=document.querySelector("[data-mp-banner]");
    nav.style.top=(b?b.getBoundingClientRect().height+14:18)+"px";
  }
  place();
  window.addEventListener("resize",place);
})();
</script>
"""

# Shown on every page while this node has no AI login. It is deliberately loud and at the top of
# the document: the failure it describes ("nothing happens when I ask for anything") is otherwise
# indistinguishable from the product being broken.
_LOGIN_BANNER_TMPL = """
<style>
#mp-login-banner{position:sticky;top:0;z-index:2147483001;
 font:400 13.5px/1.55 'DM Sans',system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased;
 color:#F0F0E8;background:#231f14;border-bottom:1px solid rgba(254,188,46,.3);padding:12px 20px}
#mp-login-banner b{font-weight:600;color:#ffd98a}
#mp-login-banner code{display:inline-block;margin:3px 6px 0 0;padding:3px 8px;
 font:400 12px/1.5 'DM Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
 color:#F0F0E8;background:rgba(0,0,0,.3);border:1px solid rgba(254,188,46,.22);border-radius:6px;
 user-select:all}
</style>
<div id="mp-login-banner" data-mp-banner>
  <b>Login required.</b> This node has no AI login yet, so no agent can run.
  Open a terminal on this machine and run: __MP_LOGIN_STEPS__
  <span style="opacity:.8">The Boss starts by itself ~15s after login — no restart needed.</span>
</div>
"""

def auth_state():
    """This node's login state, as published by the package (firstrun.write_auth_state).

    Absent file => say nothing. An install that predates the file, or a runtime driven without
    the CLI, must not grow a scary banner just because it cannot find a status file.
    """
    try:
        with open(os.path.join(CFG["INSTALL_DIR"], "status", "auth.json")) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _login_banner():
    """The 'you still have to log in' notice, or '' when this node is fine."""
    state = auth_state()
    if not state or state.get("authenticated") is not False:
        return ""
    steps = "".join(
        "<code>%s</code>" % html.escape(str(line)) for line in (state.get("howto") or []))
    return _LOGIN_BANNER_TMPL.replace("__MP_LOGIN_STEPS__", steps)


_FONTS = '<link rel="stylesheet" href="/fonts/fonts.css">'


def render_page(html):
    """Substitute the page placeholders every UI page shares, and make the running version
    visible on it. Injecting here (rather than in each .html) is what makes 'every page'
    true by construction — including pages added later."""
    # The brand faces the pages name (Instrument Serif, DM Sans, DM Mono), served by the board
    # server from bin/fonts. Without this every page fell back to Georgia and the system fonts.
    if _FONTS not in html and "</head>" in html:
        html = html.replace("</head>", _FONTS + "</head>", 1)
    banner = _login_banner()
    if banner and "id=\"mp-login-banner\"" not in html:
        if "<body" in html:
            idx = html.index("<body")
            end = html.index(">", idx) + 1
            html = html[:end] + banner + html[end:]
        else:
            html = banner + html
    chrome = ""
    if "id=\"mp-nav\"" not in html:
        chrome += _NAV
    if "id=\"mp-version-badge\"" not in html:
        chrome += _VERSION_BADGE
    if chrome:
        if "</body>" in html:
            html = html.replace("</body>", chrome + "</body>", 1)
        else:
            html += chrome
    # Placeholders are substituted LAST, after every injection: the nav and the banner carry
    # placeholders of their own, and substituting first would ship them to the browser literally.
    html = html.replace("__TTYD_PORT__", str(CFG["TTYD_BROWSER_PORT"]))
    html = html.replace("__HOST_ID__", CFG["HOST_ID"])
    return html.replace("__MP_VERSION__", "v" + version())


