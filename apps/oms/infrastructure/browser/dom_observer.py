import asyncio
from typing import Optional

from apps.oms.infrastructure.browser.raw_message import (
    RawMessage,
    MessageDirection,
)
from apps.oms.infrastructure.browser.message_cache import MessageCache
from apps.oms.infrastructure.browser.checkpoint_store import (
    CheckpointStore,
    Checkpoint,
)
from apps.oms.events import dispatcher
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)

# JavaScript MutationObserver injected into the page.
# This watches the WhatsApp chat container for new message nodes.
# When a new message is inserted, it extracts the text, sender,
# timestamp, and direction, then pushes to a global JS queue.
# Python reads this queue via page.evaluate() on a timer.
_MUTATION_OBSERVER_JS = """
() => {
    // Guard: don't inject multiple observers if called again
    if (window.__omsObserverActive) {
        return { status: 'already_active' };
    }

    // Message queue — Python drains this periodically
    window.__omsMessageQueue = window.__omsMessageQueue || [];
    window.__omsObserverActive = true;
    window.__omsMsgCounter = window.__omsMsgCounter || 0;

    // Find the chat message container
    const findContainer = () => {
        return (
            document.querySelector('#main div[role="application"]') ||
            document.querySelector('#main .copyable-area') ||
            document.querySelector('#main')
        );
    };

    const container = findContainer();
    if (!container) {
        window.__omsObserverActive = false;
        return { status: 'container_not_found' };
    }

    // Extract message data from a DOM node
    const extractMessage = (node) => {
        try {
            // Only process actual message rows
            if (!node.getAttribute || node.getAttribute('role') !== 'row') {
                // Check children for a row
                const row = node.querySelector && node.querySelector('div[role="row"]');
                if (!row) return null;
                node = row;
            }

            const isIncoming = node.querySelector('.message-in') !== null;
            const isOutgoing = node.querySelector('.message-out') !== null;
            if (!isIncoming && !isOutgoing) return null;

            // Get text content
            const textEl = node.querySelector(
                '.copyable-text span[class*="selectable"],' +
                '.copyable-text span[dir],' +
                '.message-in .copyable-text,' +
                '.message-out .copyable-text'
            );
            const rawText = textEl ? textEl.innerText.trim() : '';
            if (!rawText) return null;

            // Get timestamp and sender from data attribute
            const tsEl    = node.querySelector('[data-pre-plain-text]');
            const tsAttr  = tsEl ? tsEl.getAttribute('data-pre-plain-text') : '';
            const tsMatch = tsAttr.match(/\\[([^,]+),/);
            const snMatch = tsAttr.match(/\\] ([^:]+):/);

            window.__omsMsgCounter++;

            return {
                rawText:      rawText,
                timestamp:    tsMatch ? tsMatch[1].trim() : '',
                sender:       snMatch ? snMatch[1].trim() : (isOutgoing ? 'You' : 'Unknown'),
                direction:    isIncoming ? 'INCOMING' : 'OUTGOING',
                capturedMs:   Date.now(),
                queueIdx:     window.__omsMsgCounter,
            };
        } catch(e) {
            return null;    // Skip malformed nodes silently
        }
    };

    // Create the MutationObserver
    const observer = new MutationObserver((mutations) => {
        mutations.forEach((mutation) => {
            mutation.addedNodes.forEach((node) => {
                // Only process element nodes (not text/comment nodes)
                if (node.nodeType !== Node.ELEMENT_NODE) return;

                const msg = extractMessage(node);
                if (msg) {
                    window.__omsMessageQueue.push(msg);
                }

                // Also check immediate children (WhatsApp sometimes
                // inserts a wrapper div containing the message row)
                if (node.children) {
                    Array.from(node.children).forEach((child) => {
                        const childMsg = extractMessage(child);
                        if (childMsg) {
                            window.__omsMessageQueue.push(childMsg);
                        }
                    });
                }
            });
        });
    });

    // Observe the container for child additions
    // subtree: true — watch all descendants, not just direct children
    // childList: true — react to added/removed nodes
    observer.observe(container, {
        childList: true,
        subtree:   true,
    });

    // Store reference to allow cleanup later
    window.__omsObserver = observer;

    return { status: 'active', containerTag: container.tagName };
}
"""

# JavaScript to drain the message queue (called by Python on a timer)
_DRAIN_QUEUE_JS = """
() => {
    if (!window.__omsMessageQueue) return [];
    const batch = window.__omsMessageQueue.splice(0);  // Take all, clear
    return batch;
}
"""

# JavaScript to check if the observer is still active
_HEALTH_CHECK_JS = """
() => {
    return {
        active:    !!window.__omsObserverActive,
        queueSize: (window.__omsMessageQueue || []).length,
        msgCount:  window.__omsMsgCounter || 0,
    };
}
"""

# JavaScript to stop the observer cleanly
_STOP_OBSERVER_JS = """
() => {
    if (window.__omsObserver) {
        window.__omsObserver.disconnect();
        window.__omsObserver = null;
    }
    window.__omsObserverActive = false;
    return { status: 'stopped' };
}
"""


class DOMObserver:
    '''
    Live observer that detects new WhatsApp messages via MutationObserver.

    Injects a JavaScript MutationObserver into the WhatsApp Web page.
    The JS fires immediately when a new message node is added to the DOM.
    Python polls the JS message queue every POLL_INTERVAL seconds.

    This design means:
      - Detection latency: ~0ms (JS reacts to DOM mutation instantly)
      - Python overhead: POLL_INTERVAL seconds (only to drain the queue)
      - No selector scanning in the Python poll loop
      - No missed messages (JS buffers everything between Python polls)

    Usage:
        observer = DOMObserver(page, cache, checkpoint_store, cfg)
        task = asyncio.create_task(observer.run())
        # ... later ...
        await observer.stop()
        await task
    '''

    POLL_INTERVAL = 2.0   # Seconds between JS queue drains

    def __init__(
        self,
        page,
        cache:            MessageCache,
        checkpoint_store: CheckpointStore,
        cfg,
    ):
        self._page       = page
        self._cache      = cache
        self._store      = checkpoint_store
        self._cfg        = cfg
        self._running    = False
        self._id_counter = 0
        self._msg_count  = 0   # Messages processed this session
        self._group_name = cfg.whatsapp.group_name

    async def run(self) -> None:
        '''
        Start the live observation loop.
        Injects the MutationObserver, then polls the queue.
        Runs until stop() is called or the task is cancelled.
        '''
        self._running = True
        log.info("Live DOM Observer starting...")

        # Inject the MutationObserver into the page
        injected = await self._inject_observer()
        if not injected:
            log.error(
                "Failed to inject MutationObserver. "
                "Live observation cannot start."
            )
            self._running = False
            return

        await dispatcher.emit(
            "observer.started",
            group=self._group_name,
            poll_interval=self.POLL_INTERVAL
        )

        log.info(
            f"DOM Observer active. Watching: {self._group_name!r}\n"
            f"Poll interval: {self.POLL_INTERVAL}s | "
            f"Detection: instant (MutationObserver)"
        )

        # Main poll loop — drains JS queue every POLL_INTERVAL seconds
        while self._running:
            try:
                await asyncio.sleep(self.POLL_INTERVAL)
                await self._drain_queue()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(
                    f"Observer poll error: {e}\n"
                    "Attempting to re-inject observer..."
                )
                # Try to re-inject the observer if something went wrong
                try:
                    await self._inject_observer()
                except Exception as reinject_err:
                    log.error(f"Re-injection failed: {reinject_err}")
                    await asyncio.sleep(5)  # Brief backoff before retry

        await self._cleanup()
        await dispatcher.emit("observer.stopped", group=self._group_name)
        log.info("DOM Observer stopped.")

    async def stop(self) -> None:
        '''Signal the observer loop to stop cleanly.'''
        self._running = False

    async def _inject_observer(self) -> bool:
        '''
        Inject the JavaScript MutationObserver into the page.
        Returns True if injection succeeded.
        '''
        try:
            result = await self._page.evaluate(_MUTATION_OBSERVER_JS)
            status = result.get("status", "unknown")

            if status == "active":
                log.info(
                    f"MutationObserver injected. "
                    f"Container: {result.get('containerTag', '?')}"
                )
                return True

            elif status == "already_active":
                log.debug("MutationObserver already active — skipping injection.")
                return True

            elif status == "container_not_found":
                log.warning(
                    "WhatsApp chat container not found.\n"
                    "Is the group chat open in the browser?"
                )
                return False

            else:
                log.warning(f"Unexpected injection status: {status!r}")
                return False

        except Exception as e:
            log.error(f"Observer injection error: {e}", exc_info=True)
            return False

    async def _drain_queue(self) -> None:
        '''
        Drain the JavaScript message queue.
        Processes each queued message and emits events.
        Called every POLL_INTERVAL seconds by the main loop.
        '''
        try:
            batch = await self._page.evaluate(_DRAIN_QUEUE_JS)
        except Exception as e:
            log.debug(f"Queue drain error: {e}")
            return

        if not batch:
            return

        log.debug(f"Draining {len(batch)} message(s) from JS queue...")

        for data in batch:
            try:
                await self._process_queued_message(data)
            except Exception as e:
                log.warning(
                    f"Failed to process queued message: {e}\n"
                    f"Data: {data}"
                )
                # Continue processing remaining messages — one failure
                # must never stop the observer

    async def _process_queued_message(self, data: dict) -> None:
        '''
        Convert a raw JS data dict to a RawMessage and emit an event.
        Handles deduplication via MessageCache.
        Updates checkpoint after processing.
        '''
        # Build fingerprint for deduplication
        fingerprint = RawMessage.compute_fingerprint(
            sender    =data.get("sender", ""),
            timestamp =data.get("timestamp", ""),
            raw_text  =data.get("rawText", ""),
        )

        # Check for duplicate
        if self._cache.has_seen(fingerprint):
            log.debug(f"Duplicate skipped: {fingerprint[:8]!r}")
            return

        # Mark as seen before processing — prevents double-emit
        # even if something fails downstream
        self._cache.mark_seen(fingerprint)

        self._id_counter += 1
        self._msg_count  += 1

        msg = RawMessage(
            internal_id  =self._id_counter,
            fingerprint  =fingerprint,
            sender       =data.get("sender", "Unknown"),
            raw_text     =data.get("rawText", ""),
            timestamp    =data.get("timestamp", ""),
            direction    =MessageDirection(data.get("direction", "INCOMING")),
            group_name   =self._group_name,
            is_recovered =False,
        )

        log.info(
            f"  📨 New message #{self._msg_count}: "
            f"{msg.direction.value} | "
            f"{msg.sender!r} | "
            f"{msg.preview()!r}"
        )

        # Emit the live message event
        # Day 4's classifier subscribes to "message.received"
        await dispatcher.emit(
            "message.received",
            message=msg,
            group=self._group_name
        )

        # Update checkpoint after every live message
        # This ensures recovery can find where we left off
        self._store.save(Checkpoint(
            fingerprint    =fingerprint,
            timestamp_str  =msg.timestamp,
            saved_at       =Checkpoint.now_str(),
            message_preview=msg.preview(),
        ))

    async def _check_observer_health(self) -> bool:
        '''
        Verify the injected MutationObserver is still active.
        Called periodically to detect if the observer was cleared
        (e.g., WhatsApp Web reloaded parts of the DOM).
        '''
        try:
            result = await self._page.evaluate(_HEALTH_CHECK_JS)
            return result.get("active", False)
        except Exception:
            return False

    async def _cleanup(self) -> None:
        '''Stop the JavaScript MutationObserver cleanly.'''
        try:
            await self._page.evaluate(_STOP_OBSERVER_JS)
            log.debug("MutationObserver disconnected.")
        except Exception as e:
            log.debug(f"Observer cleanup error (non-critical): {e}")

    def stats(self) -> dict:
        '''Return observer statistics for diagnostics.'''
        return {
            "messages_processed": self._msg_count,
            "cache_stats":        self._cache.stats(),
            "poll_interval":      self.POLL_INTERVAL,
            "running":            self._running,
        }