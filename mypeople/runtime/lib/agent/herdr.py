"""Normal mp entry points for an opt-in, isolated local engineer."""

import mpherdr
import sqlite3


def herdr_enabled():
    return bool(os.environ.get("MYPEOPLE_HERDR_SOCKET"))


def herdr_target(aid):
    if not is_local(aid):
        raise mpherdr.TransportError("Herdr pilot accepts local agent IDs only")
    _, session, tab = C.parse_agent_id(aid)
    if not session.startswith("herdr-") or not tab:
        raise mpherdr.TransportError("use a separate herdr-* MyPlow session")


def herdr_error(error):
    print("ERROR: %s" % error, file=sys.stderr)
    return 1


def herdr_action(operation, aid, *args):
    try:
        herdr_target(aid)
        pilot = mpherdr.Pilot(
            os.environ.get("MYPEOPLE_HERDR_SOCKET", ""),
            os.path.join(INSTALL_DIR, "state", "herdr-pilot.sqlite3"),
        )
        result = getattr(pilot, operation)(aid, *args)
        if operation in ("spawn", "lookup"):
            identity = {key: result[key] for key in ("pane_id", "workspace_id", "tab_id", "terminal_id")}
            print(json.dumps({"agent_id": aid, **identity}, sort_keys=True), flush=True)
        elif operation == "attach":
            return result
        elif result is not None:
            print(result)
        return 0
    except mpherdr.TransportError as error:
        return herdr_error(error)
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
        return herdr_error(mpherdr.TransportError("Herdr protocol or state unavailable"))


def do_herdr_send(argv):
    if len(argv) < 4 or argv[0] != "--delivery-id" or argv[2].startswith("-"):
        print("usage: mp send --delivery-id ID <agent_id> <message>", file=sys.stderr)
        return 2
    return herdr_action("send", full_aid(argv[2]), argv[1], " ".join(argv[3:]))


def do_reconnect(argv):
    if not herdr_enabled() or not argv or argv[1:] not in ([], ["--attach"]):
        print(USAGE["reconnect"], file=sys.stderr)
        return 2
    aid = full_aid(argv[0])
    return (
        herdr_action("attach", aid, os.environ.get("MYPEOPLE_HERDR_BIN", ""))
        if argv[1:]
        else herdr_action("lookup", aid)
    )
