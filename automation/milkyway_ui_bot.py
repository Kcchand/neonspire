# automation/milkyway_ui_bot.py

from __future__ import annotations

import os, re, time, json, base64, random, string
from pathlib import Path
from urllib.parse import urlparse
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, Union, List, Tuple

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, BrowserContext, Frame, Locator

# ──────────────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)

# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class MWConfig:
    base: str = (os.getenv("MILKYWAY_BASE", "https://milkywayapp.xyz:8781") or "").rstrip("/")
    username: str = os.getenv("MW_USERNAME", "") or os.getenv("MILKYWAY_USERNAME", "")
    password: str = os.getenv("MW_PASSWORD", "") or os.getenv("MILKYWAY_PASSWORD", "")
    headless: bool = (os.getenv("MW_HEADLESS", "1") == "1")
    persist_dir: str = os.getenv("MW_PERSIST_DIR", ".data/milkyway")
    timeout_sec: int = int(os.getenv("MW_TIMEOUT_SEC", "40"))
    proxy: Optional[str] = os.getenv("MW_PROXY") or None
    slowmo_ms: int = int(os.getenv("MW_SLOWMO_MS", "0"))

    # CAPTCHA (2Captcha)
    captcha_provider: str = (os.getenv("CAPTCHA_PROVIDER", "2captcha") or "2captcha").lower()
    captcha_key: Optional[str] = os.getenv("CAPTCHA_API_KEY") or None
    captcha_poll_sec: int = int(os.getenv("CAPTCHA_POLL_SEC", "2"))
    captcha_timeout_sec: int = int(os.getenv("CAPTCHA_TIMEOUT_SEC", "90"))

    # Auto-create rules
    username_len: int = int(os.getenv("USERNAME_LEN", "6"))
    username_suffix: str = os.getenv("MW_USERNAME_SUFFIX", "_mw")
    username_alphanum: bool = (os.getenv("USERNAME_ALPHANUM", "0") == "1")

    # UI nav label
    user_nav_label: str = os.getenv("MW_USER_NAV_LABEL", "User Management") or "User Management"

    @property
    def login_url(self) -> str:
        return f"{self.base}/default.aspx"

    @property
    def store_url(self) -> str:
        return f"{self.base}/Store.aspx"


class MWError(RuntimeError):
    pass


# ──────────────────────────────────────────────────────────────────────────────
class MilkywayUIBot:
    def __init__(self, cfg: Optional[MWConfig] = None):
        self.cfg = cfg or MWConfig()
        if not self.cfg.base:
            raise MWError("MILKYWAY_BASE missing.")
        if not self.cfg.username or not self.cfg.password:
            raise MWError("MW_USERNAME/MW_PASSWORD missing.")
        Path(self.cfg.persist_dir).mkdir(parents=True, exist_ok=True)
        self._pw = None
        self._browser = None
        self._ctx: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self):
        self._pw = sync_playwright().start()
        launch_kwargs = {
            "headless": self.cfg.headless,
            "args": ["--disable-blink-features=AutomationControlled"],
            "slow_mo": self.cfg.slowmo_ms if self.cfg.slowmo_ms > 0 else 0,
        }
        proxy_dict = self._safe_proxy_dict(self.cfg.proxy)
        if proxy_dict:
            launch_kwargs["proxy"] = proxy_dict

        self._browser = self._pw.chromium.launch(**launch_kwargs)

        state = self._state_path()
        use_state = (os.getenv("MW_FORCE_LOGIN", "0") != "1") and os.path.exists(state)

        self._ctx = self._browser.new_context(
            storage_state=state if use_state else None,
            ignore_https_errors=True,
            viewport={"width": 1360, "height": 850},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36")
        )
        self.page = self._ctx.new_page()
        self.page.set_default_timeout(self.cfg.timeout_sec * 1000)
        self.page.set_default_navigation_timeout(self.cfg.timeout_sec * 1000)

    def stop(self):
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

    def _state_path(self) -> str:
        return str(Path(self.cfg.persist_dir) / "mw_state.json")

    @contextmanager
    def session(self):
        self.start()
        try:
            yield self
        finally:
            self.stop()

    # ── helpers ──────────────────────────────────────────────────────────────
    def _safe_proxy_dict(self, value: Optional[str]):
        if not value:
            return None
        v = value.strip()
        if not v:
            return None
        if "://" not in v:
            v = "http://" + v
        u = urlparse(v)
        if not (u.scheme and (u.hostname or u.netloc)):
            return None
        server = f"{u.scheme}://{u.hostname or u.netloc}"
        if u.port:
            server += f":{u.port}"
        return {"server": server}

    def _ensure_page_open(self):
        try:
            _ = self.page.url  # type: ignore[union-attr]
        except Exception:
            if self._ctx is None:
                raise MWError("Browser context is closed.")
            self.page = self._ctx.new_page()
            self.page.set_default_timeout(self.cfg.timeout_sec * 1000)
            self.page.set_default_navigation_timeout(self.cfg.timeout_sec * 1000)

    def _wait_for_idle(self, where: Optional[Union[Page, Frame]] = None, timeout_ms: int = 8000):
        where = where or self.page
        try:
            where.wait_for_load_state("networkidle", timeout=timeout_ms)  # type: ignore[attr-defined]
        except Exception:
            pass
        time.sleep(0.2)

    # ── captcha helpers ──────────────────────────────────────────────────────
    def _grab_login_captcha_bytes(self) -> bytes:
        # best effort: captcha near "Code" input on login page
        p = self.page
        assert p is not None
        try:
            p.wait_for_selector("input[placeholder='Code']", timeout=30000)
            row = p.get_by_placeholder("Code").locator("xpath=..")
            img = row.locator("img").first
            return img.screenshot()
        except Exception:
            return p.locator("img").first.screenshot()

    def _solve_captcha(self, img_bytes: bytes) -> str:
        if self.cfg.captcha_provider != "2captcha":
            raise MWError("Only 2Captcha is supported.")
        if not self.cfg.captcha_key:
            raise MWError("CAPTCHA_API_KEY missing.")
        up = requests.post(
            "https://2captcha.com/in.php",
            data={"key": self.cfg.captcha_key, "method": "base64",
                  "body": base64.b64encode(img_bytes).decode(), "json": 1},
            timeout=45,
        ).json()
        if up.get("status") != 1:
            raise MWError(f"2Captcha in.php error: {up}")
        cid = up["request"]
        t0 = time.time()
        while True:
            time.sleep(max(1, self.cfg.captcha_poll_sec))
            res = requests.get(
                "https://2captcha.com/res.php",
                params={"key": self.cfg.captcha_key, "action": "get", "id": cid, "json": 1},
                timeout=30
            ).json()
            if res.get("status") == 1:
                code_raw = str(res["request"]).strip()
                digits = "".join(ch for ch in code_raw if ch.isdigit())
                if not digits:
                    raise MWError(f"2Captcha returned no digits: {code_raw}")
                return digits
            if res.get("request") != "CAPCHA_NOT_READY":
                raise MWError(f"2Captcha res.php error: {res}")
            if time.time() - t0 > self.cfg.captcha_timeout_sec:
                raise MWError("2Captcha timeout.")

    def _dismiss_any_ok(self):
        p = self.page
        assert p is not None
        try:
            okbtn = p.get_by_role("button", name=re.compile(r"^ok$", re.I))
            if okbtn.count() > 0 and okbtn.first.is_visible():
                okbtn.first.click(timeout=1500)
        except Exception:
            pass

    # ── navigation & login ───────────────────────────────────────────────────
    def _goto(self, url: str, label: str = ""):
        p = self.page
        assert p is not None
        last_err = None
        for _ in range(3):
            try:
                self._ensure_page_open()
                p.bring_to_front()
                p.goto(url, wait_until="domcontentloaded", timeout=self.cfg.timeout_sec * 1000)
                self._wait_for_idle(p, 5000)
                if not p.url.startswith("about:blank"):
                    return
            except Exception as e:
                last_err = e
                time.sleep(0.8)
                try:
                    _ = p.url
                except Exception:
                    self._ensure_page_open()
        raise MWError(f"Failed to navigate to {label or url}: {last_err}")

    def ensure_logged_in(self):
        p = self.page
        assert p is not None

        # Try cookie state first
        try:
            self._goto(self.cfg.store_url, label="Store")
            if "store.aspx" in (p.url or "").lower():
                return
        except Exception:
            pass

        # Fresh login
        self._goto(self.cfg.login_url, label="Login")

        for _round in range(2):
            try:
                self._ensure_page_open()

                p.get_by_placeholder("Enter your username").fill(self.cfg.username)
                p.get_by_placeholder("Enter your password").fill(self.cfg.password)

                for _attempt in range(1, 8):
                    code = self._solve_captcha(self._grab_login_captcha_bytes())
                    p.get_by_placeholder("Code").fill(code)
                    p.get_by_role("button", name=re.compile("login", re.I)).click()

                    try:
                        p.wait_for_url(lambda u: "store.aspx" in u.lower(), timeout=self.cfg.timeout_sec * 1000)
                        self._wait_for_idle(p, 4000)
                        return
                    except Exception:
                        self._dismiss_any_ok()
                        continue
            except Exception:
                try:
                    self._goto(self.cfg.login_url, label="Login")
                except Exception:
                    pass

        raise MWError("Login failed after multiple captcha retries.")

    # ── frames (Store.aspx) ──────────────────────────────────────────────────
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

    # ── navigation inside Store ──────────────────────────────────────────────
    def goto_user_management(self):
        p = self.page
        assert p is not None

        if "store.aspx" not in (p.url or "").lower():
            self._goto(self.cfg.store_url, label="Store")

        lf = self._left_frame()
        if lf:
            clicked = False
            for sel in [
                f"xpath=//a[normalize-space(.)='{self.cfg.user_nav_label}']",
                f"text={self.cfg.user_nav_label}",
                "text=User Management",
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
                # ignore; sometimes already on User Management
                pass

        # wait main
        t0 = time.time()
        mf = None
        while time.time() - t0 < 10:
            mf = self._main_frame()
            if mf:
                try:
                    if mf.locator("input[placeholder='ID or Account']").count() > 0 or mf.locator("text=Create Player").count() > 0:
                        break
                except Exception:
                    pass
            time.sleep(0.2)
        self._wait_for_idle(mf or p, 3000)

    # ──────────────────────────────────────────────────────────────────────────
    # ✅ CREATE PLAYER (from your first working create logic style)
    # ──────────────────────────────────────────────────────────────────────────

    def _wait_for_create_player_form(self, timeout_sec: float = 15.0) -> Tuple[Frame, int]:
        """
        Wait until a frame appears that has at least 4 editable inputs (text/password).
        That frame is the Create Player dialog.
        """
        p = self.page
        assert p is not None
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            for fr in p.frames:
                try:
                    loc = fr.locator("input[type='text'], input[type='password']").filter(
                        has_not=fr.locator("[disabled]")
                    )
                    c = loc.count()
                    if c >= 4:
                        return fr, c
                except Exception:
                    continue
            time.sleep(0.2)
        raise MWError("Could not find Create Player dialog inputs (need >=4).")

    def _generate_username(self) -> str:
        L = max(1, int(self.cfg.username_len))
        pool = (string.ascii_letters + string.digits) if self.cfg.username_alphanum else string.ascii_letters
        core = "".join(random.choice(pool) for _ in range(L))
        if not any(ch.isalpha() for ch in core):
            core = "player"
        account = f"{core}{self.cfg.username_suffix}"
        if len(account) > 13:
            core = core[: 13 - len(self.cfg.username_suffix)]
            account = f"{core}{self.cfg.username_suffix}"
        return account

    def create_player(self, account: str, password: str, nickname: Optional[str] = None) -> dict:
        """
        Manual create with provided account/password/nickname.
        """
        self.goto_user_management()
        mf = self._main_frame() or self.page
        assert mf is not None

        # open dialog
        try:
            mf.get_by_role("button", name="Create Player").click()
        except Exception:
            try:
                mf.get_by_text("Create Player", exact=False).click()
            except Exception:
                # last resort
                mf.locator("text=Create Player").first.click()

        dialog, _ = self._wait_for_create_player_form(timeout_sec=15.0)

        nick = nickname or account
        inputs = dialog.locator("input[type='text'], input[type='password']").filter(
            has_not=dialog.locator("[disabled]")
        )
        if inputs.count() < 4:
            raise MWError(f"Create Player dialog missing inputs. Found {inputs.count()}")

        # clear
        for i in range(min(inputs.count(), 6)):
            try:
                inputs.nth(i).fill("")
            except Exception:
                pass

        # fill (Account, Nickname, Password, Confirm)
        inputs.nth(0).fill(account)
        inputs.nth(1).fill(nick)
        inputs.nth(2).fill(password)
        inputs.nth(3).fill(password)

        # submit inside dialog
        try:
            dialog.get_by_role("button", name="Create Player").click()
        except Exception:
            dialog.get_by_text("Create Player", exact=False).click()

        time.sleep(1.2)
        self._dismiss_any_ok()
        return {"created": True, "account": account, "nickname": nick, "password": password}

    def create_player_auto(self) -> dict:
        account = self._generate_username()
        password = account
        nickname = account
        out = self.create_player(account, password, nickname)
        print(f"✅ Auto-created user '{account}'")
        return out

    # ──────────────────────────────────────────────────────────────────────────
    # ✅ RECHARGE / REDEEM (from your second working code)
    # ──────────────────────────────────────────────────────────────────────────

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

    def _find_amount_modal(self, cap: str) -> Optional[Locator]:
        for ctx in self._contexts():
            try:
                btn = ctx.locator(
                    f"xpath=(//*[self::button or self::input][@type='button' or @type='submit' or not(@type)]"
                    f"[contains(normalize-space(.),'{cap}') or contains(@value,'{cap}')])[last()]"
                )
                if btn.count() > 0 and btn.first.is_visible():
                    cont = btn.first.locator("xpath=ancestor::*[self::div or self::form][1]")
                    if cont.count() > 0 and cont.first.is_visible():
                        return cont.first
            except Exception:
                pass

        for ctx in self._contexts():
            try:
                lab = ctx.locator(
                    "xpath=(//*[contains(normalize-space(.),'Recharge Amount') or contains(normalize-space(.),'Redeem Amount')"
                    " or contains(normalize-space(.),'Withdraw Amount')])[last()]"
                )
                if lab.count() > 0 and lab.first.is_visible():
                    cont = lab.first.locator("xpath=ancestor::*[self::div or self::form][1]")
                    if cont.count() > 0 and cont.first.is_visible():
                        return cont.first
            except Exception:
                pass
        return None

    def _find_amount_input_inside(self, container: Locator) -> Optional[Locator]:
        for lbl in ["Recharge Amount", "Redeem Amount", "Withdraw Amount", "Amount"]:
            try:
                by_label = container.locator(
                    f"xpath=(.//*[self::label or self::td or self::*][contains(normalize-space(.),'{lbl}')])[1]"
                    "/following::input[1]"
                )
                if by_label.count() > 0 and by_label.first.is_enabled() and by_label.first.is_visible():
                    return by_label.first
            except Exception:
                pass
        try:
            any_input = container.locator("css=input[type='text']:enabled:visible")
            if any_input.count() > 0:
                return any_input.first
        except Exception:
            pass
        return None

    def _click_action_in_main(self, labels: List[str]) -> None:
        mf = self._main_frame() or self.page
        assert mf is not None
        for t in labels:
            # role button first
            try:
                btn = mf.get_by_role("button", name=re.compile(fr"^{re.escape(t)}$", re.I))
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=2000)
                    return
            except Exception:
                pass
            # xpath fallback
            try:
                sel = (
                    "xpath=//*[self::button or self::a or self::input]"
                    f"[contains(normalize-space(.),'{t}') or contains(@value,'{t}')]"
                )
                cand = mf.locator(sel)
                if cand.count() > 0 and cand.first.is_visible():
                    cand.first.scroll_into_view_if_needed(timeout=1200)
                    cand.first.click(timeout=2000, force=True)
                    return
            except Exception:
                pass
        raise MWError(f"{'/'.join(labels)} button not found.")

    def _wait_for_message_and_click_ok(self, timeout_s: int = 8) -> Optional[str]:
        deadline = time.time() + timeout_s
        last_text = None
        while time.time() < deadline:
            for ctx in self._contexts():
                try:
                    box = ctx.locator(
                        "xpath=(//*[@role='dialog' or contains(@class,'message') or contains(@class,'alert') or contains(@class,'modal')])[last()]"
                    )
                    if box.count() > 0 and box.first.is_visible():
                        text = box.first.inner_text().strip()
                        if text:
                            last_text = text
                        ok = box.first.get_by_role("button", name=re.compile(r"^ok$", re.I))
                        if ok.count() > 0 and ok.first.is_visible():
                            ok.first.click(timeout=1500)
                            return last_text
                except Exception:
                    pass
            time.sleep(0.15)
        return last_text

    def _open_amount_popup_or_retry(self, which: str, retries: int = 6) -> Locator:
        cap = "Recharge" if which == "recharge" else "Redeem"
        for attempt in range(1, retries + 1):
            container = self._find_amount_modal(cap)
            if container:
                return container

            # click top action (Recharge/Redeem)
            self._click_action_in_main([cap])
            t0 = time.time()
            container = None
            while time.time() - t0 < 8 and container is None:
                container = self._find_amount_modal(cap)
                if container is None:
                    time.sleep(0.15)

            if container is None:
                if attempt == retries:
                    raise MWError(f"{cap} dialog not found.")
                continue

            return container
        raise MWError(f"{cap} dialog not found.")

    def _fill_amount_dialog(self, kind: str, amount: Union[int, float], note: str = ""):
        cap = "Recharge" if kind == "recharge" else "Redeem"
        container = self._open_amount_popup_or_retry(kind, retries=6)

        amount_input = self._find_amount_input_inside(container)
        if not amount_input:
            raise MWError(f"{cap} amount input not found.")

        try:
            f = float(amount)
            amt_str = str(int(f)) if f.is_integer() else str(amount)
        except Exception:
            amt_str = str(amount)

        try:
            amount_input.click(timeout=1200)
        except Exception:
            pass
        amount_input.fill("")
        amount_input.type(amt_str, delay=20)

        if note:
            try:
                container.locator("textarea:visible").first.fill(note, timeout=1500)
            except Exception:
                pass

        submitted = False
        for sel in [
            f"xpath=.//*[self::button or self::input][contains(normalize-space(.),'{cap}') or contains(@value,'{cap}')]",
            "xpath=.//button[normalize-space(.)='OK']",
            "xpath=.//input[@type='submit']",
        ]:
            try:
                b = container.locator(sel).last
                if b.count() > 0 and b.first.is_visible():
                    b.first.click(timeout=1800)
                    submitted = True
                    break
            except Exception:
                pass
        if not submitted:
            raise MWError(f"{cap} submit button not found.")

        self._wait_for_message_and_click_ok(timeout_s=10)
        self._wait_for_idle(self.page, 1500)

    def search_user(self, account_or_id: Union[str, int]) -> dict:
        self.goto_user_management()
        mf = self._main_frame() or self.page
        assert mf is not None

        box = mf.locator("input[placeholder='ID or Account']").first
        if box.count() == 0:
            t_inputs = mf.locator("input[type='text']:visible")
            if t_inputs.count() > 0:
                box = t_inputs.nth(0)

        box.fill("")
        box.type(str(account_or_id), timeout=3000)

        clicked = False
        for label in ("Search", "Query", "Find", "Go"):
            try:
                btn = mf.get_by_role("button", name=re.compile(label, re.I))
                if btn.count() > 0:
                    btn.first.click(timeout=1500)
                    clicked = True
                    break
            except Exception:
                pass

        if not clicked:
            try:
                mf.locator(
                    "xpath=//*[self::button or self::input][contains(@value,'Search') or contains(normalize-space(.),'Search')]"
                ).first.click(timeout=1500)
            except Exception:
                pass

        time.sleep(0.6)
        return {"selected": True, "account_or_id": str(account_or_id)}

    def _click_update_for_row(self, account_or_id: Union[str, int]) -> bool:
        mf = self._main_frame() or self.page
        assert mf is not None
        key = str(account_or_id)
        try:
            upd = mf.locator(
                f"xpath=//table//tr[.//td[contains(normalize-space(.),'{key}')]]//a[normalize-space(.)='Update']"
            ).first
            if upd.count() > 0 and upd.is_visible():
                upd.click(timeout=1500)
                time.sleep(0.25)
                return True
        except Exception:
            pass
        try:
            upd2 = mf.locator("xpath=//table//a[normalize-space(.)='Update']").first
            if upd2.count() > 0 and upd2.is_visible():
                upd2.click(timeout=1500)
                time.sleep(0.25)
                return True
        except Exception:
            pass
        return False

    def recharge(self, account_or_id: Union[str, int], amount: Union[int, float], note: str = ""):
        self.search_user(account_or_id)
        self._click_update_for_row(account_or_id)
        time.sleep(0.35)
        self._fill_amount_dialog("recharge", amount, note)

    def redeem(self, account_or_id: Union[str, int], amount: Union[int, float], note: str = ""):
        self.search_user(account_or_id)
        self._click_update_for_row(account_or_id)
        time.sleep(0.35)
        self._fill_amount_dialog("redeem", amount, note)

    # ── wrapper username generator (kept) ───────────────────────────────────
    def _gen_username_and_nickname_from_env(self) -> Tuple[str, str, str]:
        account = self._generate_username()
        password = account
        nickname = account
        return account, nickname, password


# ──────────────────────────────────────────────────────────────────────────────
# Wrappers / CLI
def mw_from_env() -> MilkywayUIBot:
    return MilkywayUIBot(MWConfig())

def mw_login():
    with mw_from_env().session() as bot:
        bot.ensure_logged_in()

def mw_create_player(account: str, password: str, nickname: Optional[str] = None):
    with mw_from_env().session() as bot:
        bot.ensure_logged_in()
        return bot.create_player(account, password, nickname)

def mw_create_player_auto():
    with mw_from_env().session() as bot:
        bot.ensure_logged_in()
        info = bot.create_player_auto()
        return info

def mw_recharge(account_or_id: Union[str, int], amount: Union[int, float], note: str = ""):
    with mw_from_env().session() as bot:
        bot.ensure_logged_in()
        bot.recharge(account_or_id, amount, note)

def mw_redeem(account_or_id: Union[str, int], amount: Union[int, float], note: str = ""):
    with mw_from_env().session() as bot:
        bot.ensure_logged_in()
        bot.redeem(account_or_id, amount, note)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser("Milkyway UI Bot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login")

    # create
    sub.add_parser("create-auto")
    pcreate = sub.add_parser("create")
    pcreate.add_argument("account")
    pcreate.add_argument("password")
    pcreate.add_argument("--nickname")

    # recharge / redeem
    prec = sub.add_parser("recharge")
    prec.add_argument("account_or_id")
    prec.add_argument("amount", type=float)
    prec.add_argument("--note", default="")

    pred = sub.add_parser("redeem")
    pred.add_argument("account_or_id")
    pred.add_argument("amount", type=float)
    pred.add_argument("--note", default="")

    # ✅ aliases (so you can use recharge-auto / redeem-auto too)
    preca = sub.add_parser("recharge-auto")
    preca.add_argument("account_or_id")
    preca.add_argument("amount", type=float)
    preca.add_argument("--note", default="")

    preda = sub.add_parser("redeem-auto")
    preda.add_argument("account_or_id")
    preda.add_argument("amount", type=float)
    preda.add_argument("--note", default="")

    args = ap.parse_args()

    if args.cmd == "login":
        mw_login()
        print("✅ Logged in OK")

    elif args.cmd == "create-auto":
        info = mw_create_player_auto()
        print("✅ Created:", info)

    elif args.cmd == "create":
        info = mw_create_player(args.account, args.password, args.nickname)
        print("✅ Created:", info)

    elif args.cmd in ("recharge", "recharge-auto"):
        mw_recharge(args.account_or_id, args.amount, args.note)
        print(f"✅ Recharged {args.account_or_id} by {args.amount}")

    elif args.cmd in ("redeem", "redeem-auto"):
        mw_redeem(args.account_or_id, args.amount, args.note)
        print(f"✅ Redeemed {args.account_or_id} by {args.amount}")