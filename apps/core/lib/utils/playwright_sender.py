
import asyncio
import logging
import random
import re
from datetime import datetime
from pathlib import Path
from apps.core.lib.utils.whatsapp_sender import WhatsAppSender, SendResult



logger = logging.getLogger(__name__)


class PlaywrightSender(WhatsAppSender):
    """
    WhatsApp Web automation via Playwright async API.
    Implements all 8 stealth behaviours defined in the class docstring above.

    Extends WhatsAppSender (abstract interface from src/whatsapp_sender.py).
    The Scheduler and CLI only talk to WhatsAppSender — they never
    import PlaywrightSender directly. This keeps the swap path clean.

    First run:  browser opens → QR code shown → scan with phone → session saved
    Later runs: session loads from .sessions/ → no QR needed
    """

    # ── WhatsApp Web CSS selectors ─────────────────────────────
    # These target specific UI elements in WhatsApp Web's HTML.
    # If WhatsApp updates their UI and selectors break, update here only.
    SEL = {
        # The chat list on the left — confirms we're logged in
        "chat_list":     'div[aria-label="Chat list"]',

        # QR code shown on first login
        "qr_code":       'canvas[aria-label="Scan me!"], div[data-ref]',

        # The text input box at the bottom of a chat
        "msg_input":     'div[aria-label="Type a message"]',

        # The tick icon that appears after a message is delivered
        # data-icon="msg-check"    = single grey tick (sent to server)
        # data-icon="msg-dblcheck" = double grey tick (delivered to phone)
        # Either confirms success — we don't need to wait for blue ticks
        "sent_tick":     'span[data-icon="msg-check"], '
                         'span[data-icon="msg-dblcheck"]',

        # Paperclip / attachment button
        "attach_btn":    'span[data-icon="attach-menu-plus"]',

        # Caption input when sending an image
        "caption_input": 'div[aria-label="Add a caption"]',
    }

    # ── Text that indicates a number is not on WhatsApp ────────
    # WhatsApp Web shows one of these strings in a modal when the
    # phone number navigated to is not a registered WhatsApp account.
    INVALID_TEXTS = [
        "phone number shared via url is invalid",
        "not on whatsapp",
        "invalid phone number",
    ]

    def __init__(self, cfg):
        """
        Args:
            cfg: AppConfig instance — provides delay settings and paths.
        """
        self._cfg       = cfg
        self._log       = logging.getLogger(self.__class__.__name__)

        # Paths
        self._sess_path = Path(".sessions") / "whatsapp_session"
        self._ss_path   = Path("screenshots")

        # Playwright objects — initialized in connect()
        self._pw      = None   # Playwright instance
        self._ctx     = None   # Browser context (persistent, holds session)
        self._page    = None   # Active browser tab

        # STEALTH 8: Tab rotation tracking
        # After _rotate_after messages, navigate home to reset session patterns
        self._msgs_on_tab  = 0
        self._rotate_after = random.randint(8, 12)

    # ── CONNECTION ─────────────────────────────────────────────

    async def connect(self) -> bool:
        """
        Launch browser with persistent session.

        On first run: QR code appears, user scans with phone.
                      Session is saved to .sessions/ automatically.
        All later runs: session loads silently, no QR needed.

        STEALTH 5: headless=False — WhatsApp Web detects headless.
                   Viewport 1280×800 — matches a real laptop screen.

        Returns:
            True if connected and chat list is visible.

        Raises:
            ConnectionError if connection could not be established.
        """
        # Playwright is imported here (deferred import) so that
        # modules that don't need the browser (--preview, --dry-run)
        # don't need Playwright installed just to import this file.
        from playwright.async_api import async_playwright

        self._log.info("Launching browser...")
        self._sess_path.mkdir(parents=True, exist_ok=True)
        self._ss_path.mkdir(exist_ok=True)

        self._pw = await async_playwright().start()

        # launch_persistent_context = browser + profile in one call.
        # user_data_dir stores cookies, localStorage, session tokens.
        # This is what makes the WhatsApp login persist between runs.
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._sess_path),
            headless=False,                            # STEALTH 5
            viewport={"width": 1280, "height": 800},   # STEALTH 5
            locale="en-US",
            timezone_id="Africa/Lagos",                # Match Nigeria timezone
            args=[
                "--no-sandbox",
                # Hides the "Chrome is controlled by automated software" banner
                # and removes the navigator.webdriver=true JS property
                # that WhatsApp Web could detect
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )

        # Use the first existing tab or open a new one
        self._page = (
            self._ctx.pages[0]
            if self._ctx.pages
            else await self._ctx.new_page()
        )

        self._log.info("Navigating to WhatsApp Web...")
        await self._page.goto(
            "https://web.whatsapp.com",
            wait_until="domcontentloaded"
        )

        try:
            # Wait for either the chat list (logged in) or QR code (first run)
            await self._page.wait_for_selector(
                f"{self.SEL['chat_list']}, {self.SEL['qr_code']}",
                timeout=30_000   # 30 seconds
            )

            # Check which element appeared
            chat_list = await self._page.query_selector(self.SEL["chat_list"])

            if chat_list:
                self._log.info("Session loaded — no QR scan needed.")
                return True

            # QR code appeared — guide the user to scan
            print("\n" + "=" * 55)
            print("  SCAN QR CODE TO LOG IN TO WHATSAPP")
            print("=" * 55)
            print("  1. Open WhatsApp on your phone")
            print("  2. Tap Menu (⋮) → Linked Devices → Link a Device")
            print("  3. Point your phone camera at the QR code")
            print("     shown in the browser window that just opened")
            print("  You have 2 minutes. The session will be saved")
            print("  automatically — you won't need to scan again.")
            print("=" * 55 + "\n")

            # Wait up to 2 minutes for the scan to complete
            await self._page.wait_for_selector(
                self.SEL["chat_list"],
                timeout=120_000   # 2 minutes
            )
            self._log.info("QR scanned. Session saved for future runs.")
            return True

        except Exception as e:
            raise ConnectionError(
                f"Could not connect to WhatsApp Web.\n"
                f"Error: {e}\n"
                f"Check your internet connection and run --setup again."
            )

    async def disconnect(self) -> None:
        """
        Close the browser cleanly.
        CRITICAL: Never delete .sessions/ — it holds the saved login.
        Always called in a finally block so it runs even on crash.
        """
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
            self._log.info("Browser closed cleanly.")
        except Exception as e:
            self._log.error(f"Error during disconnect: {e}")

    async def is_connected(self) -> bool:
        """
        Check if the WhatsApp session is still alive.
        Called by Scheduler at the start of each session.
        If False: Scheduler attempts to reconnect before proceeding.
        """
        try:
            if not self._page:
                return False
            el = await self._page.query_selector(self.SEL["chat_list"])
            return el is not None
        except Exception:
            return False

    # ── STEALTH HELPERS ────────────────────────────────────────

    async def _rotate_tab(self):
        """
        STEALTH 8: Navigate home after N messages to reset session patterns.

        WhatsApp's browser-side code tracks navigation patterns.
        Sending message after message to different URLs in a tight
        loop looks different from normal browsing behaviour.
        Navigating home and pausing resets this accumulated pattern.

        N (self._rotate_after) is randomized per cycle so the
        rotation itself doesn't happen at a predictable interval.
        """
        if self._msgs_on_tab >= self._rotate_after:
            self._log.info(
                f"Tab rotation after {self._msgs_on_tab} messages — "
                f"resetting session pattern..."
            )
            await self._page.goto(
                "https://web.whatsapp.com",
                wait_until="domcontentloaded"
            )
            # Pause as if the user is looking at their chat list
            await asyncio.sleep(random.uniform(3, 6))

            # Reset counters — new threshold is random (8–12)
            self._msgs_on_tab  = 0
            self._rotate_after = random.randint(8, 12)
            self._log.debug(
                f"Tab rotated. Next rotation after {self._rotate_after} messages."
            )

    async def _mouse_human_to(self, target_x: int, target_y: int):
        """
        STEALTH 3: Move mouse via a random intermediate position.

        Human mouse movement is never a straight teleport.
        We move to a random point first, pause, then move to target.
        This prevents the cursor jumping directly to an input element
        which is a detectable bot pattern.

        Args:
            target_x: Final x coordinate (center of target element)
            target_y: Final y coordinate (center of target element)
        """
        # Random intermediate point anywhere on the visible page
        mid_x = random.randint(150, 1100)
        mid_y = random.randint(80,  650)

        await self._page.mouse.move(mid_x, mid_y)
        await asyncio.sleep(random.uniform(0.3, 0.8))   # Natural pause
        await self._page.mouse.move(target_x, target_y)
        await asyncio.sleep(random.uniform(0.1, 0.3))   # Settle on target

    async def _type_human(self, selector: str, message: str):
        """
        STEALTH 1 + STEALTH 6: Type message exactly like a human.

        Text segments: typed character-by-character with random ms delay.
        Emoji segments: injected via JavaScript to prevent garbled chars.
        Long messages: occasional mid-message thinking pause.

        WHY page.type() AND NOT page.fill():
            page.fill() sets the input value directly in the DOM.
            It fires NO keyboard events (keydown, keypress, keyup).
            WhatsApp Web uses keyboard events to detect typing.
            Absence of keyboard events = detectable bot pattern.

            page.type() fires real keyboard events per character.
            With a random delay between each character, the event
            timing is statistically identical to a human typing.

        Args:
            selector: CSS selector of the message input element.
            message:  The full message string to type.
        """
        # ── Split message into text and emoji segments ──────────
        # Emoji are multi-byte Unicode characters. page.type() can
        # garble them on some OS/keyboard combinations. We handle
        # them separately via JavaScript injection (STEALTH 6).
        emoji_re = re.compile(
            "["
            "\U0001F600-\U0001F64F"   # Emoticons (😊👋🙏)
            "\U0001F300-\U0001F5FF"   # Symbols & pictographs
            "\U0001F680-\U0001F6FF"   # Transport & map symbols
            "\U0001F1E0-\U0001F1FF"   # Country flags
            "\U00002702-\U000027B0"   # Dingbats (✂)
            "\U000024C2-\U0001F251"   # Enclosed characters
            "\U0001f926-\U0001f937"   # Supplemental symbols (🤷)
            "\U00010000-\U0010ffff"   # Other emoji
            "\u2640-\u2642"           # Gender symbols
            "\u2600-\u2B55"           # Misc (☀🔴)
            "\u200d"                  # Zero-width joiner (compound emoji)
            "\ufe0f"                  # Variation selector
            "]+",
            flags=re.UNICODE
        )

        # Build list of (type, content) tuples in message order
        text_parts  = emoji_re.split(message)
        emoji_parts = emoji_re.findall(message)
        segments    = []
        for i, text in enumerate(text_parts):
            if text:
                segments.append(("text", text))
            if i < len(emoji_parts):
                segments.append(("emoji", emoji_parts[i]))

        d          = self._cfg.delays
        char_count = 0

        for kind, content in segments:

            if kind == "text":
                # ── STEALTH 1: Type character by character ──────
                for char in content:
                    await self._page.type(
                        selector,
                        char,
                        delay=random.uniform(
                            d.type_speed_min,
                            d.type_speed_max
                        )
                    )
                    char_count += 1

                    # Mid-message thinking pause for longer messages.
                    # A real person sometimes pauses to think about
                    # phrasing mid-sentence. We simulate that here.
                    if (
                        len(message) > 80
                        and char_count % random.randint(40, 60) == 0
                    ):
                        await asyncio.sleep(random.uniform(0.8, 2.0))

            elif kind == "emoji":
                # ── STEALTH 6: Inject emoji via JavaScript ───────
                # This inserts the emoji at the current cursor position
                # in the WhatsApp input box without using keyboard events.
                # A separate input event is dispatched so WhatsApp
                # registers the change and enables the send button.
                try:
                    await self._page.evaluate(
                        """(args) => {
                            const el = document.querySelector(args.selector);
                            if (!el) return;
                            const sel = window.getSelection();
                            if (!sel || !sel.rangeCount) return;
                            const range = sel.getRangeAt(0);
                            const node  = document.createTextNode(args.emoji);
                            range.insertNode(node);
                            range.setStartAfter(node);
                            range.setEndAfter(node);
                            sel.removeAllRanges();
                            sel.addRange(range);
                            // Tell WhatsApp the content changed
                            el.dispatchEvent(
                                new Event('input', { bubbles: true })
                            );
                        }""",
                        {"selector": selector, "emoji": content}
                    )
                    await asyncio.sleep(random.uniform(0.05, 0.2))
                except Exception:
                    # Fallback: try typing directly
                    # May garble on some systems but better than crashing
                    self._log.debug(f"JS emoji injection failed — falling back to type()")
                    await self._page.type(selector, content, delay=50)

    async def _check_invalid_number(self) -> bool:
        """
        Check if WhatsApp is showing an 'invalid/unregistered number' modal.

        After navigating to a chat URL, WhatsApp Web may show a modal
        with text like "Phone number shared via url is invalid" if the
        number is not registered. We check for these strings in the
        page body before attempting to type anything.

        Returns:
            True  → number is NOT on WhatsApp, mark INVALID_NUMBER
            False → number appears valid, proceed with sending
        """
        try:
            body_text = (await self._page.inner_text("body")).lower()
            return any(pattern in body_text for pattern in self.INVALID_TEXTS)
        except Exception:
            # If we can't read the body, assume valid and let send fail naturally
            return False

    async def _take_screenshot(self, order_id: str) -> str:
        """
        Capture a screenshot of the browser at the moment of delivery.
        Saved as: screenshots/{order_id}_{timestamp}.png

        This gives you visual proof that each message was sent,
        showing the message in the chat with the delivery tick.

        Args:
            order_id: Used in the filename so screenshots are traceable.

        Returns:
            Path string to the saved screenshot, or "" on failure.
        """
        try:
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            # Sanitize order_id — remove characters invalid in filenames
            safe_id  = re.sub(r"[^\w\-]", "_", str(order_id))
            filename = f"{safe_id}_{ts}.png"
            path     = self._ss_path / filename

            await self._page.screenshot(
                path=str(path),
                full_page=False   # Capture viewport only, not full page
            )
            self._log.debug(f"Screenshot saved: {path}")
            return str(path)
        except Exception as e:
            self._log.warning(f"Screenshot failed: {e}")
            return ""

    # ── SEND METHODS ───────────────────────────────────────────

    async def send_text(
        self,
        phone:    str,
        message:  str,
        order_id: str
    ) -> SendResult:
        """
        Send a text message to one WhatsApp number.
        Applies all 8 stealth techniques in order.

        Flow:
          1. Tab rotation check         (STEALTH 8)
          2. Navigate to chat URL       (STEALTH 4 — no ?text=)
          3. Wait for chat to load
          4. Check for invalid number modal
          5. Pre-typing pause           (STEALTH 2)
          6. Occasional scroll          (STEALTH 7)
          7. Human mouse to input       (STEALTH 3)
          8. Click input, type message  (STEALTH 1 + 6)
          9. Press Enter
         10. Wait for delivery tick
         11. Screenshot for proof
         12. Return SendResult

        Args:
            phone:    13-digit normalized phone e.g. "2348038365784"
            message:  Full rendered message string
            order_id: For logging and screenshot naming

        Returns:
            SendResult with status SENT | FAILED | INVALID_NUMBER
        """
        self._log.info(f"→ Sending to +{phone} [Order: {order_id}]")

        try:
            # ── STEALTH 8: Rotate tab if threshold reached ──────
            await self._rotate_tab()

            # ── STEALTH 4: Navigate WITHOUT ?text= parameter ────
            # Clean URL — no pre-filled text
            url = f"https://web.whatsapp.com/send?phone={phone}"
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=20_000
            )

            # Wait for the message input to appear (chat loaded)
            try:
                await self._page.wait_for_selector(
                    self.SEL["msg_input"],
                    timeout=15_000
                )
            except Exception:
                # Input didn't appear — check if it's an invalid number
                if await self._check_invalid_number():
                    self._log.info(f"  ✗ Not on WhatsApp: +{phone}")
                    return SendResult(
                        success=False,
                        status="INVALID_NUMBER",
                        error_message="Phone not registered on WhatsApp"
                    )
                # Genuine timeout — not invalid number, just slow load
                raise TimeoutError(
                    f"Chat did not load within 15s for +{phone}"
                )

            # Double-check for invalid number after load
            # (modal sometimes appears after the input is visible)
            if await self._check_invalid_number():
                self._log.info(f"  ✗ Not on WhatsApp: +{phone}")
                return SendResult(
                    success=False,
                    status="INVALID_NUMBER",
                    error_message="Phone not registered on WhatsApp"
                )

            # ── STEALTH 2: Pre-typing pause ─────────────────────
            # Simulate the user reading the chat before replying
            pre_pause = random.uniform(
                self._cfg.delays.pre_type_min,
                self._cfg.delays.pre_type_max
            )
            self._log.debug(f"  Pre-type pause: {pre_pause:.1f}s")
            await asyncio.sleep(pre_pause)

            # ── STEALTH 7: Occasional random scroll ─────────────
            # Simulate glancing at previous messages before typing
            if random.choice([True, False]):
                scroll_up = random.uniform(100, 400)
                await self._page.evaluate(f"window.scrollBy(0, -{scroll_up})")
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await self._page.evaluate("window.scrollBy(0, 10000)")
                await asyncio.sleep(random.uniform(0.3, 0.8))

            # ── STEALTH 3: Human mouse movement ─────────────────
            # Move to a random position first, then to the input box
            el = await self._page.query_selector(self.SEL["msg_input"])
            if el:
                box = await el.bounding_box()
                if box:
                    center_x = int(box["x"] + box["width"]  / 2)
                    center_y = int(box["y"] + box["height"] / 2)
                    await self._mouse_human_to(center_x, center_y)

            # Click the input to focus it
            await self._page.click(self.SEL["msg_input"])
            await asyncio.sleep(random.uniform(0.2, 0.5))

            # ── STEALTH 1 + 6: Type message ─────────────────────
            await self._type_human(self.SEL["msg_input"], message)

            # Small pause after finishing typing — human habit
            await asyncio.sleep(random.uniform(0.3, 0.8))

            # Press Enter to send
            await self._page.keyboard.press("Enter")
            self._log.debug("  Message sent. Waiting for delivery tick...")

            # Wait for send confirmation tick
            # Single tick (msg-check) = delivered to WhatsApp server
            # Double tick (msg-dblcheck) = delivered to recipient's phone
            # Either is sufficient confirmation — we don't wait for blue ticks
            try:
                await self._page.wait_for_selector(
                    self.SEL["sent_tick"],
                    timeout=15_000
                )
                self._log.info(f"  ✓ Delivered: +{phone}")
            except Exception:
                # Tick didn't appear within 15s.
                # Message was likely sent but we couldn't confirm.
                # Mark as SENT anyway — better than a false FAILED.
                self._log.warning(
                    f"  ⚠ Tick timeout for +{phone} — "
                    f"message likely delivered (unconfirmed)"
                )

            # Screenshot for delivery proof
            screenshot_path = await self._take_screenshot(order_id)

            # Increment tab message counter (for STEALTH 8)
            self._msgs_on_tab += 1

            return SendResult(
                success=True,
                status="SENT",
                screenshot_path=screenshot_path
            )

        except Exception as e:
            self._log.error(
                f"  ✗ Failed for +{phone}: {e}",
                exc_info=True
            )
            return SendResult(
                success=False,
                status="FAILED",
                error_message=str(e)
            )

    async def send_image(
        self,
        phone:      str,
        image_path: str,
        caption:    str,
        order_id:   str
    ) -> SendResult:
        """
        Send an image with a caption to one WhatsApp number.
        Used when cfg.image_path is configured in config.py.

        Flow:
          1. Navigate to chat (same as send_text)
          2. Click attachment button (paperclip icon)
          3. Upload image file via file chooser
          4. Wait for image preview to load
          5. Type caption in caption input
          6. Press Enter to send

        Args:
            phone:      13-digit normalized phone
            image_path: Local path to image file e.g. "data/product.jpg"
            caption:    Text caption rendered from message template
            order_id:   For logging and screenshot naming

        Returns:
            SendResult with status SENT | FAILED | INVALID_NUMBER
        """
        self._log.info(f"→ Sending image to +{phone} [Order: {order_id}]")

        try:
            await self._rotate_tab()

            await self._page.goto(
                f"https://web.whatsapp.com/send?phone={phone}",
                wait_until="domcontentloaded",
                timeout=20_000
            )

            try:
                await self._page.wait_for_selector(
                    self.SEL["msg_input"],
                    timeout=15_000
                )
            except Exception:
                if await self._check_invalid_number():
                    return SendResult(
                        success=False,
                        status="INVALID_NUMBER",
                        error_message="Phone not registered on WhatsApp"
                    )
                raise

            if await self._check_invalid_number():
                return SendResult(
                    success=False,
                    status="INVALID_NUMBER",
                    error_message="Phone not registered on WhatsApp"
                )

            # Click the attachment (paperclip) button
            await self._page.click(self.SEL["attach_btn"])
            await asyncio.sleep(random.uniform(0.5, 1.0))

            # Handle the file chooser dialog
            async with self._page.expect_file_chooser() as fc_info:
                await self._page.click('input[accept*="image"]')
            file_chooser = await fc_info.value
            await file_chooser.set_files(image_path)

            # Wait for the image preview to appear in the media editor
            await self._page.wait_for_selector(
                'div[data-testid="media-editor"]',
                timeout=10_000
            )
            await asyncio.sleep(1.0)

            # Type caption in the caption input field
            try:
                await self._page.click(self.SEL["caption_input"])
                await self._type_human(self.SEL["caption_input"], caption)
            except Exception:
                self._log.warning(
                    "Caption input not found — sending image without caption"
                )

            # Send with Enter
            await self._page.keyboard.press("Enter")
            await asyncio.sleep(2.0)   # Image upload takes longer to confirm

            screenshot_path = await self._take_screenshot(order_id)
            self._msgs_on_tab += 1

            return SendResult(
                success=True,
                status="SENT",
                screenshot_path=screenshot_path
            )

        except Exception as e:
            self._log.error(
                f"  ✗ Image send failed for +{phone}: {e}",
                exc_info=True
            )
            return SendResult(
                success=False,
                status="FAILED",
                error_message=str(e)
            )
