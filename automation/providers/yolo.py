#!/usr/bin/env python3
import os
import json
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]  # -> crypto_casino/
PYTHON = os.getenv("PYTHON_BIN", "python")       # let you override in .env if needed

# You already have .env at project root; we just read values that matter
YOLO_ENABLED = os.getenv("YOLO_ENABLED", "true").lower() == "true"
YOLO_LABEL = os.getenv("YOLO_LABEL", "YOLO777")

# path to the bot we just finished
YOLO_BOT_PATH = BASE_DIR / "automation" / "yolo_bot.py"


def _run_cmd(args: list[str]) -> tuple[int, str, str]:
    """
    Run a command and return (rc, stdout, stderr).
    We keep it simple, same style as other providers.
    """
    proc = subprocess.Popen(
        args,
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate()
    return proc.returncode, out, err


def create(username: str | None = None) -> dict:
    """
    Create a YOLO player.

    If username is None, yolo_bot.py will generate one (auto_xxxxx_yl) — we already
    saw that working.
    We parse its JSON-ish print:
        {'ok': True, 'account': '...', 'password': '...', 'credit': 0}
    and normalize.
    """
    if not YOLO_ENABLED:
        return {"ok": False, "error": "YOLO provider disabled"}

    # our bot does: python automation/yolo_bot.py create
    # (it does not accept username yet, so we just call it)
    cmd = [PYTHON, str(YOLO_BOT_PATH), "create"]

    rc, out, err = _run_cmd(cmd)

    if rc != 0:
        return {
            "ok": False,
            "error": f"yolo create failed (rc={rc})",
            "stdout": out,
            "stderr": err,
        }

    # the bot prints python-dict style, so eval-safe parse:
    # out: "{'ok': True, 'account': 'tzgm0_jz1p7_yl', 'password': 'Abc12345', 'credit': 0}"
    out = out.strip()
    data = None
    # try json first
    try:
        data = json.loads(out)
    except Exception:
        # try python-literal style
        try:
            data = eval(out, {"__builtins__": {}})
        except Exception:
            data = None

    if not data or not isinstance(data, dict):
        return {
            "ok": False,
            "error": "yolo create: could not parse output",
            "raw": out,
        }

    # normalize
    return {
        "ok": bool(data.get("ok")),
        "username": data.get("account"),
        "password": data.get("password"),
        "raw": data,
        "provider": "yolo",
    }


def credit(username: str, amount: float) -> dict:
    """
    Recharge (load) the player on YOLO.
    This calls: python automation/yolo_bot.py recharge <username> <amount>
    """
    if not YOLO_ENABLED:
        return {"ok": False, "error": "YOLO provider disabled"}

    cmd = [
        PYTHON,
        str(YOLO_BOT_PATH),
        "recharge",
        str(username),
        str(amount),
    ]
    rc, out, err = _run_cmd(cmd)

    if rc != 0:
        return {
            "ok": False,
            "error": f"yolo recharge failed (rc={rc})",
            "stdout": out,
            "stderr": err,
        }

    return {
        "ok": True,
        "username": username,
        "amount": float(amount),
        "provider": "yolo",
        "stdout": out,
    }


def redeem(username: str, amount: float) -> dict:
    """
    Redeem (withdraw) from player on YOLO.
    This calls: python automation/yolo_bot.py redeem <username> <amount>
    """
    if not YOLO_ENABLED:
        return {"ok": False, "error": "YOLO provider disabled"}

    cmd = [
        PYTHON,
        str(YOLO_BOT_PATH),
        "redeem",
        str(username),
        str(amount),
    ]
    rc, out, err = _run_cmd(cmd)

    if rc != 0:
        return {
            "ok": False,
            "error": f"yolo redeem failed (rc={rc})",
            "stdout": out,
            "stderr": err,
        }

    return {
        "ok": True,
        "username": username,
        "amount": float(amount),
        "provider": "yolo",
        "stdout": out,
    }


def auto_create_if_needed(player_obj) -> dict:
    """
    Same pattern you use with other providers: employee approves -> system
    checks if remote account exists -> if not, create.

    Here we just wrap create().
    """
    res = create()
    if not res.get("ok"):
        return res

    # you can update local DB here if needed; we just return.
    return res


PROVIDER = {
    "key": "yolo",
    "label": YOLO_LABEL,
    "create": create,
    "credit": credit,
    "redeem": redeem,
    "auto_create": auto_create_if_needed,
}