"""The visible browser for Greenhouse autofill, driven by Playwright from a thread of its own.

Playwright is optional. Nothing here imports it until the user asks for a browser, so the app
starts and every other feature works without it; asking without it gives ``SETUP_MESSAGE``.

Playwright's synchronous API belongs to the thread that started it, while each web request runs
on any worker thread. So one dedicated thread owns Playwright, the browser and its page, and every
call is handed to it. The browser gets a fresh context: no saved cookies, sign-ins or downloads.

What this module may do to a page is deliberately small: read the form (``INSPECT``), put a value
into a field with ``fill``, give the file input a file with ``set_input_files``, and read the
values back. It never clicks, presses or types keys, so nothing it does can submit a form. While
it fills, a guard also stops any submission the page itself attempts (a submit event, a script
calling ``form.submit()`` or ``requestSubmit()``, a form post navigation) and counts it.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, replace

from .autofill_plan import MAX_FIELDS, FormField

SETUP_MESSAGE = ("Autofill needs Playwright, an optional add-on that isn't installed. Install it with "
                 "`pip install -r requirements-autofill.txt`, run `python -m playwright install chromium`, and "
                 "restart ResuApply. Everything else works without it.")
PAGE_CHANGED = ("The page changed or closed while ResuApply was working on it, so that step stopped. Choose "
                "Check the form again, or fill it in yourself.")
LOAD_SECONDS = 30
ACTION_SECONDS = 10
SETTLE_SECONDS = 5
CALL_SECONDS = 120  # the longest a web request waits for the browser thread, beyond a fill's own steps


def fill_seconds(steps: int) -> float:
    """How long a fill may take: each step can use its action timeout to fill and again to read back."""
    return CALL_SECONDS + steps * 2 * ACTION_SECONDS

# Installed in every page of the browser before the page's own scripts. Inactive until a fill starts.
GUARD = """
(() => {
  if (window.__resuapplyGuard) return;
  const guard = { active: false, blocked: 0 };
  Object.defineProperty(window, "__resuapplyGuard", { value: guard });
  window.addEventListener("submit", (event) => {
    if (guard.active) { guard.blocked++; event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  const submit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    if (guard.active) { guard.blocked++; return; }
    return submit.call(this);
  };
  const requestSubmit = HTMLFormElement.prototype.requestSubmit;
  if (requestSubmit) HTMLFormElement.prototype.requestSubmit = function (...args) {
    if (guard.active) { guard.blocked++; return; }
    return requestSubmit.apply(this, args);
  };
})();
"""

# Reads the page: where it is, whether it shows a CAPTCHA or a sign-in, and the application form's controls.
INSPECT = """
(maxFields) => {
  const shown = (el) => {
    if (!el.isConnected) return false;
    if (typeof el.checkVisibility === "function" && !el.checkVisibility({ visibilityProperty: true })) return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const big = (el) => { const box = el.getBoundingClientRect(); return box.width > 40 && box.height > 40; };
  const words = (node) => {
    const copy = node.cloneNode(true);
    copy.querySelectorAll("input, select, textarea, button, option, script, style").forEach((n) => n.remove());
    return (copy.textContent || "").replace(/\\s+/g, " ").trim();
  };
  const captchaFrames = [...document.querySelectorAll("iframe")].filter((f) =>
    /captcha|turnstile|arkoselabs|funcaptcha/i.test((f.src || "") + " " + (f.title || "")) &&
    !/size=invisible/i.test(f.src || "") && shown(f) && big(f));
  const captchaBoxes = [...document.querySelectorAll(".g-recaptcha, .h-captcha, .cf-turnstile")].filter((el) =>
    el.getAttribute("data-size") !== "invisible" && shown(el) && big(el));
  const out = {
    url: location.href,
    captcha: captchaFrames.length + captchaBoxes.length > 0,
    login: [...document.querySelectorAll("input[type=password]")].some(shown),
    form: false,
    fields: [],
  };
  const form = document.querySelector("form#application-form, form#application_form") ||
    [...document.forms].find((f) =>
      f.querySelector("input[type=file]") && f.querySelector("input[type=email], #email"));
  if (!form) return out;
  out.form = true;
  const unique = (selector) => document.querySelectorAll(selector).length === 1;
  const controls = [...new Set(form.querySelectorAll(
    "input, textarea, select, [role=combobox], [contenteditable=''], [contenteditable=true]"))];
  let index = 0;
  for (const el of controls) {
    const tag = el.tagName.toLowerCase();
    const type = tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : "";
    if (["hidden", "submit", "button", "reset", "image"].includes(type)) continue;
    if (out.fields.length >= maxFields) { out.tooMany = true; break; }
    index++;
    let key = "index:" + index;
    if (el.id && unique("#" + CSS.escape(el.id))) key = "id:" + el.id;
    else if (el.name && unique('[name="' + CSS.escape(el.name) + '"]')) key = "name:" + el.name;
    const texts = new Set();
    for (const id of (el.getAttribute("aria-labelledby") || "").split(/\\s+/).filter(Boolean)) {
      const node = document.getElementById(id);
      if (node) texts.add(words(node));
    }
    if (!texts.size && el.labels) for (const label of el.labels) { const t = words(label); if (t) texts.add(t); }
    if (!texts.size && el.getAttribute("aria-label")) texts.add(el.getAttribute("aria-label").trim());
    let value = "";
    if (type === "file") value = [...(el.files || [])].map((f) => f.name).join(", ");
    else if (type === "checkbox" || type === "radio") value = el.checked ? "checked" : "";
    else if (tag === "select") value = [...el.selectedOptions].map((o) => o.text).join(", ");
    else if ("value" in el) value = String(el.value || "");
    else value = el.textContent || "";
    out.fields.push({
      key, tag, type, role: (el.getAttribute("role") || "").toLowerCase(),
      label: [...texts].join(" / "), labels: texts.size,
      required: !!el.required || el.getAttribute("aria-required") === "true",
      visible: shown(el), editable: !el.disabled && !el.readOnly && el.getAttribute("aria-readonly") !== "true",
      value, maxLength: typeof el.maxLength === "number" && el.maxLength > 0 ? el.maxLength : null,
      sensitiveSection: !!el.closest(
        "[id*=eeoc i], [class*=eeoc i], [id*=demographic i], [class*=demographic i], " +
        "[id*=self-identification i], [class*=self-identification i], [id*=self_identification i]"),
      autocomplete: el.hasAttribute("list") || ["list", "both"].includes(el.getAttribute("aria-autocomplete")),
    });
  }
  return out;
}
"""


class BrowserError(Exception):
    """The browser couldn't do what was asked. The message is plain and safe to show."""


def installed() -> bool:
    """Whether Playwright is installed, without importing it."""
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


@dataclass(frozen=True)
class LaunchOptions:
    """How the browser starts. The app uses the defaults; tests run it hidden and serve local pages."""

    headless: bool = False
    executable_path: str | None = None  # a Chrome or Chromium to use instead of Playwright's own
    prepare_context: Callable | None = None  # called with each new browser context before a page opens


@dataclass(frozen=True)
class Inspection:
    url: str = ""
    status: int | None = None  # the HTTP status of the page as loaded, when it was just opened
    form: bool = False
    captcha: bool = False
    login: bool = False
    too_many: bool = False
    fields: tuple[FormField, ...] = ()
    closed: bool = False  # the window or tab is gone


@dataclass(frozen=True)
class FillStep:
    key: str
    value: str = ""  # the text to fill
    file_name: str = ""  # with ``file_data``: the file to attach
    file_data: bytes = b""

    @property
    def is_file(self) -> bool:
        return bool(self.file_name)


@dataclass(frozen=True)
class FillOutcome:
    key: str
    done: bool  # the value was put in (the read-back may still differ)
    read: str = ""  # the field's value read back after filling (for a file: "name (size bytes)")
    read_size: int | None = None  # the attached file's size as the page reports it
    error: str = ""


@dataclass(frozen=True)
class FillResult:
    outcomes: tuple[FillOutcome, ...]
    blocked_submissions: int  # submissions the page attempted during the fill, stopped by the guard
    stopped: str  # "" when every step ran, else "closed" or "navigated"
    after: Inspection


def selector(key: str) -> str:
    """A CSS selector that finds the control the inspection keyed, by its id or name."""
    kind, _, value = key.partition(":")
    if kind not in ("id", "name") or not value:
        raise ValueError(key)
    quoted = (value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\a ").replace("\r", "\\d ")
              .replace("\f", "\\c "))
    return f'[{kind}="{quoted}"]'


class FormBrowser:
    """One visible browser window with one page, for one form at a time."""

    def __init__(self, options: LaunchOptions | None = None):
        self.options = options or LaunchOptions()
        self._executor: ThreadPoolExecutor | None = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._navigations = 0

    # ------------------------------------------------------------ called from web requests

    def _call(self, function, *args, timeout: float = CALL_SECONDS):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="resuapply-browser")
        try:
            return self._executor.submit(function, *args).result(timeout=timeout)
        except FutureTimeout:
            raise BrowserError("The browser stopped responding. Close it with Close the browser and try "
                               "again.") from None
        except BrowserError:
            raise
        except Exception as exc:
            # Any other Playwright failure (a page that navigated or closed mid-step, a crashed browser) is
            # reported like the rest, never as a server error.
            if type(exc).__module__.startswith("playwright"):
                raise BrowserError(PAGE_CHANGED) from None
            raise

    def open(self, url: str) -> Inspection:
        """Open the page in the visible browser (starting it if needed) and read its form."""
        if not installed():
            raise BrowserError(SETUP_MESSAGE)
        return self._call(self._open, url)

    def inspect(self) -> Inspection:
        if self._page is None:
            return Inspection(closed=True)
        return self._call(self._inspect)

    def fill(self, steps: list[FillStep], still_expected: Callable[[str], bool]) -> FillResult:
        """Fill the steps in order and read each value back; stop if the page leaves ``still_expected`` URLs."""
        # A long form legitimately takes longer than one call: wait for every step rather than leave the
        # browser thread filling while the page reports it as stuck.
        return self._call(self._fill, steps, still_expected, timeout=fill_seconds(len(steps)))

    def close(self) -> None:
        """Close the browser and stop Playwright. Safe to call when nothing is open."""
        if self._executor is None:
            return
        try:
            self._call(self._close)
        except BrowserError:
            pass

    def shutdown(self) -> None:
        """Close everything and end the browser thread (app shutdown)."""
        self.close()
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None

    # ------------------------------------------------------------ on the browser thread

    def _start(self):
        from playwright.sync_api import Error, sync_playwright

        if self._playwright is None:
            self._playwright = sync_playwright().start()
        if self._browser is not None and self._browser.is_connected():
            return
        try:
            self._browser = self._playwright.chromium.launch(headless=self.options.headless,
                                                             executable_path=self.options.executable_path)
        except Error as exc:
            self._browser = None
            text = str(exc)
            if "Executable doesn't exist" in text or "executable doesn't exist" in text.lower():
                raise BrowserError("Playwright's Chromium browser isn't installed. Run `python -m playwright "
                                   "install chromium`, then try again.") from None
            if "DISPLAY" in text or "XServer" in text or "X server" in text:
                raise BrowserError("The browser window couldn't open: autofill needs a desktop session that can "
                                   "show windows.") from None
            raise BrowserError("The browser couldn't start. Check that Playwright's Chromium is installed "
                               "(`python -m playwright install chromium`), then try again.") from None

    def _new_page(self):
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        self._context = self._browser.new_context(accept_downloads=False, no_viewport=not self.options.headless)
        self._context.set_default_timeout(ACTION_SECONDS * 1000)
        self._context.add_init_script(GUARD)
        if self.options.prepare_context is not None:
            self.options.prepare_context(self._context)
        page = self._context.new_page()
        self._navigations = 0

        def navigating(request):
            # A request for a new document in the main frame. Same-page changes made through the History API
            # or the URL's #fragment request nothing, so they don't count as leaving the form.
            if request.is_navigation_request() and request.frame == page.main_frame:
                self._navigations += 1

        page.on("request", navigating)
        self._page = page
        return page

    def _open(self, url: str) -> Inspection:
        from playwright.sync_api import Error, TimeoutError as PlaywrightTimeout

        self._start()
        page = self._new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=LOAD_SECONDS * 1000)
        except PlaywrightTimeout:
            raise BrowserError(f"The form didn't load within {LOAD_SECONDS} seconds. It stays open in the browser; "
                               "fill it in yourself or try again.") from None
        except Error:
            raise BrowserError("The form couldn't be loaded. Check your connection and the posting link, then try "
                               "again.") from None
        for state in ("load", "networkidle"):  # a script that moves the page on runs before the form is read
            try:
                page.wait_for_load_state(state, timeout=SETTLE_SECONDS * 1000)
            except PlaywrightTimeout:
                pass  # a slow image or script; the form is read as it is
        inspection = self._inspect()
        status = response.status if response is not None else None
        return replace(inspection, status=status)

    def _inspect(self) -> Inspection:
        from playwright.sync_api import Error

        page = self._page
        if page is None or page.is_closed() or self._browser is None or not self._browser.is_connected():
            return Inspection(closed=True)
        raw = None
        for _ in range(2):  # a page that is still navigating gets one more chance to settle
            try:
                raw = page.evaluate(INSPECT, MAX_FIELDS)
                break
            except Error:
                if page.is_closed():
                    return Inspection(closed=True)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=ACTION_SECONDS * 1000)
                except Error:
                    pass
        if not isinstance(raw, dict):
            return Inspection(url=page.url)
        fields = raw.get("fields") if isinstance(raw.get("fields"), list) else []
        parsed = tuple(f for f in (FormField.from_page(item) for item in fields[:MAX_FIELDS]) if f is not None)
        return Inspection(url=page.url, form=raw.get("form") is True, captcha=raw.get("captcha") is True,
                          login=raw.get("login") is True, too_many=raw.get("tooMany") is True, fields=parsed)

    def _fill(self, steps: list[FillStep], still_expected: Callable[[str], bool]) -> FillResult:
        from playwright.sync_api import Error

        page = self._page
        if page is None or page.is_closed():
            return FillResult((), 0, "closed", Inspection(closed=True))
        blocked_posts = [0]

        def no_form_posts(route):
            request = route.request
            if request.is_navigation_request() and request.method not in ("GET", "HEAD"):
                blocked_posts[0] += 1
                route.abort()
            else:
                route.fallback()

        page.route("**/*", no_form_posts)
        blocked = 0
        outcomes: list[FillOutcome] = []
        stopped = ""
        start = self._navigations
        try:
            page.evaluate(GUARD)  # in case the page was opened before the guard was installed
            page.evaluate("() => { const g = window.__resuapplyGuard; g.blocked = 0; g.active = true; }")
            done: list[tuple[FillStep, object]] = []
            for step in steps:
                if page.is_closed():
                    stopped = "closed"
                    break
                if self._navigations != start or not still_expected(page.url):
                    stopped = "navigated"
                    break
                locator = page.locator(selector(step.key))
                try:
                    if locator.count() != 1:
                        outcomes.append(FillOutcome(step.key, False, error="The field is no longer on the form."))
                        continue
                    if step.is_file:
                        locator.set_input_files({"name": step.file_name, "mimeType": "application/pdf",
                                                 "buffer": step.file_data})
                    else:
                        locator.fill(step.value)
                except Error:
                    outcomes.append(FillOutcome(step.key, False, error="ResuApply couldn't fill this field."))
                    continue
                done.append((step, locator))
            for step, locator in done:
                if stopped or page.is_closed():
                    outcomes.append(FillOutcome(step.key, True, error="Not read back: the page changed."))
                    continue
                try:
                    if step.is_file:
                        files = locator.evaluate("(el) => [...(el.files || [])].map((f) => [f.name, f.size])")
                        files = files if isinstance(files, list) else []
                        first = files[0] if len(files) == 1 and isinstance(files[0], list) else ["", None]
                        size = first[1] if isinstance(first[1], int) else None
                        read = ", ".join(str(f[0]) for f in files if isinstance(f, list) and f)
                        outcomes.append(FillOutcome(step.key, True, read=read[:500], read_size=size))
                    else:
                        outcomes.append(FillOutcome(step.key, True, read=locator.input_value()[:20000]))
                except Error:
                    outcomes.append(FillOutcome(step.key, True, error="The value couldn't be read back."))
        finally:
            if not page.is_closed():
                try:
                    blocked = int(page.evaluate(
                        "() => { const g = window.__resuapplyGuard; g.active = false; return g.blocked; }") or 0)
                except (Error, TypeError, ValueError):
                    pass
                try:
                    page.unroute("**/*", no_form_posts)
                except Error:
                    pass
        if not stopped and self._navigations != start:
            stopped = "navigated"
        order = {step.key: i for i, step in enumerate(steps)}
        outcomes.sort(key=lambda o: order.get(o.key, 0))
        return FillResult(tuple(outcomes), blocked + blocked_posts[0], stopped, self._inspect())

    def _close(self) -> None:
        for thing in (self._context, self._browser):
            if thing is not None:
                try:
                    thing.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = self._browser = self._context = self._page = None
