#!/usr/bin/env python3
import os, asyncio
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("YOLO_BASE_URL", "https://agent.yolo777.game").rstrip("/")
LOGIN_URL = os.getenv("YOLO_LOGIN_URL", f"{BASE_URL}/admin/auth/login")
YOLO_USER = os.getenv("YOLO_USER") or os.getenv("YOLO_USERNAME")
YOLO_PASS = os.getenv("YOLO_PASS") or os.getenv("YOLO_PASSWORD")
HEADLESS = False

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        # 1. go to /admin
        await page.goto(f"{BASE_URL}/admin", wait_until="domcontentloaded")

        # 2. if we see login, fill
        html = (await page.content()).lower()
        if "password" in html or "auth/login" in page.url:
            # username
            for sel in [
                "input[name='username']",
                "input[placeholder*='User' i]",
                "input[type='text']",
            ]:
                loc = page.locator(sel).first
                if await loc.count():
                    await loc.fill(YOLO_USER)
                    break
            # password
            for sel in [
                "input[name='password']",
                "input[placeholder*='Pass' i]",
                "input[type='password']",
            ]:
                loc = page.locator(sel).first
                if await loc.count():
                    await loc.fill(YOLO_PASS)
                    break
            # submit
            btn = page.locator("button[type='submit'], button:has-text('Login'), button:has-text('Sign in')").first
            await btn.click()
            await page.wait_for_load_state("networkidle")

        # 3. now we are in dashboard → save HTML
        html = await page.content()
        with open("yolo_dashboard.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("saved -> yolo_dashboard.html")

        # 4. also list all iframes
        frames = page.frames
        print("FRAMES:")
        for fr in frames:
            print("-", fr.name, fr.url)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())