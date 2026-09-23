"""Playwright plumbing shared by prefill and submit.

Headed Chromium with a persistent profile in browser_profile/, so logins and
cookies survive between runs. Nothing in this module ever clicks a submit
button -- that lives in apply/submit.py behind the approval checks.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
from pathlib import Path
from typing import Optional

from .. import config
from .base import FillPlan, choose_option, normalize
from .dom import EXTRACT_JS, FormField

log = logging.getLogger(__name__)

CAPTCHA_JS = r"""
() => {
  const frames = [...document.querySelectorAll('iframe')].filter(f =>
    /recaptcha|hcaptcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha/i.test(f.src || ''));
  const shown = f => {
    const r = f.getBoundingClientRect(); const s = getComputedStyle(f);
    return r.width > 60 && r.height > 60 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  return frames.some(shown) || !!document.querySelector('.cf-turnstile, #challenge-form');
}
"""

LOGIN_JS = r"""
() => [...document.querySelectorAll('input[type=password]')].some(e => e.getBoundingClientRect().width > 0)
"""

ERRORS_JS = r"""
() => {
  const texts = new Set();
  for (const el of document.querySelectorAll('[aria-invalid="true"], [role="alert"], .error, .field-error, [class*="error"]')) {
    const r = el.getBoundingClientRect();
    const t = (el.innerText || '').trim();
    if (r.width > 0 && t && t.length < 200) texts.add(t);
  }
  return [...texts].slice(0, 10);
}
"""


def profile_dir() -> Path:
    return config.project_root() / "browser_profile"


class Browser:
    """`async with Browser() as b:` -> b.context is a persistent Chromium context."""

    def __init__(self, headless: Optional[bool] = None) -> None:
        cfg = config.settings().get("apply", {})
        self.headless = bool(cfg.get("headless", False)) if headless is None else headless
        self._pw = None
        self.context = None
        self._lock = None

    async def _acquire_profile(self) -> None:
        """Wait until no other process has the profile open.

        Chromium refuses a second instance on one user-data dir, so a submit
        approved in the UI while a prefill run is going would otherwise crash
        into needs_human. With the lock it just starts when the prefill ends.
        """
        profile_dir().mkdir(parents=True, exist_ok=True)
        self._lock = open(profile_dir() / ".jobpilot.lock", "a")
        waited = False
        while True:
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if not waited:
                    print("browser profile is in use by another JobPilot run; waiting for it...", flush=True)
                    waited = True
                await asyncio.sleep(1.0)

    def _release_profile(self) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    async def __aenter__(self) -> "Browser":
        from playwright.async_api import async_playwright

        await self._acquire_profile()
        try:
            self._pw = await async_playwright().start()
            self.context = await self._pw.chromium.launch_persistent_context(
                str(profile_dir()), headless=self.headless, viewport={"width": 1280, "height": 900}
            )
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self.context is not None:
                await self.context.close()
            if self._pw is not None:
                await self._pw.stop()
        finally:
            self._release_profile()

    async def new_page(self):
        return await self.context.new_page()


async def open_form(page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001 - chatty pages never go idle; the DOM is enough
        pass
    await page.wait_for_timeout(800)


async def blocker(page) -> Optional[str]:
    """'captcha' / 'login' when a human has to step in, else None."""
    if await page.evaluate(CAPTCHA_JS):
        return "captcha"
    if await page.evaluate(LOGIN_JS) or any(
        k in page.url.lower() for k in ("/login", "/signin", "/sign-in", "/sso")
    ):
        return "login"
    return None


async def extract_fields(page) -> list[FormField]:
    return [FormField.from_js(r) for r in await page.evaluate(EXTRACT_JS)]


async def _pick_combobox(page, loc, value: str) -> Optional[str]:
    await loc.click()
    await page.wait_for_timeout(300)
    options = page.locator('[role="option"]')
    texts = [t.strip() for t in await options.all_inner_texts()]
    pick = choose_option(value, texts)
    if pick is None and value:
        await loc.fill(value[:40])
        await page.wait_for_timeout(500)
        texts = [t.strip() for t in await options.all_inner_texts()]
        pick = choose_option(value, texts)
    if pick is None:
        await loc.press("Escape")
        return None
    await options.nth(texts.index(pick)).click()
    return pick


def _option(page, field: FormField, value: str):
    return page.locator(f'[data-jp-option="{field.id}:{field.options.index(value)}"]')


async def execute(page, plan: FillPlan, fields: list[FormField]) -> list[str]:
    """Carry out the plan. Returns problems (each one means `needs_human`)."""
    by_id = {f.id: f for f in fields}
    problems: list[str] = []
    for action in plan.actions:
        loc = page.locator(f'[data-jp-id="{action.field_id}"]')
        try:
            if action.kind == "file":
                await loc.set_input_files(action.value)
            elif action.kind in ("text", "textarea"):
                await loc.fill(action.value)
            elif action.kind == "select":
                await loc.select_option(label=action.value)
            elif action.kind in ("radio", "checkbox"):
                opt = _option(page, by_id[action.field_id], action.value)
                # Styled radios hide the real input; a DOM click still fires React's change.
                await opt.evaluate("e => { if (!e.checked) e.click(); }")
            elif action.kind == "combobox":
                picked = await _pick_combobox(page, loc, action.value)
                if picked is None:
                    problems.append(f"'{action.label}': no dropdown option matches '{action.value}'")
                else:
                    action.value = picked
        except Exception as exc:  # noqa: BLE001 - any widget failure goes to a human
            problems.append(f"'{action.label}': could not fill ({type(exc).__name__}: {str(exc)[:120]})")
    return problems


async def verify(page, plan: FillPlan, fields: list[FormField]) -> list[str]:
    """Re-read every filled control and report the ones that didn't stick."""
    by_id = {f.id: f for f in fields}
    problems: list[str] = []
    for action in plan.actions:
        loc = page.locator(f'[data-jp-id="{action.field_id}"]')
        try:
            if action.kind == "file":
                ok = await loc.evaluate("e => e.files && e.files.length > 0")
            elif action.kind in ("text", "textarea"):
                ok = normalize(await loc.input_value()) == normalize(action.value)
            elif action.kind == "select":
                ok = await loc.evaluate("(e, v) => e.options[e.selectedIndex]?.text.trim() === v", action.value)
            elif action.kind in ("radio", "checkbox"):
                ok = await _option(page, by_id[action.field_id], action.value).is_checked()
            else:
                ok = True  # comboboxes are confirmed by picking an option
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            problems.append(f"'{action.label}' did not keep its value")
    return problems


async def page_errors(page) -> list[str]:
    return await page.evaluate(ERRORS_JS)


async def screenshot(page, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(path), full_page=True)
    return path


async def wait_until_closed(page) -> None:
    """Leave a page open for the human; return when they close it."""
    try:
        await page.wait_for_event("close", timeout=0)
    except Exception:  # noqa: BLE001 - browser closed entirely
        pass
