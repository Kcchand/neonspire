# automation/providers/gameroom.py
import sys
import subprocess
from pathlib import Path

from .base import Provider  # this is the same base you use for juwa/milkyway/etc.

# absolute project root
BASE_DIR = Path(__file__).resolve().parents[2]  # .../crypto_casino

BOT_PATH = BASE_DIR / "automation" / "gameroom_bot.py"
PYTHON = sys.executable  # use current venv python


def _run_bot(args: list[str]) -> tuple[bool, str]:
    """
    run: python automation/gameroom_bot.py <args...>
    return (ok, output)
    """
    cmd = [PYTHON, str(BOT_PATH)] + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(BASE_DIR),
        )
    except Exception as e:
        return False, f"gameroom bot run error: {e}"

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    ok = proc.returncode == 0
    return ok, out.strip()


def gr_create(username: str | None = None) -> dict:
    """
    Let the bot generate username & password (it does that already).
    We just parse stdout line:
      ✅ Gameroom user created successfully | account=... | password=... | balance=...
    """
    ok, out = _run_bot(["create"])
    if not ok:
        return {"ok": False, "log": out}

    # very simple parse
    account = None
    password = None
    balance = None
    for line in out.splitlines():
        line = line.strip()
        if "Gameroom user created successfully" in line:
            # split by |
            parts = [p.strip() for p in line.split("|")]
            for p in parts:
                if p.startswith("account="):
                    account = p.split("=", 1)[1].strip()
                elif p.startswith("password="):
                    password = p.split("=", 1)[1].strip()
                elif p.startswith("balance="):
                    balance = p.split("=", 1)[1].strip()
    return {
        "ok": True,
        "account": account,
        "password": password,
        "balance": balance,
        "raw": out,
    }


def gr_credit(account: str, amount: float | int) -> dict:
    """
    call: python automation/gameroom_bot.py recharge <account> <amount>
    NOTE: gameroom wants integer -> our bot already enforces int, so just pass
    """
    amt_str = str(int(float(amount)))
    ok, out = _run_bot(["recharge", account, amt_str])
    return {
        "ok": ok,
        "raw": out,
    }


def gr_redeem(account: str, amount: float | int) -> dict:
    amt_str = str(int(float(amount)))
    ok, out = _run_bot(["redeem", account, amt_str])
    return {
        "ok": ok,
        "raw": out,
    }


# ---------------------------------------------------------------------------
# provider object (same shape as juwa / milkyway / ultrapanda)
# ---------------------------------------------------------------------------
provider = Provider(
    key="gameroom",
    title="Gameroom",
    auto_create=gr_create,
    credit=gr_credit,
    redeem=gr_redeem,
)