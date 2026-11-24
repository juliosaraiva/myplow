"""Authenticated board entry point for learning records."""

import mplearning as learning
import sqlite3


def learning_request(handler, ident, body):
    grants = os.environ.get("LEARNING_GRANTS_FILE") or CFG.get("LEARNING_GRANTS_FILE")
    if not grants or CFG["BIND_ADDR"] != "127.0.0.1" or ident != "machine":
        return 403, {"error": "learning_unavailable"}
    try:
        grant = learning.authenticate(grants, handler.headers.get("X-MyPlow-Learning", ""))
        store = learning.LearningStore(os.path.join(INSTALL_DIR, "state", "learning", "episodes.sqlite3"))
        return 200, store.execute(grant, body)
    except learning.Denied:
        return 403, {"error": "learning_denied"}
    except learning.Conflict:
        return 409, {"error": "learning_conflict"}
    except ValueError:
        return 400, {"error": "invalid_learning_request"}
    except (OSError, sqlite3.Error):
        return 503, {"error": "learning_store_unavailable"}
