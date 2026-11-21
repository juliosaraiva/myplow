# ---------------------------------------------------------------- status
def cmd_status(args):
    cfg = load_cfg()
    if not cfg:
        print("[myplow] not configured yet — run `mypeople up`")
        return 1
    hud = "http://127.0.0.1:%s" % cfg.get("HUD_PORT", "9900")
    secret = cfg.get("QUEUE_SECRET", "")
    hc, hb = _get(hud + "/health")
    print("health : %s" % (hb.get("status") if isinstance(hb, dict) else "DOWN"))
    ac, agents = _get(hud + "/agents", secret)
    for a in (agents or []):
        print("  %-40s [%s] %s" % (a.get("agent_id"), a.get("state", "?"), a.get("status", "")))
    for k, v in urls(cfg).items():
        print("%-6s : %s" % (k, v))
    return 0 if hc == 200 else 1


