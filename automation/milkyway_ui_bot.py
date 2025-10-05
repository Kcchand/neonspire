# automation/milkyway_ui_bot.py
import os, re, time, json, base64, random, string
from pathlib import Path
from urllib.parse import urlparse
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, Union, List, Tuple

import requests
from dotenv import load_dotenv
from playwright.sync_api import (
    sync_playwright, Page, BrowserContext, Frame, Locator
)

# ──────────────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)

# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class MWConfig:
    base: str = os.getenv("MILKYWAY_BASE", "https://milkywayapp.xyz:8781")
    username: str = os.getenv("MW_USERNAME", "")
    password: str = os.getenv("MW_PASSWORD", "")
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

    # UI overrides (optional)
    user_nav_label: Optional[str] = os.getenv("MW_USER_NAV_LABEL") or "User Management"
    create_selector: Optional[str] = os.getenv("MW_CREATE_SELECTOR") or None
    form_scope_selector: Optional[str] = os.getenv("MW_FORM_SCOPE_SELECTOR") or None
    create_text: str = os.getenv("MW_CREATE_TEXT", "Create Player")

    # Auto-create name rules (used by create-auto)
    username_len: int = int(os.getenv("USERNAME_LEN", "6"))
    username_suffix: str = os.getenv("USERNAME_SUFFIX", "_mw")
    username_alphanum: bool = (os.getenv("USERNAME_ALPHANUM", "0") == "1")

    @property
    def login_url(self) -> str: return f"{self.base}/default.aspx"
    @property
    def store_url(self) -> str: return f"{self.base}/Store.aspx"

class MWError(RuntimeError): ...
# ──────────────────────────────────────────────────────────────────────────────

class MilkywayUIBot:
    def __init__(self, cfg: Optional[MWConfig] = None):
        self.cfg = cfg or MWConfig()
        if self.cfg.proxy and self.cfg.proxy.strip().startswith("#"):
            self.cfg.proxy = None
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
                if self._ctx: self._ctx.close()
            finally:
                try:
                    if self._browser: self._browser.close()
                finally:
                    if self._pw: self._pw.stop()

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
        if not value: return None
        v = value.strip()
        if not v: return None
        if "://" not in v: v = "http://" + v
        u = urlparse(v)
        if not (u.scheme and (u.hostname or u.netloc)): return None
        server = f"{u.scheme}://{u.hostname or u.netloc}"
        if u.port: server += f":{u.port}"
        return {"server": server}

    def _dump_dom(self, prefix="last"):
        d = Path(self.cfg.persist_dir); d.mkdir(parents=True, exist_ok=True)
        try:
            html = self.page.content()
            (d / f"{prefix}_dom.html").write_text(html, encoding="utf-8")
        except Exception:
            pass

    def _dump_frame_dom(self, fr: Frame, name: str):
        try:
            Path(self.cfg.persist_dir).mkdir(parents=True, exist_ok=True)
            (Path(self.cfg.persist_dir)/f"{name}_frame_dom.html").write_text(fr.content(), encoding="utf-8")
        except Exception:
            pass

    def _save_debug(self, scope, reason: str, prefix="last"):
        d = Path(self.cfg.persist_dir); d.mkdir(parents=True, exist_ok=True)
        try:
            scope.screenshot(path=str(d / f"{prefix}_error.png"), full_page=True)
        except Exception:
            try: self.page.screenshot(path=str(d / f"{prefix}_error.png"), full_page=True)
            except Exception: pass
        try:
            (d / f"{prefix}_reason.txt").write_text(reason, encoding="utf-8")
        except Exception:
            pass
        self._dump_dom(prefix)

    def _wait_for_idle(self, where: Optional[Page | Frame] = None, timeout_ms: int = 8000):
        where = where or self.page
        try:
            where.wait_for_load_state("networkidle", timeout=timeout_ms)  # type: ignore[attr-defined]
        except Exception:
            pass
        mask_css = [".el-loading-mask",".ant-spin",".ant-modal-mask",".modal-backdrop","[aria-busy='true']","[data-loading='true']"]
        t0 = time.time()
        while time.time() - t0 < timeout_ms/1000:
            any_visible = False
            for sel in mask_css:
                try:
                    loc = where.locator(sel)  # type: ignore[attr-defined]
                    if loc.count() > 0 and loc.first.is_visible():
                        any_visible = True; break
                except Exception:
                    pass
            if not any_visible:
                break
            time.sleep(0.2)

    def _ensure_page_open(self):
        try:
            _ = self.page.url
        except Exception:
            if self._ctx is None:
                raise MWError("Browser context is closed.")
            self.page = self._ctx.new_page()
            self.page.set_default_timeout(self.cfg.timeout_sec * 1000)
            self.page.set_default_navigation_timeout(self.cfg.timeout_sec * 1000)

    # ── captcha helpers ──────────────────────────────────────────────────────
    def _grab_login_captcha_bytes(self) -> bytes:
        try:
            code_input = self.page.locator("input[type='text']").nth(1)
            img = code_input.locator("xpath=following::img[1]")
            if img.count() > 0:
                return img.first.screenshot()
        except Exception:
            pass
        return self.page.locator("img").first.screenshot()

    def _solve_captcha(self, img_bytes: bytes) -> str:
        if self.cfg.captcha_provider != "2captcha":
            raise MWError("Only 2Captcha is supported in this build.")
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
            res = requests.get("https://2captcha.com/res.php",
                               params={"key": self.cfg.captcha_key, "action": "get", "id": cid, "json": 1},
                               timeout=30).json()
            if res.get("status") == 1:
                return str(res["request"]).strip()
            if res.get("request") != "CAPCHA_NOT_READY":
                raise MWError(f"2Captcha res.php error: {res}")
            if time.time() - t0 > self.cfg.captcha_timeout_sec:
                raise MWError("2Captcha timeout.")

    def _refresh_and_solve_captcha(self) -> str:
        try:
            code_input = self.page.locator("input[type='text']").nth(1)
            img = code_input.locator("xpath=following::img[1]").first
            if img and img.is_visible():
                img.click(timeout=1000)
                time.sleep(0.9)
        except Exception:
            pass
        code = self._solve_captcha(self._grab_login_captcha_bytes())
        return "".join(ch for ch in code if ch.isdigit())

    def _dismiss_verifycode_dialog_if_present(self) -> bool:
        try:
            msg = self.page.locator(
                "xpath=//*[contains(normalize-space(.),'validation code') or contains(normalize-space(.),'verifycode') or contains(normalize-space(.),'incorrect')]"
            ).first
            if msg.count() > 0 and msg.is_visible():
                ok = self.page.get_by_role("button", name=re.compile(r"^ok$", re.I))
                if ok.count() > 0 and ok.first.is_visible():
                    ok.first.click(timeout=1200)
                return True
        except Exception:
            pass
        return False

    def _dismiss_message_if_contains(self, substrings: List[str]) -> bool:
        try:
            for s in substrings:
                box = self.page.locator(f"xpath=//*[contains(normalize-space(.),'{s}')]").first
                if box.count() > 0 and box.is_visible():
                    ok = self.page.get_by_role("button", name=re.compile("^ok$", re.I))
                    if ok.count() > 0 and ok.first.is_visible():
                        ok.first.click(timeout=1200)
                        return True
            return False
        except Exception:
            return False

    # ── navigation & login ───────────────────────────────────────────────────
    def _goto(self, url: str, label: str = ""):
        last_err = None
        for _ in range(3):
            try:
                self._ensure_page_open()
                self.page.bring_to_front()
                self.page.goto(url, wait_until="load", timeout=self.cfg.timeout_sec * 1000)
                self._wait_for_idle(self.page, 5000)
                if not self.page.url.startswith("about:blank"):
                    return
            except Exception as e:
                last_err = e
                time.sleep(0.8)
                try: _ = self.page.url
                except Exception: self._ensure_page_open()
        self._save_debug(self.page, f"Failed goto {label or url}")
        raise MWError(f"Failed to navigate to {label or url}: {last_err}")

    def ensure_logged_in(self):
        # Try cookie first
        try:
            self._goto(self.cfg.store_url, label="Store")
            if self.page.url.startswith(self.cfg.store_url):
                return
        except MWError:
            pass

        # Fresh login
        self._goto(self.cfg.login_url, label="Login")
        for full_try in range(2):                    # two fresh-login rounds
            try:
                self._ensure_page_open()
                acct = self.page.locator("input[type='text']").first
                pwd  = self.page.locator("input[type='password']").first
                code = self.page.locator("input[type='text']").nth(1)
                acct.fill(self.cfg.username)
                pwd.fill(self.cfg.password)

                for attempt in range(1, 8):          # a few more captcha attempts
                    captcha = self._solve_captcha(self._grab_login_captcha_bytes()) if attempt == 1 else self._refresh_and_solve_captcha()
                    captcha = "".join(ch for ch in captcha if ch.isdigit())
                    code.fill(captcha)
                    self.page.get_by_role("button", name=re.compile("login", re.I)).click()
                    try:
                        self.page.wait_for_url(self.cfg.store_url, timeout=self.cfg.timeout_sec * 1000)
                        self._wait_for_idle(self.page, 4000)
                        return
                    except Exception:
                        if self._dismiss_verifycode_dialog_if_present():
                            continue
                        break
            except Exception:
                try: self._goto(self.cfg.login_url, label="Login")
                except Exception: pass

        self._save_debug(self.page, "Login failed after captcha retries")
        raise MWError("Login failed after multiple captcha retries.")

    # ── frames (Store.aspx) ──────────────────────────────────────────────────
    def _left_frame(self) -> Optional[Frame]:
        fr = self.page.frame(name="frm_left_frm")
        if fr: return fr
        for f in self.page.frames:
            try:
                if "Left.aspx" in (f.url or ""):
                    return f
            except Exception:
                pass
        return None

    def _main_frame(self) -> Optional[Frame]:
        fr = self.page.frame(name="frm_main_content")
        if fr: return fr
        for f in self.page.frames:
            try:
                u = (f.url or "")
                if "right.aspx" in u.lower() or "accountslist.aspx" in u.lower():
                    return f
            except Exception:
                pass
        return None

    def _dialog_create_frame(self) -> Optional[Frame]:
        for f in self.page.frames:
            try:
                u = (f.url or "")
            except Exception:
                u = ""
            if "CreateAccount.aspx" in u or "CreateAccount" in u:
                return f
        return None

    # ── navigation inside Store ──────────────────────────────────────────────
    def goto_user_management(self):
        if "Store.aspx" not in self.page.url:
            self._goto(self.cfg.store_url, label="Store")

        lf = self._left_frame()
        if lf:
            labels = [self.cfg.user_nav_label] if self.cfg.user_nav_label else ["User Management","Players","Account Management"]
            clicked = False
            for txt in labels:
                for sel in [
                    f"xpath=//a[normalize-space(.)='{txt}']",
                    f"text={txt}",
                    f"xpath=//*[contains(normalize-space(.),'{txt}')]",
                ]:
                    try:
                        loc = lf.locator(sel).first
                        if loc.count() > 0 and loc.is_visible():
                            loc.click(timeout=1500)
                            clicked = True
                            break
                    except Exception:
                        pass
                if clicked: break

        mf = None
        t0 = time.time()
        while time.time() - t0 < 8:
            mf = self._main_frame()
            if mf:
                if mf.locator("input[placeholder='ID or Account']").count() > 0 or \
                   mf.locator("xpath=//*[self::a or self::button][contains(normalize-space(.),'Create Player')]").count() > 0:
                    break
            time.sleep(0.2)
        self._wait_for_idle(mf or self.page, 5000)

    def _dismiss_any_ok(self):
        try:
            okbtn = self.page.get_by_role("button", name=re.compile(r"^ok$", re.I))
        except Exception:
            okbtn = None
        try:
            if okbtn and okbtn.count() > 0 and okbtn.first.is_visible():
                okbtn.first.click(timeout=1500)
        except Exception:
            pass

    # ── universal clicker for the blue "Create Player" button ───────────────
    def _click_create_player_button_anywhere(self, fr: Optional[Frame] = None) -> bool:
        scopes: List[Page | Frame] = []
        if fr: scopes.append(fr)
        dlg_fr = self._dialog_create_frame()
        if dlg_fr and dlg_fr not in scopes: scopes.append(dlg_fr)
        mf = self._main_frame()
        if mf: scopes.append(mf)
        scopes.append(self.page)

        selectors = [
            "input#btnAdd",
            "input[name='btnAdd']",
            "input[id$='btnAdd']",
            "input[type='button'][value*='Create']",
            "xpath=//input[@type='button' and (contains(@value,'Create Player') or contains(@value,'Create'))]",
            "button:has-text('Create Player')",
            "xpath=//button[contains(normalize-space(.),'Create Player') or contains(normalize-space(.),'Create')]",
            "css=button:has-text('Create')",
            "text=Create Player",
        ]

        for sc in scopes:
            for sel in selectors:
                try:
                    loc = sc.locator(sel).first  # type: ignore[attr-defined]
                    if loc.count() > 0 and loc.is_visible():
                        loc.scroll_into_view_if_needed(timeout=1500)
                        loc.click(timeout=2500, force=True)
                        return True
                except Exception:
                    pass

            # JS fallback inside the scope
            try:
                ok = sc.evaluate("""
                () => {
                  const isV = el => {
                    if (!el) return false;
                    const s = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s.display!=='none' && s.visibility!=='hidden' && +s.opacity!==0 && r.width>0 && r.height>0;
                  };
                  const txt = el => (el.innerText||el.textContent||el.value||'').toLowerCase().trim();
                  const cands = Array.from(document.querySelectorAll('input[type=button],button,a[role=button]'));
                  const el = cands.find(e => isV(e) && (txt(e).includes('create player') || txt(e).includes('create')));
                  if (!el) return false;
                  el.scrollIntoView({block:'center', inline:'center'});
                  el.click();
                  return true;
                }
                """)  # type: ignore[attr-defined]
                if ok: return True
            except Exception:
                pass

        self._save_debug(self.page, "Could not click Create Player (all scopes)")
        return False

    # ── open Create Player ───────────────────────────────────────────────────
    def _click_create_player(self):
        if self._dialog_create_frame():
            return

        self.goto_user_management()

        if self.cfg.create_selector:
            try:
                mf = self._main_frame() or self.page
                mf.locator(self.cfg.create_selector).first.click(timeout=2000)  # type: ignore[attr-defined]
                self._wait_for_idle(self.page, 1500)
                return
            except Exception:
                pass

        mf = self._main_frame() or self.page
        candidates = [
            "css=button:has-text('Create Player')",
            "css=a:has-text('Create Player')",
            "xpath=//*[self::button or self::a or @role='button'][contains(normalize-space(.),'Create Player')]",
            "xpath=//input[@type='button' and contains(@value,'Create Player')]",
        ]
        clicked = False
        for sel in candidates:
            try:
                loc = mf.locator(sel).last  # type: ignore[attr-defined]
                if loc.count() > 0 and loc.is_visible():
                    loc.scroll_into_view_if_needed(timeout=1500)
                    loc.click(timeout=2500, force=True)
                    clicked = True
                    break
            except Exception:
                pass

        if not clicked:
            js = f"""
            (() => {{
              const want = {json.dumps(self.cfg.create_text.lower())};
              const isV = el => {{ const s = getComputedStyle(el); const r = el.getBoundingClientRect();
                return s.display!=='none' && s.visibility!=='hidden' && +s.opacity!==0 && r.width>0 && r.height>0; }};
              const txt = el => (el.innerText||el.textContent||el.value||el.title||el.getAttribute('aria-label')||'')
                  .replace(/\\s+/g,' ').trim().toLowerCase();
              const all = Array.from(document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit],*'));
              const hits = all.filter(el => isV(el) && txt(el).includes(want));
              if (!hits.length) return false;
              const el = hits.sort((a,b)=>b.getBoundingClientRect().left - a.getBoundingClientRect().left)[0];
              el.scrollIntoView({{block:'center',inline:'center'}});
              el.dispatchEvent(new MouseEvent('click',{{bubbles:true,cancelable:true}}));
              return true;
            }})()
            """
            try:
                if not bool(self.page.evaluate(js)):
                    self._save_debug(self.page, "Create Player button not found")
                    raise MWError("Create Player button not found.")
            except Exception:
                self._save_debug(self.page, "Create Player button not found (JS failed)")
                raise MWError("Create Player button not found.")

        t0 = time.time()
        while time.time() - t0 < 8:
            if self._dialog_create_frame():
                break
            time.sleep(0.2)
        self._wait_for_idle(self.page, 1500)

    # ── fill + submit (iframe first, fallback to inline/modal) ───────────────
    def _fill_create_inline_or_modal_and_submit(self, account: str, nickname: str, password: str):
        fr = self._dialog_create_frame()
        if fr:
            def fill_any(sel_list, value):
                for sel in sel_list:
                    try:
                        loc = fr.locator(sel).first
                        if loc.count() > 0 and loc.is_visible():
                            loc.fill(value, timeout=2500)
                            return True
                    except Exception:
                        pass
                return False

            fill_any(["input#txtAccount","input[name='txtAccount']","input[id$='txtAccount']","input[placeholder*='Account' i]"], account)
            fill_any(["input#txtNickName","input[name='txtNickName']","input[id$='txtNickName']","input[placeholder*='Nick' i]"], nickname)

            pw_inputs = fr.locator("input[type='password']")
            if pw_inputs.count() >= 2:
                try: pw_inputs.nth(0).fill(password, timeout=2000)
                except Exception: pass
                try: pw_inputs.nth(1).fill(password, timeout=2000)
                except Exception: pass
            else:
                fill_any(["input#txtPassword","input[name='txtPassword']","input[id$='txtPassword']","input[type='password']"], password)
                fill_any(["input#txtConfirm","input[name='txtConfirm']","input[id$='txtConfirm']","input[placeholder*='Confirm' i]"], password)

            clicked = self._click_create_player_button_anywhere(fr)
            if not clicked:
                try:
                    (fr.locator("input#txtConfirm, input[type='password']").last
                       .press("Enter", timeout=1500))
                    clicked = True
                except Exception:
                    pass

            if not clicked:
                self._dump_frame_dom(fr, "create_dialog")
                self._save_debug(self.page, "Create submit not found in iframe")
                raise MWError("Create form submit button not found (iframe).")

            t0 = time.time()
            while time.time() - t0 < 8:
                if not self._dialog_create_frame():
                    break
                time.sleep(0.2)

            time.sleep(0.6)
            self._dismiss_any_ok()
            self._wait_for_idle(self.page, 1500)
            return

        # Fallback: inline / modal within main frame
        def pick_scope() -> Locator:
            scopes: List[Page | Frame] = [self._main_frame() or self.page, self.page]
            if self.cfg.form_scope_selector:
                for sc in scopes:
                    try:
                        cont = sc.locator(self.cfg.form_scope_selector)  # type: ignore[attr-defined]
                        if cont.count() > 0 and cont.first.is_visible():
                            return cont.first
                    except Exception:
                        pass
            for sc in scopes:
                for sel in [
                    "xpath=(//div[.//label[contains(.,'Account')] and (.//button[contains(.,'Create')]|.//input[@type='button' and contains(@value,'Create')]))[1])",
                    "xpath=(//section[.//label[contains(.,'Account')] and (.//button[contains(.,'Create')]|.//input[@type='button' and contains(@value,'Create')]))[1])",
                    "xpath=(//div[contains(@class,'modal') or @role='dialog'])[last()]",
                ]:
                    try:
                        cont = sc.locator(sel)  # type: ignore[attr-defined]
                        if cont.count() > 0 and cont.first.is_visible():
                            return cont.first
                    except Exception:
                        pass
            return (self._main_frame() or self.page).locator("xpath=//*").first  # type: ignore[attr-defined]

        scope = pick_scope()

        def fill_any(labels: List[str], value: str) -> bool:
            for lv in labels:
                try:
                    loc = scope.locator(f"xpath=.//label[contains(normalize-space(.), '{lv}')]/following::input[1]").first
                    if loc.count() > 0 and loc.is_visible():
                        loc.fill(value, timeout=2000)
                        return True
                except Exception:
                    pass
            for ph in labels:
                try:
                    loc = scope.locator(f"input[placeholder*='{ph}' i]").first
                    if loc.count() > 0 and loc.is_visible():
                        loc.fill(value, timeout=2000)
                        return True
                except Exception:
                    pass
            for key in ["account","user","username","login","nick","nickname","display","pass","pwd","password","confirm"]:
                try:
                    loc = scope.locator(f"css=input[id*='{key}' i], input[name*='{key}' i]").first
                    if loc.count() > 0 and loc.is_visible():
                        loc.fill(value, timeout=2000)
                        return True
                except Exception:
                    pass
            return False

        if not fill_any(["Account","User","Username","Login"], account):
            try: scope.locator("input[type='text']:visible").first.fill(account, timeout=2000)
            except Exception: pass

        if not fill_any(["NickName","Nickname","Display"], nickname):
            try:
                t_inputs = scope.locator("input[type='text']:visible")
                if t_inputs.count() >= 2: t_inputs.nth(1).fill(nickname, timeout=2000)
            except Exception: pass

        for _ in (0, 1):
            if not fill_any(["Password","Login password","Confirm","Confirm password"], password):
                try:
                    pws = scope.locator("input[type='password']:visible")
                    for i in range(min(2, pws.count())):
                        pws.nth(i).fill(password, timeout=2000)
                except Exception: pass

        if not self._click_create_player_button_anywhere(None):
            try:
                scope.locator("input[type='password']:visible").last.press("Enter", timeout=1500)
            except Exception:
                pass

        time.sleep(0.6)
        self._dismiss_any_ok()
        self._wait_for_idle(self.page, 1500)

    # ── popup/error handling (for recharge/redeem) ───────────────────────────
    def _popup_has_runtime_error(self, container: Locator) -> bool:
        try:
            txt = container.inner_text(timeout=300).lower()
            return ("server error in '/'" in txt) or ("runtime error" in txt) or ("web.config" in txt)
        except Exception:
            return False

    def _close_topmost_popup(self):
        for ctx in self._contexts():
            try:
                c = ctx.locator("xpath=(//*[contains(@class,'dialog') or contains(@class,'modal') or @role='dialog']//*[self::button or self::a][contains(@class,'close') or text()='×' or text()='x'])[last()]")
                if c.count() > 0 and c.first.is_visible():
                    c.first.click(timeout=800)
                    time.sleep(0.2)
                    return
            except Exception:
                pass
        try: self.page.keyboard.press("Escape")
        except Exception: pass
        time.sleep(0.2)

    def _contexts(self) -> List[Union[Page, Frame]]:
        ctxs: List[Union[Page, Frame]] = [self.page]
        mf = self._main_frame()
        if mf and mf not in ctxs: ctxs.append(mf)
        for fr in self.page.frames:
            if fr not in ctxs:
                ctxs.append(fr)
        return ctxs

    def _find_amount_modal(self, cap: str) -> Optional[Locator]:
        for ctx in self._contexts():
            try:
                btn = ctx.locator(
                    f"xpath=(//*[self::button or self::input][@type='button' or @type='submit' or not(@type)][contains(normalize-space(.),'{cap}') or contains(@value,'{cap}')])[last()]"
                )
                if btn.count() > 0 and btn.first.is_visible():
                    cont = btn.first.locator("xpath=ancestor::*[self::div or self::form][1]")
                    if cont.count() > 0 and cont.first.is_visible():
                        return cont.first
            except Exception:
                pass
        for ctx in self._contexts():
            try:
                lab = ctx.locator("xpath=(//*[contains(normalize-space(.),'Recharge Amount') or contains(normalize-space(.),'Redeem Amount') or contains(normalize-space(.),'Withdraw Amount')])[last()]")
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
                    f"xpath=(.//*[self::label or self::td or self::*][contains(normalize-space(.),'{lbl}')])[1]/following::input[1]"
                )
                if by_label.count() > 0 and by_label.first.is_enabled() and by_label.first.is_visible():
                    return by_label.first
            except Exception:
                pass
        try:
            guess = container.locator(
                "xpath=.//input[not(@readonly) and not(@disabled) and "
                "(contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'amount') or "
                " contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'amount') or "
                " contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'recharge') or "
                " contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'recharge') or "
                " contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'redeem') or "
                " contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'redeem'))]"
            )
            if guess.count() > 0 and guess.first.is_visible():
                return guess.first
        except Exception:
            pass
        try:
            any_input = container.locator("css=input[type='text']:enabled:visible")
            if any_input.count() > 0:
                return any_input.first
        except Exception:
            pass
        return None

    def _open_amount_popup_or_retry(self, which: str, retries: int = 6) -> Locator:
        cap = "Recharge" if which == "recharge" else "Redeem"
        for attempt in range(1, retries + 1):
            container = self._find_amount_modal(cap)
            if container and not self._popup_has_runtime_error(container):
                return container

            self._click_action_in_main([cap, "Top Up" if cap == "Recharge" else "Debit",
                                        "Credit" if cap == "Recharge" else "Withdraw", "Load"])
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

            if self._popup_has_runtime_error(container):
                self._close_topmost_popup()
                time.sleep(0.4)
                if attempt == retries:
                    self._save_debug(self.page, f"{cap} popup kept showing runtime error")
                    raise MWError(f"{cap} failed: runtime error in popup.")
                continue

            return container
        raise MWError(f"{cap} dialog not found.")

    # ── action buttons ───────────────────────────────────────────────────────
    def _click_action_in_main(self, labels: List[str]) -> None:
        mf = self._main_frame() or self.page
        for t in labels:
            try:
                btn = mf.get_by_role("button", name=re.compile(fr"^{re.escape(t)}$", re.I))
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=2000)
                    return
            except Exception:
                pass
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

    # ── amount dialog submit ─────────────────────────────────────────────────
    def _fill_amount_dialog(self, kind: str, amount: Union[int,float], note: str=""):
        cap = "Recharge" if kind == "recharge" else "Redeem"
        container = self._open_amount_popup_or_retry(kind, retries=6)

        amount_input = self._find_amount_input_inside(container)
        if not amount_input:
            self._save_debug(container, f"{cap} amount input not found")
            raise MWError(f"{cap} amount input not found.")

        try:
            f = float(amount)
            amt_str = str(int(f)) if f.is_integer() else str(amount)
        except Exception:
            amt_str = str(amount)

        try: amount_input.click(timeout=1200)
        except Exception: pass
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
            self._save_debug(container, f"{cap} submit button not found")
            raise MWError(f"{cap} submit button not found.")

        msg_text = (self._wait_for_message_and_click_ok(timeout_s=10) or "").lower()
        if "server error" in msg_text or "runtime error" in msg_text:
            self._save_debug(self.page, f"{cap} failed — message: {msg_text}")
            raise MWError(f"{cap} failed: {msg_text or 'server error'}")

        self._wait_for_idle(self.page, 1500)

    def _wait_for_message_and_click_ok(self, timeout_s: int = 8) -> Optional[str]:
        deadline = time.time() + timeout_s
        last_text = None
        while time.time() < deadline:
            for ctx in self._contexts():
                try:
                    box = ctx.locator("xpath=(//*[@role='dialog' or contains(@class,'message') or contains(@class,'alert') or contains(@class,'modal')])[last()]")
                    if box.count() > 0 and box.first.is_visible():
                        text = box.first.inner_text().strip()
                        if text: last_text = text
                        ok = box.first.get_by_role("button", name=re.compile(r"^ok$", re.I))
                        if ok.count() > 0 and ok.first.is_visible():
                            ok.first.click(timeout=1500)
                            return last_text
                except Exception:
                    pass
            time.sleep(0.15)
        return last_text

    # ── grid helpers ─────────────────────────────────────────────────────────
    def _click_update_for_row(self, account_or_id: Union[str,int]) -> bool:
        mf = self._main_frame() or self.page
        key = str(account_or_id)
        try:
            upd = mf.locator(
                f"xpath=//table//tr[.//td[contains(normalize-space(.),'{key}')]]//a[normalize-space(.)='Update']"
            ).first
            if upd.count() > 0 and upd.is_visible():
                upd.click(timeout=1500); time.sleep(0.25); return True
        except Exception:
            pass
        try:
            upd2 = mf.locator("xpath=//table//a[normalize-space(.)='Update']").first
            if upd2.count() > 0 and upd2.is_visible():
                upd2.click(timeout=1500); time.sleep(0.25); return True
        except Exception:
            pass
        return False

    def _get_row_balance(self, account_or_id: Union[str,int]) -> Optional[float]:
        mf = self._main_frame() or self.page
        key = str(account_or_id)
        try:
            row = mf.locator(f"xpath=//table//tr[.//td[contains(normalize-space(.),'{key}')]][1]")
            if row.count() == 0 or not row.first.is_visible():
                return None
            bal_td = row.first.locator("xpath=./td[5]")
            if bal_td.count() == 0:
                bal_td = row.first.locator("xpath=./td[.//text()]")
            txt = bal_td.first.inner_text().strip()
            m = re.search(r"-?\d+(?:\.\d+)?", txt.replace(",", ""))
            return float(m.group(0)) if m else None
        except Exception:
            return None

    # ── public ops ───────────────────────────────────────────────────────────
    def create_player(self, account: str, password: str, nickname: Optional[str] = None) -> dict:
        self.goto_user_management()
        self._click_create_player()
        self._fill_create_inline_or_modal_and_submit(account, nickname or account, password)
        time.sleep(0.4)
        try:
            self.search_user(account)
        except Exception:
            pass
        return {"created": True, "account": account, "nickname": nickname or account}

    def search_user(self, account_or_id: Union[str, int]) -> dict:
        self.goto_user_management()
        mf = self._main_frame() or self.page
        box = mf.locator("input[placeholder='ID or Account']").first  # type: ignore[attr-defined]
        if box.count() == 0:
            t_inputs = mf.locator("input[type='text']:visible")  # type: ignore[attr-defined]
            if t_inputs.count() > 0: box = t_inputs.nth(0)
        box.fill("")
        box.type(str(account_or_id), timeout=3000)
        clicked = False
        for label in ("Search","Query","Find","Go"):
            try:
                btn = mf.get_by_role("button", name=re.compile(label, re.I))  # type: ignore[attr-defined]
                if btn.count() > 0:
                    btn.first.click(timeout=1500); clicked = True; break
            except Exception:
                pass
        if not clicked:
            try: mf.locator("xpath=//*[self::button or self::input][contains(@value,'Search') or contains(normalize-space(.),'Search')]").first.click(timeout=1500)
            except Exception: pass
        time.sleep(0.6)
        return {"selected": True, "account_or_id": str(account_or_id)}

    def recharge(self, account_or_id: Union[str,int], amount: Union[int,float], note: str=""):
        self.search_user(account_or_id)
        before = self._get_row_balance(account_or_id)
        self._click_update_for_row(account_or_id)
        time.sleep(0.35)
        self._fill_amount_dialog("recharge", amount, note)
        for verify_try in range(2):
            for _ in range(4):
                time.sleep(0.7)
                self.search_user(account_or_id)
                after = self._get_row_balance(account_or_id)
                if before is None or after is None:
                    continue
                if abs((after - before) - float(amount)) < 1e-6:
                    return
            if verify_try == 0:
                self._click_update_for_row(account_or_id)
                time.sleep(0.2)
                self._fill_amount_dialog("recharge", amount, note)
        raise MWError("Recharge completed without balance change; likely failed silently.")

    def redeem(self, account_or_id: Union[str,int], amount: Union[int,float], note: str=""):
        self.search_user(account_or_id)
        before = self._get_row_balance(account_or_id)
        self._click_update_for_row(account_or_id)
        time.sleep(0.35)
        self._fill_amount_dialog("redeem", amount, note)
        for verify_try in range(2):
            for _ in range(4):
                time.sleep(0.7)
                self.search_user(account_or_id)
                after = self._get_row_balance(account_or_id)
                if before is None or after is None:
                    continue
                if abs((before - after) - float(amount)) < 1e-6:
                    return
            if verify_try == 0:
                self._click_update_for_row(account_or_id)
                time.sleep(0.2)
                self._fill_amount_dialog("redeem", amount, note)
        raise MWError("Redeem completed without balance change; likely failed silently.")

    def scan_ui(self):
        self.ensure_logged_in()
        self.goto_user_management()
        self._save_debug(self.page, "UI Scan (top)", prefix="scan_top")
        lf = self._left_frame()
        if lf:
            try:
                Path(self.cfg.persist_dir).mkdir(parents=True, exist_ok=True)
                (Path(self.cfg.persist_dir)/"scan_left_dom.html").write_text(lf.content(), encoding="utf-8")
            except Exception: pass
        mf = self._main_frame()
        if mf:
            try:
                Path(self.cfg.persist_dir).mkdir(parents=True, exist_ok=True)
                (Path(self.cfg.persist_dir)/"scan_main_dom.html").write_text(mf.content(), encoding="utf-8")
            except Exception: pass
        print("Saved scan files to .data/milkyway (scan_top_error.png / scan_*_dom.html)")

    # ── generator used by create-auto only ───────────────────────────────────
    def _gen_username_and_nickname_from_env(self) -> Tuple[str, str, str]:
        L = max(1, int(self.cfg.username_len))
        pool = (string.ascii_letters + string.digits) if self.cfg.username_alphanum else string.ascii_letters
        core = "".join(random.choice(pool) for _ in range(L))
        if not any(ch.isalpha() for ch in core):
            core = "player"
        account = f"{core}{self.cfg.username_suffix}"
        if len(account) > 13:
            core = core[: 13 - len(self.cfg.username_suffix)]
            account = f"{core}{self.cfg.username_suffix}"
        password = account
        nickname = core[:6]
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
        account, nickname, password = bot._gen_username_and_nickname_from_env()
        info = bot.create_player(account, password, nickname)
        print(f"✅ Auto-created user '{account}' (nickname '{nickname}')")
        return info

def mw_recharge(account_or_id: Union[str,int], amount: Union[int,float], note: str=""):
    with mw_from_env().session() as bot:
        bot.ensure_logged_in(); bot.recharge(account_or_id, amount, note)

def mw_redeem(account_or_id: Union[str,int], amount: Union[int,float], note: str=""):
    with mw_from_env().session() as bot:
        bot.ensure_logged_in(); bot.redeem(account_or_id, amount, note)

# NEW: one-shot combos
def mw_combo(account: str, password: str, nickname: Optional[str],
             recharge_amount: float, redeem_amount: float,
             recharge_note: str = "", redeem_note: str = ""):
    with mw_from_env().session() as bot:
        bot.ensure_logged_in()
        bot.create_player(account, password, nickname)
        if recharge_amount > 0:
            bot.recharge(account, recharge_amount, recharge_note)
        if redeem_amount > 0:
            bot.redeem(account, redeem_amount, redeem_note)
        return {"account": account, "nickname": nickname or account,
                "recharged": recharge_amount, "redeemed": redeem_amount}

def mw_combo_auto(recharge_amount: float, redeem_amount: float,
                  recharge_note: str = "", redeem_note: str = ""):
    with mw_from_env().session() as bot:
        bot.ensure_logged_in()
        account, nickname, password = bot._gen_username_and_nickname_from_env()
        bot.create_player(account, password, nickname)
        if recharge_amount > 0:
            bot.recharge(account, recharge_amount, recharge_note)
        if redeem_amount > 0:
            bot.redeem(account, redeem_amount, redeem_note)
        print(f"✅ Account: {account} (nick '{nickname}'), created, recharged {recharge_amount}, redeemed {redeem_amount}")
        return {"account": account, "nickname": nickname, "password": password,
                "recharged": recharge_amount, "redeemed": redeem_amount}

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser("Milkyway UI Bot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login")
    sub.add_parser("scan")
    sub.add_parser("create-auto")

    pcreate = sub.add_parser("create")
    pcreate.add_argument("account")
    pcreate.add_argument("password")
    pcreate.add_argument("--nickname")

    prec = sub.add_parser("recharge")
    prec.add_argument("account_or_id")
    prec.add_argument("amount", type=float)
    prec.add_argument("--note", default="")

    pred = sub.add_parser("redeem")
    pred.add_argument("account_or_id")
    pred.add_argument("amount", type=float)
    pred.add_argument("--note", default="")

    # NEW combo CLIs
    pcombo = sub.add_parser("combo", help="Create -> Recharge -> Redeem")
    pcombo.add_argument("account")
    pcombo.add_argument("password")
    pcombo.add_argument("--nickname")
    pcombo.add_argument("--recharge", type=float, required=True)
    pcombo.add_argument("--redeem", type=float, required=True)
    pcombo.add_argument("--recharge-note", default="")
    pcombo.add_argument("--redeem-note", default="")

    pcomboa = sub.add_parser("combo-auto", help="Auto create -> Recharge -> Redeem")
    pcomboa.add_argument("--recharge", type=float, required=True)
    pcomboa.add_argument("--redeem", type=float, required=True)
    pcomboa.add_argument("--recharge-note", default="")
    pcomboa.add_argument("--redeem-note", default="")

    args = ap.parse_args()
    if args.cmd == "login":
        mw_login(); print("✅ Logged in OK")
    elif args.cmd == "scan":
        with mw_from_env().session() as bot: bot.scan_ui()
    elif args.cmd == "create-auto":
        info = mw_create_player_auto(); print("✅ Created:", info)
    elif args.cmd == "create":
        info = mw_create_player(args.account, args.password, args.nickname); print("✅ Created:", info)
    elif args.cmd == "recharge":
        mw_recharge(args.account_or_id, args.amount, args.note); print(f"✅ Recharged {args.account_or_id} by {args.amount}")
    elif args.cmd == "redeem":
        mw_redeem(args.account_or_id, args.amount, args.note); print(f"✅ Redeemed {args.account_or_id} by {args.amount}")
    elif args.cmd == "combo":
        out = mw_combo(args.account, args.password, args.nickname, args.recharge, args.redeem, args.recharge_note, args.redeem_note)
        print("✅ Combo done:", out)
    elif args.cmd == "combo-auto":
        out = mw_combo_auto(args.recharge, args.redeem, args.recharge_note, args.redeem_note)
        print("✅ Combo-auto done:", out)