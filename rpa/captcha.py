# rpa/captcha.py
# -*- coding: utf-8 -*-
"""
Strict 2Captcha image solver for 3–5 digit captchas (GameVault uses 4).
- Accepts either TWO_CAPTCHA_APIKEY or TWO_CAPTCHA_API_KEY from env
- Uploads image, polls for result, returns only the numeric digits
- Validates to 3–5 digits (you can force exactly 4 in caller if you like)
"""

import os
import time
import re
import requests
from typing import Optional

CAPTCHA_KEY = os.getenv("TWO_CAPTCHA_APIKEY") or os.getenv("TWO_CAPTCHA_API_KEY") or ""

class CaptchaError(RuntimeError):
    pass

def _post_in(png_bytes: bytes, key: str) -> str:
    files = {"file": ("captcha.png", png_bytes, "image/png")}
    data = {"key": key, "method": "post", "soft_id": "2626"}  # soft_id is optional attribution
    r = requests.post("https://2captcha.com/in.php", data=data, files=files, timeout=60)
    if r.status_code != 200 or "OK|" not in r.text:
        raise CaptchaError(f"2Captcha upload failed: {r.text}")
    return r.text.split("|", 1)[1]

def _get_res(key: str, cap_id: str, max_wait_s: int = 60) -> str:
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        g = requests.get(
            "https://2captcha.com/res.php",
            params={"key": key, "action": "get", "id": cap_id},
            timeout=30,
        )
        txt = g.text.strip()
        if txt == "CAPCHA_NOT_READY":
            time.sleep(2.5)
            continue
        if txt.startswith("OK|"):
            return txt.split("|", 1)[1]
        # hard error from service
        raise CaptchaError(f"2Captcha error: {txt}")
    raise CaptchaError("2Captcha timeout waiting for solution")

def _digits_only(s: str) -> str:
    return "".join(re.findall(r"\d", s or ""))

def solve_image_captcha(api_key: Optional[str], png_bytes: bytes) -> str:
    """
    Uploads PNG bytes to 2Captcha and returns ONLY the digits (3–5 chars typical).
    Raises CaptchaError on failure.
    """
    key = (api_key or CAPTCHA_KEY).strip()
    if not key:
        raise CaptchaError("TWO_CAPTCHA_APIKEY / TWO_CAPTCHA_API_KEY not set")

    cap_id = _post_in(png_bytes, key)
    raw = _get_res(key, cap_id, max_wait_s=90)
    digits = _digits_only(raw)

    # Expect a short numeric code (GameVault shows 4 digits).
    if not (3 <= len(digits) <= 5):
        raise CaptchaError(f"Solver returned non-numeric/invalid: {raw!r}")

    # If 5+ digits, pick the last 4 (works best if service appends noise)
    if len(digits) > 4:
        digits = digits[-4:]

    return digits