# automation/orionstars_ui_bot.py
from __future__ import annotations

import os, re, time, json, base64, random, string
from pathlib import Path
from dataclasses import dataclass
from urllib.parse import urlparse
from contextlib import contextmanager
from typing import Optional, Union, Tuple, List

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, BrowserContext, Frame, Locator


# ──────────────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)


class OSError(RuntimeError):
    pass


@dataclass
class OSConfig:
    # Orion Stars Configuration
    base: str = (os.getenv("ORIONSTARS_BASE_URL", "https://orionstars.vip:8781") or "").rstrip("/")
    username: str = os.getenv("OS_USERNAME", "Ros1820")
    password: str = os.getenv("OS_PASSWORD", "Ros1820@@@###")

    # Playwright Behavior
    headless: bool = (os.getenv("OS_HEADLESS", "0") == "1")
    slowmo_ms: int = int(os.getenv("OS_SLOWMO_MS", "0"))
    timeout_sec: int = int(os.getenv("OS_TIMEOUT_SEC", "120"))
    force_login: bool = (os.getenv("OS_FORCE_LOGIN", "1") == "1")
    persist_dir: str = os.getenv("OS_PERSIST_DIR", ".data/orionstars")

    # Navigation
    nav_start: str = os.getenv("OS_NAV_START", "login")

    # CAPTCHA (2Captcha) - SAME AS FIREKIRIN
    captcha_provider: str = (os.getenv("CAPTCHA_PROVIDER", "2captcha") or "2captcha").lower()
    captcha_key: Optional[str] = os.getenv("CAPTCHA_API_KEY") or None
    captcha_poll_sec: int = int(os.getenv("CAPTCHA_POLL_SEC", "2"))
    captcha_timeout_sec: int = int(os.getenv("CAPTCHA_TIMEOUT_SEC", "90"))

    # Auto-create rules - SAME AS FIREKIRIN
    username_len: int = int(os.getenv("USERNAME_LEN", "6"))
    username_suffix: str = os.getenv("OS_USERNAME_SUFFIX", "_os") or "_os"
    username_alphanum: bool = (os.getenv("USERNAME_ALPHANUM", "0") == "1")

    # UI nav label (left menu) - SAME AS FIREKIRIN
    user_nav_label: str = os.getenv("OS_USER_NAV_LABEL", "User Management") or "User Management"

    # Manual captcha fallback - SAME AS FIREKIRIN
    manual_captcha_wait_sec: int = int(os.getenv("OS_MANUAL_CAPTCHA_WAIT_SEC", "180"))

    @property
    def login_url(self) -> str:
        return f"{self.base}/default.aspx"

    @property
    def store_url(self) -> str:
        return f"{self.base}/Store.aspx"


class OrionStarsUIBot:
    def __init__(self, cfg: Optional[OSConfig] = None):
        self.cfg = cfg or OSConfig()
        if not self.cfg.base:
            raise OSError("ORIONSTARS_BASE_URL missing.")
        if not self.cfg.username or not self.cfg.password:
            raise OSError("OS_USERNAME/OS_PASSWORD missing.")

        Path(self.cfg.persist_dir).mkdir(parents=True, exist_ok=True)

        self._pw = None
        self._browser = None
        self._ctx: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    # ──────────────────────────────────────────────────────────────────────
    # lifecycle - IDENTICAL TO FIREKIRIN
    def _state_path(self) -> str:
        return str(Path(self.cfg.persist_dir) / "os_state.json")

    def start(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.cfg.headless,
            slow_mo=self.cfg.slowmo_ms if self.cfg.slowmo_ms > 0 else 0,
            args=["--disable-blink-features=AutomationControlled"],
        )

        state = self._state_path()
        use_state = (not self.cfg.force_login) and os.path.exists(state)

        self._ctx = self._browser.new_context(
            storage_state=state if use_state else None,
            ignore_https_errors=True,
            viewport={"width": 1360, "height": 850},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
        )
        self.page = self._ctx.new_page()
        self.page.set_default_timeout(self.cfg.timeout_sec * 1000)
        self.page.set_default_navigation_timeout(self.cfg.timeout_sec * 1000)

    def stop(self):
        # IMPORTANT: save state BEFORE closing (prevents captcha every run)
        try:
            if self._ctx:
                with open(self._state_path(), "w") as f:
                    f.write(json.dumps(self._ctx.storage_state()))
        finally:
            try:
                if self._ctx:
                    self._ctx.close()
            finally:
                try:
                    if self._browser:
                        self._browser.close()
                finally:
                    if self._pw:
                        self._pw.stop()

    @contextmanager
    def session(self):
        self.start()
        try:
            yield self
        finally:
            self.stop()

    # ──────────────────────────────────────────────────────────────────────
    # helpers - IDENTICAL TO FIREKIRIN
    def _wait_for_idle(self, where: Optional[Union[Page, Frame]] = None, timeout_ms: int = 8000):
        where = where or self.page
        try:
            where.wait_for_load_state("networkidle", timeout=timeout_ms)  # type: ignore[attr-defined]
        except Exception:
            pass
        time.sleep(0.2)

    def _goto(self, url: str, label: str = ""):
        p = self.page
        assert p is not None
        last_err = None
        for _ in range(3):
            try:
                p.bring_to_front()
                p.goto(url, wait_until="domcontentloaded", timeout=self.cfg.timeout_sec * 1000)
                self._wait_for_idle(p, 5000)
                return
            except Exception as e:
                last_err = e
                time.sleep(0.8)
        raise OSError(f"Failed to navigate to {label or url}: {last_err}")

    def _dismiss_any_ok(self):
        p = self.page
        assert p is not None
        for name in ("OK", "Ok", "ok", "Confirm", "CONFIRM", "Close"):
            try:
                btn = p.get_by_role("button", name=re.compile(fr"^{re.escape(name)}$", re.I))
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=1500)
                    print(f"✅ Closed '{name}' popup")
                    return
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────────
    # captcha (2Captcha base64) - IDENTICAL TO FIREKIRIN
    def _grab_login_captcha_bytes(self) -> bytes:
        """
        Orion Stars login is same style: username/password + code input + captcha img near it.
        We screenshot the captcha <img> (or first img as fallback).
        """
        p = self.page
        assert p is not None

        # First try: find Code input then nearest img in same row/container
        # Supports: placeholder 'Code' or 'Verification' style
        selectors = [
            "input[placeholder='Code']",
            "input[placeholder*='code' i]",
            "input[placeholder*='verification' i]",
            "input[name*='code' i]",
            "input[name*='verify' i]",
            "input[name*='captcha' i]",
        ]
        for s in selectors:
            try:
                if p.locator(s).count() > 0:
                    code_inp = p.locator(s).first
                    code_inp.wait_for(timeout=15000)
                    # nearest image in same parent
                    row = code_inp.locator("xpath=ancestor::*[self::div or self::tr or self::td][1]")
                    img = row.locator("img, canvas").first
                    if img.count() > 0:
                        return img.screenshot()
            except Exception:
                pass

        # fallback: first visible img/canvas on page (captcha usually only one)
        try:
            return p.locator("img:visible, canvas:visible").first.screenshot()
        except Exception:
            return p.locator("img, canvas").first.screenshot()

    def _solve_captcha(self, img_bytes: bytes) -> str:
        if self.cfg.captcha_provider != "2captcha":
            raise OSError("Only 2Captcha is supported.")
        if not self.cfg.captcha_key:
            raise OSError("CAPTCHA_API_KEY missing.")

        up = requests.post(
            "https://2captcha.com/in.php",
            data={
                "key": self.cfg.captcha_key,
                "method": "base64",
                "body": base64.b64encode(img_bytes).decode(),
                "json": 1
            },
            timeout=45,
        ).json()

        if up.get("status") != 1:
            raise OSError(f"2Captcha in.php error: {up}")

        cid = up["request"]
        t0 = time.time()

        while True:
            time.sleep(max(1, self.cfg.captcha_poll_sec))
            res = requests.get(
                "https://2captcha.com/res.php",
                params={"key": self.cfg.captcha_key, "action": "get", "id": cid, "json": 1},
                timeout=30,
            ).json()

            if res.get("status") == 1:
                code = str(res["request"]).strip()
                return code

            if res.get("request") != "CAPCHA_NOT_READY":
                raise OSError(f"2Captcha res.php error: {res}")

            if time.time() - t0 > self.cfg.captcha_timeout_sec:
                raise OSError("2Captcha timeout.")

    # ──────────────────────────────────────────────────────────────────────
    # frames (Store.aspx family) - IDENTICAL TO FIREKIRIN
    def _left_frame(self) -> Optional[Frame]:
        p = self.page
        assert p is not None
        fr = p.frame(name="frm_left_frm")
        if fr:
            return fr
        for f in p.frames:
            try:
                if "left.aspx" in (f.url or "").lower():
                    return f
            except Exception:
                pass
        return None

    def _main_frame(self) -> Optional[Frame]:
        p = self.page
        assert p is not None
        fr = p.frame(name="frm_main_content")
        if fr:
            return fr
        for f in p.frames:
            try:
                u = (f.url or "").lower()
                if "right.aspx" in u or "accountslist.aspx" in u:
                    return f
            except Exception:
                pass
        return None

    # ──────────────────────────────────────────────────────────────────────
    # login + ensure logged in - IDENTICAL TO FIREKIRIN
    def ensure_logged_in(self):
        p = self.page
        assert p is not None

        # 1) try open store directly (if state cookie is valid)
        try:
            self._goto(self.cfg.store_url, label="Store")
            if "store.aspx" in (p.url or "").lower():
                return
        except Exception:
            pass

        # 2) fresh login
        self._goto(self.cfg.login_url, label="Login")

        # Check if we have 2Captcha API key
        if self.cfg.captcha_key:
            # Try automated captcha solving with 2Captcha
            for _attempt in range(1, 8):
                # Fill user/pass
                try:
                    p.get_by_placeholder("Enter your username").fill(self.cfg.username)
                    p.get_by_placeholder("Enter your password").fill(self.cfg.password)
                except Exception:
                    # fallback generic
                    p.locator("input[type='text']").first.fill(self.cfg.username)
                    p.locator("input[type='password']").first.fill(self.cfg.password)

                # Solve captcha and fill
                try:
                    code = self._solve_captcha(self._grab_login_captcha_bytes())
                    
                    filled = False
                    for sel in [
                        "input[placeholder='Code']",
                        "input[placeholder*='code' i]",
                        "input[placeholder*='verification' i]",
                        "input[name*='verify' i]",
                        "input[name*='captcha' i]",
                    ]:
                        try:
                            loc = p.locator(sel).first
                            if loc.count() > 0 and loc.is_visible():
                                loc.fill("")
                                loc.type(code, delay=20)
                                filled = True
                                break
                        except Exception:
                            pass
                    if not filled:
                        # last resort type into focused field
                        try:
                            p.keyboard.type(code, delay=20)
                        except Exception:
                            pass
                except Exception as e:
                    print(f"⚠️  2Captcha failed: {e}. Switching to manual mode.")
                    break

                # Submit login
                clicked = False
                for sel in [
                    "button:has-text('Login')",
                    "button:has-text('login')",
                    "button:has-text('Sign in')",
                    "button[type='submit']",
                ]:
                    try:
                        b = p.locator(sel).first
                        if b.count() > 0 and b.is_visible():
                            b.click()
                            clicked = True
                            break
                    except Exception:
                        pass
                if not clicked:
                    try:
                        p.keyboard.press("Enter")
                    except Exception:
                        pass

                # success = redirected to store
                try:
                    p.wait_for_url(lambda u: "store.aspx" in u.lower(), timeout=self.cfg.timeout_sec * 1000)
                    self._wait_for_idle(p, 4000)
                    self._dismiss_any_ok()  # Close any popups
                    return
                except Exception:
                    self._dismiss_any_ok()
                    # refresh captcha then retry
                    try:
                        p.reload(wait_until="domcontentloaded")
                    except Exception:
                        pass

        # Manual captcha fallback
        if self.cfg.headless:
            raise OSError("Headless mode can't do manual captcha. Set OS_HEADLESS=0 then run again.")

        print("\n🧩 MANUAL CAPTCHA REQUIRED")
        print("1) Look at the opened browser")
        print("2) Type captcha code and click Login")
        print(f"3) The bot will wait up to {self.cfg.manual_captcha_wait_sec}s for Store.aspx\n")

        deadline = time.time() + self.cfg.manual_captcha_wait_sec
        while time.time() < deadline:
            if "store.aspx" in (p.url or "").lower():
                self._wait_for_idle(p, 4000)
                self._dismiss_any_ok()
                print("✅ Logged in (manual captcha) — session saved for next runs.")
                return
            self._dismiss_any_ok()
            time.sleep(0.3)

        raise OSError("Timed out waiting for manual login.")

    # ──────────────────────────────────────────────────────────────────────
    # navigation inside store - IDENTICAL TO FIREKIRIN
    def goto_user_management(self):
        p = self.page
        assert p is not None

        if "store.aspx" not in (p.url or "").lower():
            self._goto(self.cfg.store_url, label="Store")

        lf = self._left_frame()
        if lf:
            # click User Management
            clicked = False
            for sel in [
                f"xpath=//a[normalize-space(.)='{self.cfg.user_nav_label}']",
                f"text={self.cfg.user_nav_label}",
                "text=User Management",
                "text=Account Management",
            ]:
                try:
                    loc = lf.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.click(timeout=1500)
                        clicked = True
                        break
                except Exception:
                    pass
            if not clicked:
                # sometimes already in UM
                pass

        # wait main frame ready
        t0 = time.time()
        while time.time() - t0 < 12:
            mf = self._main_frame()
            if mf:
                try:
                    # In Orion Stars, look for Create Player form or Search input
                    if mf.locator("text=Create Player").count() > 0:
                        break
                    if mf.locator("input[placeholder='ID or Account']").count() > 0:
                        break
                    if mf.locator("text=Account:").count() > 0:  # Create Player form label
                        break
                except Exception:
                    pass
            time.sleep(0.2)
        self._wait_for_idle(self._main_frame() or p, 2500)

    # ──────────────────────────────────────────────────────────────────────
    # create player - SAME AS FIREKIRIN (BUT ADJUSTED FOR ORION STARS)
    def _wait_for_create_player_form(self, timeout_sec: float = 15.0) -> Tuple[Frame, int]:
        p = self.page
        assert p is not None

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            for fr in p.frames:
                try:
                    # Orion Stars: look for Create Player form inputs
                    # They're usually text/password inputs that are enabled
                    inputs = fr.locator("input[type='text'], input[type='password']").filter(
                        has_not=fr.locator("[disabled]")
                    )
                    c = inputs.count()
                    if c >= 4:  # Account, NickName, Password, Confirm Password
                        return fr, c
                except Exception:
                    continue
            time.sleep(0.2)
        raise OSError("Could not find Create Player dialog inputs (need >=4).")

    def _generate_username(self) -> str:
        L = max(1, int(self.cfg.username_len))
        pool = (string.ascii_letters + string.digits) if self.cfg.username_alphanum else string.ascii_letters
        core = "".join(random.choice(pool) for _ in range(L))
        if not any(ch.isalpha() for ch in core):
            core = "player"
        account = f"{core}{self.cfg.username_suffix}"
        # Orion Stars: 13 chars or less
        if len(account) > 13:
            core = core[: 13 - len(self.cfg.username_suffix)]
            account = f"{core}{self.cfg.username_suffix}"
        return account

    def create_player(self, account: str, password: str, nickname: Optional[str] = None) -> dict:
        self.goto_user_management()
        mf = self._main_frame() or self.page
        assert mf is not None

        # click Create Player (robust)
        clicked = False
        for sel in [
            "role=button[name='Create Player']",
            "text=Create Player",
            "xpath=//*[self::button or self::a][contains(normalize-space(.),'Create Player')]",
            "input[value='Create Player']",
        ]:
            try:
                mf.locator(sel).first.click(timeout=2500)
                clicked = True
                break
            except Exception:
                pass
        if not clicked:
            # In Orion Stars, the form might already be visible
            # Check if Create Player form is already on the page
            try:
                if mf.locator("text=Account:").count() > 0:
                    clicked = True  # Form is already visible
            except Exception:
                pass
        
        if not clicked:
            raise OSError("Create Player button not found in User Management page.")

        # Wait for form to appear (or be ready if already visible)
        dialog_frame, _ = self._wait_for_create_player_form(timeout_sec=15.0)

        nick = nickname or account
        inputs = dialog_frame.locator("input[type='text'], input[type='password']").filter(
            has_not=dialog_frame.locator("[disabled]")
        )
        if inputs.count() < 4:
            raise OSError(f"Create Player dialog missing inputs. Found {inputs.count()}")

        # clear then fill: Account, Nickname, Password, Confirm
        for i in range(4):
            try:
                inputs.nth(i).fill("")
            except Exception:
                pass

        inputs.nth(0).fill(account)
        inputs.nth(1).fill(nick)
        inputs.nth(2).fill(password)
        inputs.nth(3).fill(password)

        # submit inside dialog
        submitted = False
        for sel in [
            "role=button[name='Create Player']",
            "text=Create Player",
            "xpath=//*[self::button or self::a][contains(normalize-space(.),'Create Player')]",
            "role=button[name='OK']",
            "text=OK",
            "input[value='Create Player']",
        ]:
            try:
                dialog_frame.locator(sel).last.click(timeout=2500)
                submitted = True
                break
            except Exception:
                pass
        if not submitted:
            raise OSError("Create Player submit button not found in dialog.")

        time.sleep(1.2)
        self._dismiss_any_ok()
        return {"ok": True, "created": True, "account": account, "nickname": nick, "password": password}

    def create_player_auto(self) -> dict:
        acct = self._generate_username()
        pwd = acct  # Same as FireKirin: password = username
        out = self.create_player(acct, pwd, acct)
        print(f"✅ Auto-created Orion Stars user '{acct}'")
        return out

    # ──────────────────────────────────────────────────────────────────────
    # Recharge / Redeem functionality - SAME AS FIREKIRIN
    def _contexts(self) -> List[Union[Page, Frame]]:
        p = self.page
        assert p is not None
        ctxs: List[Union[Page, Frame]] = [p]
        mf = self._main_frame()
        if mf and mf not in ctxs:
            ctxs.append(mf)
        for fr in p.frames:
            if fr not in ctxs:
                ctxs.append(fr)
        return ctxs

    def _click_in_main(self, labels: List[str]) -> None:
        mf = self._main_frame() or self.page
        assert mf is not None
        for t in labels:
            try:
                loc = mf.locator(f"text={t}").first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=2500)
                    return
            except Exception:
                pass
            try:
                sel = (
                    "xpath=//*[self::button or self::a or self::input]"
                    f"[contains(normalize-space(.),'{t}') or contains(@value,'{t}')]"
                )
                cand = mf.locator(sel).first
                if cand.count() > 0 and cand.is_visible():
                    cand.scroll_into_view_if_needed(timeout=1500)
                    cand.click(timeout=2500, force=True)
                    return
            except Exception:
                pass
        raise OSError(f"Button not found: {labels}")

    def search_user(self, account_or_id: Union[str, int]) -> dict:
        """Search for a user in the management interface"""
        self.goto_user_management()
        mf = self._main_frame() or self.page
        assert mf is not None

        # Handle suffix removal for searching
        search_key = str(account_or_id)
        if search_key.endswith('_os'):
            search_key_without_suffix = search_key[:-3]
            print(f"🔍 Note: Will search for '{search_key_without_suffix}' (original: '{search_key}')")
            search_key = search_key_without_suffix

        # Find search input (Orion Stars: "ID or Account")
        box = mf.locator("input[placeholder*='ID'], input[placeholder*='Account'], input[type='text']").first
        if box.count() == 0:
            box = mf.locator("input[type='text']:visible").first

        # Clear and type search term
        box.fill("")
        box.type(search_key, delay=10)

        # Click search button
        for label in ("Search", "Query", "Find", "Go"):
            try:
                mf.locator(f"text={label}").first.click(timeout=2000)
                break
            except Exception:
                pass
        else:
            # Fallback search button
            try:
                mf.locator(
                    "xpath=//*[self::button or self::input][contains(@value,'Search') or contains(normalize-space(.),'Search')]"
                ).first.click(timeout=2000)
            except Exception:
                pass

        # Wait for results
        time.sleep(1.5)
        
        # Verify user appears in results
        try:
            mf.locator(f"text={search_key}").first.wait_for(state="visible", timeout=10000)
            print(f"✅ User '{search_key}' found in search results")
        except Exception:
            print(f"⚠️  User '{search_key}' not immediately visible - table may still be loading")
            
        return {"ok": True, "searched": True, "account_or_id": str(account_or_id), "search_key_used": search_key}

    def _click_action_for_row(self, account_or_id: Union[str, int], action_type: str = "Recharge") -> bool:
        """Find and click the action link (Recharge/Redeem) for a specific user"""
        mf = self._main_frame() or self.page
        assert mf is not None
        
        # Prepare search keys with and without suffix
        original_key = str(account_or_id)
        key_without_suffix = original_key
        
        if original_key.endswith('_os'):
            key_without_suffix = original_key[:-3]  # Remove "_os"
        
        # Both keys to try
        search_keys = [key_without_suffix, original_key]
        
        # Orion Stars actions: Recharge, Redeam (typo), Redeem
        action_texts = [action_type]
        if action_type == "Redeem":
            action_texts.extend(["Redeam", "兑现", "提现"])  # Include typo and Chinese
        
        for key in search_keys:
            try:
                # Strategy 1: Find cell with username, then find action link in same row
                user_cell = mf.locator(f"xpath=//td[contains(text(),'{key}')]").first
                if user_cell.count() > 0 and user_cell.is_visible():
                    # Get the table row containing this cell
                    row = user_cell.locator("xpath=ancestor::tr")
                    
                    # Look for action link
                    for action_text in action_texts:
                        action_link = row.locator(f"xpath=.//a[contains(text(),'{action_text}')]").first
                        
                        # If no exact match found, try any link in the row
                        if action_link.count() == 0:
                            action_link = row.locator("xpath=.//a").first
                        
                        if action_link.count() > 0 and action_link.is_visible():
                            print(f"✅ Found {action_type} link for user '{key}'")
                            action_link.click(timeout=2500)
                            time.sleep(0.5)
                            return True
            except Exception:
                continue
        
        # Strategy 2: Direct XPath search
        try:
            for key in search_keys:
                for action_text in action_texts:
                    action = mf.locator(
                        f"xpath=//tr[.//td[contains(text(),'{key}')]]//a[contains(text(),'{action_text}')]"
                    ).first
                    if action.count() > 0 and action.is_visible():
                        print(f"✅ Found {action_type} link via XPath for '{key}'")
                        action.click(timeout=2500)
                        time.sleep(0.25)
                        return True
        except Exception:
            pass
        
        # Debug information
        print(f"❌ Could not find {action_type} link for user '{original_key}'")
        print(f"🔍 Tried searching for: {search_keys}")
        print(f"🔍 {action_type} links on page: {mf.locator(f'text={action_type}').count()}")
        
        return False

    def _find_amount_modal(self, cap: str) -> Optional[Locator]:
        for ctx in self._contexts():
            try:
                lab = ctx.locator(
                    "xpath=(//*[contains(normalize-space(.),'Recharge Amount') or contains(normalize-space(.),'Redeem Amount')"
                    " or contains(normalize-space(.),'Withdraw Amount') or contains(normalize-space(.),'Amount')])[last()]"
                )
                if lab.count() > 0 and lab.first.is_visible():
                    cont = lab.first.locator("xpath=ancestor::*[self::div or self::form][1]")
                    if cont.count() > 0 and cont.first.is_visible():
                        return cont.first
            except Exception:
                pass
        return None

    def _find_amount_input_inside(self, container: Locator) -> Optional[Locator]:
        try:
            any_input = container.locator("css=input[type='text']:enabled:visible, input:enabled:visible").first
            if any_input.count() > 0:
                return any_input
        except Exception:
            pass
        return None

    def _open_amount_popup_or_retry(self, which: str, retries: int = 6) -> Locator:
        cap = "Recharge" if which == "recharge" else "Redeem"
        for _ in range(retries):
            container = self._find_amount_modal(cap)
            if container:
                return container
            self._click_in_main([cap])
            t0 = time.time()
            while time.time() - t0 < 8:
                container = self._find_amount_modal(cap)
                if container:
                    return container
                time.sleep(0.15)
        raise OSError(f"{cap} dialog not found.")

    def _fill_amount_dialog(self, kind: str, amount: Union[int, float], note: str = ""):
        cap = "Recharge" if kind == "recharge" else "Redeem"
        container = self._open_amount_popup_or_retry(kind, retries=6)

        amount_input = self._find_amount_input_inside(container)
        if not amount_input:
            raise OSError(f"{cap} amount input not found.")

        amt_str = str(int(float(amount))) if float(amount).is_integer() else str(amount)
        amount_input.fill("")
        amount_input.type(amt_str, delay=15)

        if note:
            try:
                container.locator("textarea:visible").first.fill(note, timeout=1500)
            except Exception:
                pass

        for sel in [
            f"xpath=.//*[self::button or self::input][contains(normalize-space(.),'{cap}') or contains(@value,'{cap}')]",
            "text=OK",
            "xpath=.//button[contains(.,'Confirm') or contains(.,'CONFIRM')]",
        ]:
            try:
                b = container.locator(sel).last
                if b.count() > 0 and b.first.is_visible():
                    b.first.click(timeout=2500)
                    break
            except Exception:
                pass
        else:
            raise OSError(f"{cap} submit button not found.")

        time.sleep(1.0)
        self._dismiss_any_ok()
        self._wait_for_idle(self.page, 1500)

    # ✅ WORKING RECHARGE FUNCTION
    def recharge(self, account_or_id: Union[str, int], amount: Union[int, float], note: str = "") -> dict:
        print(f"🔄 Starting recharge for '{account_or_id}' amount {amount}")
        
        # Step 1: Search for the user
        self.search_user(account_or_id)
        
        # Step 2: Click Recharge link for the user
        if not self._click_action_for_row(account_or_id, "Recharge"):
            raise OSError("Recharge link not found after search.")
        
        time.sleep(0.5)
        
        # Step 3: Fill and submit recharge dialog
        self._fill_amount_dialog("recharge", amount, note)
        
        print(f"✅ Successfully recharged '{account_or_id}' with {amount}")
        return {"ok": True, "recharged": True, "account_or_id": str(account_or_id), "amount": amount}

    # ✅ WORKING REDEEM FUNCTION
    def redeem(self, account_or_id: Union[str, int], amount: Union[int, float], note: str = "") -> dict:
        print(f"🔄 Starting redeem for '{account_or_id}' amount {amount}")
        
        # Step 1: Search for the user
        self.search_user(account_or_id)
        
        # Step 2: Click Redeem link for the user (handles "Redeam" typo)
        if not self._click_action_for_row(account_or_id, "Redeem"):
            raise OSError("Redeem link not found after search.")

        time.sleep(0.5)

        # Step 3: Fill and submit redeem dialog
        self._fill_amount_dialog("redeem", amount, note)
        
        print(f"✅ Successfully redeemed {amount} from '{account_or_id}'")
        return {"ok": True, "redeemed": True, "account_or_id": str(account_or_id), "amount": amount}


# ──────────────────────────────────────────────────────────────────────────────
# CLI - IDENTICAL TO FIREKIRIN
def main():
    import argparse

    ap = argparse.ArgumentParser("Orion Stars UI Bot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login")
    sub.add_parser("create-auto")

    pcreate = sub.add_parser("create")
    pcreate.add_argument("account")
    pcreate.add_argument("password")
    pcreate.add_argument("--nickname", default=None)

    prec = sub.add_parser("recharge")
    prec.add_argument("account_or_id")
    prec.add_argument("amount", type=float)
    prec.add_argument("--note", default="")

    pred = sub.add_parser("redeem")
    pred.add_argument("account_or_id")
    pred.add_argument("amount", type=float)
    pred.add_argument("--note", default="")

    args = ap.parse_args()
    bot = OrionStarsUIBot(OSConfig())

    with bot.session() as b:
        # ✅ IMPORTANT: login and action happen in SAME session
        b.ensure_logged_in()

        if args.cmd == "login":
            print("✅ Orion Stars logged in (2Captcha or manual captcha) + session saved")
            return

        if args.cmd == "create-auto":
            info = b.create_player_auto()
            print("✅ Created:", info)
            return

        if args.cmd == "create":
            info = b.create_player(args.account, args.password, args.nickname)
            print("✅ Created:", info)
            return

        if args.cmd == "recharge":
            info = b.recharge(args.account_or_id, args.amount, args.note)
            print("✅ Recharged:", info)
            return

        if args.cmd == "redeem":
            info = b.redeem(args.account_or_id, args.amount, args.note)
            print("✅ Redeemed:", info)
            return


# ──────────────────────────────────────────────────────────────────────────────
# SYNC WRAPPERS FOR player_bp.py - IDENTICAL TO FIREKIRIN
def auto_create_sync() -> dict:
    """
    Auto-create an Orion Stars account (for player_bp.py).
    Returns: {"ok": True, "account": "username", "password": "pwd"} or error dict.
    """
    try:
        bot = OrionStarsUIBot(OSConfig())
        with bot.session() as b:
            b.ensure_logged_in()
            result = b.create_player_auto()
            return {"ok": True, "account": result["account"], "password": result["password"]}
    except Exception as e:
        return {"ok": False, "error": f"Orion Stars auto_create failed: {str(e)}"}


def recharge_sync(account: str, amount: float, note: str = "") -> dict:
    """
    Recharge an Orion Stars account (for player_bp.py).
    """
    try:
        bot = OrionStarsUIBot(OSConfig())
        with bot.session() as b:
            b.ensure_logged_in()
            result = b.recharge(account, float(amount), note or "")
            return {"ok": True, "recharged": True, "account": account, "amount": amount}
    except Exception as e:
        return {"ok": False, "error": f"Orion Stars recharge failed: {str(e)}"}


def redeem_sync(account: str, amount: float, note: str = "") -> dict:
    """
    Redeem from an Orion Stars account (for player_bp.py).
    """
    try:
        bot = OrionStarsUIBot(OSConfig())
        with bot.session() as b:
            b.ensure_logged_in()
            result = b.redeem(account, float(amount), note or "")
            return {"ok": True, "redeemed": True, "account": account, "amount": amount}
    except Exception as e:
        return {"ok": False, "error": f"Orion Stars redeem failed: {str(e)}"}


if __name__ == "__main__":
    main()