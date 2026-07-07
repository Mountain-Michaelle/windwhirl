import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apps.oms.config.settings import get_settings
from apps.oms.infrastructure.browser.bootstrap import BrowserBootstrap
from apps.oms.infrastructure.browser.raw_message import RawMessage
from apps.oms.infrastructure.browser.message_cache import MessageCache
from apps.oms.infrastructure.browser.checkpoint_store import CheckpointStore
from apps.oms.infrastructure.browser.recovery_manager import RecoveryManager
from apps.oms.infrastructure.browser.dom_observer import DOMObserver
from apps.oms.application.classifier import MessageClassifier, MessageClass
from apps.oms.application.parser import OrderParser
from apps.oms.application.validator import OrderValidator
from apps.oms.application.assignment_engine import SingleStaffAssignmentEngine
from apps.oms.application.pipeline import MessagePipeline
from apps.oms.domain.entities import Order, Staff
from apps.oms.shared.logger import get_logger
from apps.oms.events import dispatcher

log = get_logger("oms.runner")


# ── Build pipeline ───────────────────────────────────────────────
# Assembled once at startup — injected with all dependencies

def build_pipeline(settings) -> MessagePipeline:
    staff = Staff(
        number      =settings.whatsapp.staff_number,
        group_name  =settings.whatsapp.group_name,
        display_name=getattr(settings.whatsapp, "staff_display_name", ""),
    )
    return MessagePipeline(
        classifier       =MessageClassifier(),
        parser           =OrderParser(staff_number=staff.number),
        validator        =OrderValidator(),
        assignment_engine=SingleStaffAssignmentEngine(staff),
        staff            =staff,
    )


# ── Event listeners ──────────────────────────────────────────────

@dispatcher.on("browser.connected")
async def on_browser_connected(**kwargs):
    log.info(f"Browser connected — {kwargs.get('state')}")


@dispatcher.on("recovery.completed")
async def on_recovery_completed(**kwargs):
    log.info(
        f"Recovery complete — "
        f"{kwargs.get('recovered_count', 0)} message(s) replayed"
    )


@dispatcher.on("observer.started")
async def on_observer_started(**kwargs):
    log.info(
        f"Observer active — watching: {kwargs.get('group')!r}\n"
        f"Waiting for new messages..."
    )


@dispatcher.on("message.classified")
async def on_classified(**kwargs):
    log.debug(
        f"Classified: {kwargs.get('cls')} "
        f"({kwargs.get('confidence', 0):.2f})"
    )


@dispatcher.on("order.parsed")
async def on_order_parsed(order: Order, **kwargs):
    log.info(
        f"Order parsed: {order.order_id!r} — "
        f"{order.customer_name} — {order.item_summary()}"
    )


@dispatcher.on("order.invalid")
async def on_order_invalid(order: Order, errors: list, **kwargs):
    log.warning(
        f"Order {order.order_id!r} invalid:\n"
        + "\n".join(f"  • {e}" for e in errors)
    )


@dispatcher.on("order.detected")
async def on_order_detected(order: Order, **kwargs):
    log.info(
        f"\n{'=' * 50}\n"
        f"  NEW ORDER DETECTED\n"
        f"  ID:       {order.order_id}\n"
        f"  Customer: {order.customer_name}\n"
        f"  Phone:    {order.customer.phone}\n"
        f"  Items:    {order.item_summary()}\n"
        f"  Address:  {order.customer.address or '(not provided)'}\n"
        f"  Status:   {order.status.value}\n"
        f"{'=' * 50}"
    )
    # Day 5 will save this to the database here


async def main():
    log.info("Windwhirl OMS starting — Day 4...")

    settings = get_settings()

    # Set these if not using environment variables:
    # settings.whatsapp.group_name   = "Your Group Name Here"
    # settings.whatsapp.staff_number = "2348XXXXXXXXX"

    pipeline      = build_pipeline(settings)
    bootstrap     = BrowserBootstrap(settings)
    observer_task = None

    # ── Register message pipeline on "message.received" event ────
    @dispatcher.on("message.received")
    async def handle_message(message: RawMessage, **kwargs):
        await pipeline.process(message)

    # ── Same for recovered messages ───────────────────────────────
    @dispatcher.on("message.recovered")
    async def handle_recovered(message: RawMessage, **kwargs):
        log.info(f"Processing recovered message: {message.preview()!r}")
        await pipeline.process(message)

    try:
        await bootstrap.start()

        if settings.whatsapp.group_name:
            opened = await bootstrap.session_manager.open_target_group(
                settings.whatsapp.group_name
            )
            if not opened:
                log.warning(
                    f"Could not open group: {settings.whatsapp.group_name!r}"
                )
        else:
            log.warning("whatsapp.group_name not configured — set in settings.py")
            await bootstrap.run_forever()
            return

        page = bootstrap.page

        checkpoint_store = CheckpointStore(
            group_name  =settings.whatsapp.group_name,
            data_dir    ="data",
            max_history =settings.observer.checkpoint_history_size,
        )
        cache = MessageCache(max_size=settings.observer.message_cache_size)

        # Recovery
        recovery = RecoveryManager(
            page             =page,
            checkpoint_store =checkpoint_store,
            cache            =cache,
            cfg              =settings,
        )
        await recovery.run()

        # Live observer
        observer = DOMObserver(
            page             =page,
            cache            =cache,
            checkpoint_store =checkpoint_store,
            cfg              =settings,
        )
        observer_task = asyncio.create_task(
            observer.run(),
            name="oms_dom_observer"
        )

        await bootstrap.run_forever()

    except KeyboardInterrupt:
        log.info("Keyboard interrupt — shutting down.")
    except Exception as e:
        log.error(f"OMS runner error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if observer_task and not observer_task.done():
            observer_task.cancel()
            try:
                await observer_task
            except asyncio.CancelledError:
                pass
        # Log final pipeline stats before exiting
        log.info(f"Pipeline stats: {pipeline.stats()}")
        await bootstrap.stop()
        log.info("Windwhirl OMS stopped.")


if __name__ == "__main__":
    asyncio.run(main())