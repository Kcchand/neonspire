# gamevault_cookie_login.py
import json, os
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(".env", override=True)

BASE  = os.getenv("GAMEVAULT_BASE_URL", "https://agent.gamevault999.com/api").rstrip("/")
NAME  = os.getenv("GAMEVAULT_AGENT_NAME") or ""
PWD   = os.getenv("GAMEVAULT_AGENT_PWD") or ""

COOKIES_PATH = Path(".gamevault_cookies.json")

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()

        # Use non-/api for the UI page
        ui_base = BASE.replace("/api", "")
        page.goto(ui_base + "/agentLogin.html", wait_until="domcontentloaded")

        # Fill login form
        page.fill('input[name="agent_name"]', NAME)
        page.fill('input[name="agent_pwd"]', PWD)

        print("\n➡️ In the opened browser window:")
        print("   1) Type the 4-digit code shown (if any).")
        print("   2) Click the Login button.")
        print("   3) After it logs in, return here and press the ▶️ resume button in the small Playwright bar.\n")

        page.pause()  # you click Login in the visible browser, then press ▶️ resume

        # Save cookies
        cookies = ctx.cookies()
        COOKIES_PATH.write_text(json.dumps(cookies, indent=2))
        print(f"✅ Saved cookies to {COOKIES_PATH.resolve()}")
        browser.close()

if __name__ == "__main__":
    run()