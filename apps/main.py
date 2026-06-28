# ==============================================================
# main.py
# PATH: windwhirl/apps/main.py
# ==============================================================
# WHAT CHANGED FROM YOUR CURRENT VERSION:
#   Added a PATH SETUP block at the very top (lines 1-15).
#   This is the ONLY change. Everything else is identical.
#
# WHY IT FAILED:
#   You run:  python main.py  from inside windwhirl/apps/
#   Your imports say: from apps.config import AppConfig
#   Python looks for a folder called "apps" inside the current folder.
#   Current folder IS "apps" — so Python can't find "apps" inside itself.
#
#   The fix: add windwhirl/ (the PARENT of apps/) to sys.path.
#   Then Python can find apps/ as a package inside windwhirl/.
#
# YOUR FOLDER STRUCTURE (what we assume based on your error):
#   windwhirl/
#   ├── apps/
#   │   ├── main.py                        ← you run this
#   │   ├── config.py                      ← from apps.config
#   │   └── core/
#   │       ├── db/
#   │       │   └── database.py            ← from apps.core.db.database
#   │       └── lib/
#   │           ├── scheduler/
#   │           │   └── scheduler.py       ← from apps.core.lib.scheduler.scheduler
#   │           └── utils/
#   │               ├── data_reader.py     ← from apps.core.lib.utils.data_reader
#   │               ├── message_builder.py ← from apps.core.lib.utils.message_builder
#   │               ├── playwright_sender.py
#   │               ├── reporter.py
#   │               └── whatsapp_sender.py
# ==============================================================

# ==============================================================
# PATH SETUP — MUST BE FIRST, BEFORE EVERYTHING ELSE
# ==============================================================
# This adds windwhirl/ (the parent of apps/) to Python's module
# search path so that "from apps.config import AppConfig" works
# correctly when you run "python main.py" from inside apps/.
#
# Path(__file__).resolve()        → full path to main.py
#                                   e.g. C:\...\windwhirl\apps\main.py
# .parent                         → apps/ folder
# .parent                         → windwhirl/ folder  ← we add THIS
#
# sys.path.insert(0, ...)         → insert at position 0 so it is
#                                   checked FIRST before anything else
# ==============================================================
import sys
from pathlib import Path

# Add windwhirl/ (parent of apps/) to Python's search path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Confirm path is set (visible in logs/automation.log at DEBUG level)
# Remove this print once everything is working
print(f"[PATH] Project root added: {_PROJECT_ROOT}")
print(f"[PATH] sys.path[0]: {sys.path[0]}")

# ==============================================================
# END PATH SETUP
# ==============================================================


import argparse
import asyncio
import logging
import signal


# ==============================================================
# LOGGING SETUP
# ==============================================================
# Must run before any src/apps imports because those modules
# call logging.getLogger() at module load time.
# If logging is set up after them, their log messages are lost.
# ==============================================================

def _setup_logging():
    """Configure console + rotating file logging for the application."""
    import logging.handlers

    Path("logs").mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console: INFO and above — clean output the user reads in real time
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S"
    ))

    # File: DEBUG and above — full trace for troubleshooting
    file_h = logging.handlers.RotatingFileHandler(
        "logs/automation.log",
        maxBytes=5 * 1024 * 1024,   # 5 MB per file
        backupCount=3,
        encoding="utf-8"
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    root.addHandler(console)
    root.addHandler(file_h)

_setup_logging()


# ==============================================================
# APPLICATION IMPORTS
# ==============================================================
# These now work because windwhirl/ is in sys.path,
# so Python can find apps/ as a package inside windwhirl/.
# ==============================================================

from apps.config import AppConfig
from apps.core.db.database import Database
from apps.core.lib.utils.data_reader import DataReader
from apps.core.lib.utils.message_builder import MessageBuilder
from apps.core.lib.utils.playwright_sender import PlaywrightSender
from apps.core.lib.scheduler.scheduler import Scheduler
from apps.core.lib.utils.reporter import Reporter

log = logging.getLogger("main")


# ==============================================================
# ALSO CHECK: Every __init__.py must exist
# ==============================================================
# Python needs an __init__.py in EVERY folder that is part of
# an import path. Check these files all exist (can be empty):
#
#   windwhirl/apps/__init__.py
#   windwhirl/apps/core/__init__.py
#   windwhirl/apps/core/db/__init__.py
#   windwhirl/apps/core/lib/__init__.py
#   windwhirl/apps/core/lib/scheduler/__init__.py
#   windwhirl/apps/core/lib/utils/__init__.py
#
# Quick check — run this in PowerShell:
#   Get-ChildItem -Recurse -Filter "__init__.py" | Select FullName
#
# If any are missing, create them as empty files:
#   New-Item apps\core\__init__.py -ItemType File
#   New-Item apps\core\db\__init__.py -ItemType File
#   New-Item apps\core\lib\__init__.py -ItemType File
#   New-Item apps\core\lib\scheduler\__init__.py -ItemType File
#   New-Item apps\core\lib\utils\__init__.py -ItemType File
# ==============================================================


# ==============================================================
# FOLDER SETUP
# ==============================================================

def _create_directories():
    """
    Create all required runtime directories if they don't exist.
    Called before every command.
    """
    for folder in ["data", ".sessions", "logs", "reports", "screenshots"]:
        Path(folder).mkdir(parents=True, exist_ok=True)


# ==============================================================
# CLI COMMAND FUNCTIONS
# ==============================================================

async def cmd_setup(cfg: AppConfig):
    """
    --setup: First-time initialization.
    Creates folders, imports Excel, opens browser for QR scan.
    """
    print("\n" + "=" * 55)
    print("  WHATSAPP AUTOMATION — SETUP")
    print("=" * 55)

    _create_directories()
    print("✅ Folders ready.")

    db = Database(cfg.database_url)
    db.init()
    print("✅ Database ready.")

    excel_path = cfg.excel_path()
    if not excel_path.exists():
        print(f"\n❌ Excel file not found: {excel_path}")
        print(f"   Drop your file into the data/ folder as: {cfg.excel_filename}")
        print("   Then run --setup again.")
        return

    reader    = DataReader(cfg.country_code)
    customers = reader.read_and_filter(excel_path, cfg.target_product)

    if not customers:
        print(
            f"\n⚠️  No customers matched product keyword: '{cfg.target_product}'\n"
            "   Check your Excel file and target_product in config.py."
        )
        return

    for customer in customers:
        db.upsert_customer(customer)

    stats = db.get_stats()
    print(
        f"✅ Excel imported: {stats['total']} customers, "
        f"{stats['pending']} pending, "
        f"{stats['invalid_phones']} invalid phones."
    )

    print("\n" + "=" * 55)
    print("  Opening browser for WhatsApp login...")
    print("=" * 55)

    sender = PlaywrightSender(cfg)
    try:
        connected = await sender.connect()
        if connected:
            print("\n✅ WhatsApp connected. Login session saved.")
            print("   You won't need to scan QR again on future runs.")
            print(
                "\n✅ Setup complete. Run next:\n"
                "   python main.py --preview\n"
                "   python main.py --dry-run"
            )
    finally:
        await sender.disconnect()


def cmd_preview(cfg: AppConfig):
    """
    --preview: Show customer stats and today's schedule.
    No browser. No sending.
    """
    db = Database(cfg.database_url)
    db.init()

    stats   = db.get_stats()
    samples = db.get_sample_customers(5)

    print("\n" + "=" * 50)
    print("  WHATSAPP AUTOMATION — PREVIEW")
    print("=" * 50)
    print(f"  Target product:    {cfg.target_product}")
    print(f"  Daily limit:       {cfg.daily_limit}")
    print(f"  Send order:        {cfg.send_order}")
    print(f"  Email reports:     {'yes' if cfg.has_email() else 'not configured'}")
    print()
    print(f"  Total customers:   {stats['total']}")
    print(f"  Pending (unsent):  {stats['pending']}")
    print(f"  Already sent:      {stats['sent']}")
    print(f"  Invalid numbers:   {stats['invalid']}")
    print(f"  Bad phones:        {stats['invalid_phones']}")

    if samples:
        print(f"\n  Sample names:      {', '.join(samples)}")

    print("\n  TODAY'S SCHEDULE")
    print("  " + "-" * 35)
    for i, job in enumerate(cfg.session_jobs()):
        print(
            f"  Session {i + 1}:  "
            f"{job['hour']:02d}:{job['minute']:02d}  →  "
            f"{job['count']} messages"
        )
    print(f"\n  Total today:       {cfg.total_daily_count()} messages\n")


def cmd_dry_run(cfg: AppConfig):
    """
    --dry-run: Preview messages for first 3 customers.
    No browser. No sending.
    """
    cmd_preview(cfg)

    db = Database(cfg.database_url)
    db.init()

    pending = db.get_pending(limit=3, order=cfg.send_order)

    if not pending:
        print("  No pending customers to preview messages for.")
        return

    builder = MessageBuilder(cfg)

    print("=" * 50)
    print("  DRY RUN — SAMPLE MESSAGES (nothing is being sent)")
    print("=" * 50)

    for customer in pending:
        print(f"\n  {'─' * 45}")
        print(
            f"  Customer: {customer['first_name']}  "
            f"|  Order: {customer['order_id']}"
        )
        print(f"  {'─' * 45}")

        both = builder.preview(customer)

        print("\n  [TEMPLATE A — Results Check-In]\n")
        for line in both["A"].split("\n"):
            print(f"  {line}")

        print("\n  [TEMPLATE B — Honest Feedback]\n")
        for line in both["B"].split("\n"):
            print(f"  {line}")

    print("\n" + "=" * 50)
    print("  DRY RUN COMPLETE — Zero messages were sent.")
    print("=" * 50 + "\n")


async def cmd_run(cfg: AppConfig, run_now: bool = False, count: int = 3):
    """
    --run:                  Full 6-session scheduled day.
    --run --now:            Immediate session (default 3 messages).
    --run --now --count N:  Immediate session with N messages.
    """
    if cfg.daily_limit > 200:
        print(
            f"\n⚠️  WARNING: daily_limit={cfg.daily_limit} exceeds "
            f"recommended maximum of 200.\n"
        )

    db       = Database(cfg.database_url)
    db.init()
    builder  = MessageBuilder(cfg)
    reporter = Reporter()
    sender   = PlaywrightSender(cfg)

    try:
        log.info("Connecting to WhatsApp Web...")
        connected = await sender.connect()
        if not connected:
            print("❌ Could not connect to WhatsApp. Run --setup first.")
            return

        scheduler = Scheduler(cfg, db, sender, builder, reporter)

        if run_now:
            await scheduler.run_now(count)
        else:
            scheduler.start()

    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user (Ctrl+C).")
    except Exception as e:
        log.error(f"Run failed: {e}", exc_info=True)
        print(f"\n❌ Error: {e}")
        print("   Full details in: logs/automation.log")
    finally:
        log.info("Disconnecting browser...")
        await sender.disconnect()


def cmd_report(cfg: AppConfig):
    """
    --report: Generate today's report and optionally email it.
    """
    db       = Database(cfg.database_url)
    db.init()
    reporter = Reporter()

    report_text = reporter.generate_report(db)
    print("\n" + report_text)

    if cfg.has_email():
        sent = reporter.send_email(report_text, cfg)
        if sent:
            print(f"\n✅ Report emailed to {cfg.smtp_to}")
        else:
            print("\n❌ Email failed — check logs/automation.log")
    else:
        print(
            "\n(Email not configured — "
            "set smtp_email + smtp_password in config.py to enable)"
        )


def cmd_reset_failed(cfg: AppConfig):
    """
    --reset-failed: Reset FAILED → PENDING for retry tomorrow.
    """
    db    = Database(cfg.database_url)
    db.init()
    count = db.reset_failed()
    print(f"\n✅ {count} message(s) reset to PENDING for next --run.")
    if count == 0:
        print("   (No FAILED entries found — nothing to reset)")


# ==============================================================
# MAIN ENTRY POINT
# ==============================================================

def main():
    """Parse CLI args, run startup checks, dispatch to commands."""

    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="WhatsApp Review Automation System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --setup                    First-time setup
  python main.py --preview                  Check customer count + schedule
  python main.py --dry-run                  Preview messages before sending
  python main.py --run --now --count 3      Send 3 test messages now
  python main.py --run                      Start full scheduled day
  python main.py --report                   Generate and email today's report
  python main.py --reset-failed             Reset failed messages for retry
        """
    )

    parser.add_argument("--setup",        action="store_true")
    parser.add_argument("--preview",      action="store_true")
    parser.add_argument("--dry-run",      action="store_true", dest="dry_run")
    parser.add_argument("--run",          action="store_true")
    parser.add_argument("--now",          action="store_true")
    parser.add_argument("--count",        type=int, default=3)
    parser.add_argument("--report",       action="store_true")
    parser.add_argument("--reset-failed", action="store_true", dest="reset_failed")

    args = parser.parse_args()

    if not any([
        args.setup, args.preview, args.dry_run,
        args.run, args.report, args.reset_failed
    ]):
        parser.print_help()
        sys.exit(0)

    _create_directories()

    try:
        cfg = AppConfig()
    except ValueError as e:
        print(f"\n❌ Configuration error:\n   {e}")
        print("   Edit apps/config.py → CONFIG dict to fix this.")
        sys.exit(1)

    def _handle_signal(signum, frame):
        log.info(f"Signal {signum} received — shutting down.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        if args.setup:
            asyncio.run(cmd_setup(cfg))
        elif args.preview:
            cmd_preview(cfg)
        elif args.dry_run:
            cmd_dry_run(cfg)
        elif args.run:
            asyncio.run(cmd_run(cfg, run_now=args.now, count=args.count))
        elif args.report:
            cmd_report(cfg)
        elif args.reset_failed:
            cmd_reset_failed(cfg)

    except KeyboardInterrupt:
        log.info("Interrupted by user.")
        sys.exit(0)
    except Exception as e:
        log.error(f"Unexpected error: {e}", exc_info=True)
        print(f"\n❌ Unexpected error: {e}")
        print("   Full details in: logs/automation.log")
        sys.exit(1)


if __name__ == "__main__":
    main()