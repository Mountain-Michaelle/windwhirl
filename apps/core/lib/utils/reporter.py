
import logging
import smtplib
from datetime import datetime, date
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)


class Reporter:
    """
    Generates the daily summary report and emails it.

    Has no dependency on any other src/ module except Database
    (passed at call time, not stored as a dependency).
    This makes Reporter trivially testable and reusable from
    any context — CLI, FastAPI route, scheduled task.

    Usage:
        reporter = Reporter()

        # Generate and save the report:
        report_text = reporter.generate_report(db)

        # Optionally email it:
        sent = reporter.send_email(report_text, cfg)
    """

    def __init__(self):
        self._log = logging.getLogger(self.__class__.__name__)

    def generate_report(self, db) -> str:
        """
        Build the daily text report from DB summary data.
        Saves it to reports/daily_report_{date}.txt.
        Returns the report as a string (for printing and emailing).

        Args:
            db: Database instance

        Returns:
            Full report as a plain text string.
        """
        summary   = db.get_daily_summary()
        today_str = date.today().strftime("%Y-%m-%d")

        # ── Build report lines ─────────────────────────────────
        lines = [
            f"WhatsApp Automation Report — {today_str}",
            "=" * 52,
            "",
            "SUMMARY",
            f"  Sent:             {summary.get('SENT', 0)}",
            f"  Failed:           {summary.get('FAILED', 0)}",
            f"  Failed (final):   {summary.get('FAILED_FINAL', 0)}",
            f"  Invalid number:   {summary.get('INVALID_NUMBER', 0)}",
            f"  Pending:          {summary.get('PENDING', 0)}",
            "",
            "TEMPLATE PERFORMANCE (today)",
            f"  Template A sent:  {summary.get('template_A', 0)}",
            f"  Template B sent:  {summary.get('template_B', 0)}",
            "",
        ]

        # ── Failed details ─────────────────────────────────────
        failed = summary.get("failed_details", [])
        if failed:
            lines.append("FAILED — eligible for retry (run --reset-failed)")
            lines.append("-" * 40)
            for item in failed:
                lines.append(
                    f"  {item['name']}  |  "
                    f"+{item['phone']}  |  "
                    f"{item['error']}"
                )
        else:
            lines.append("FAILED: None")

        lines.append("")

        # ── Invalid number details ─────────────────────────────
        invalid = summary.get("invalid_details", [])
        if invalid:
            lines.append("INVALID NUMBERS — not on WhatsApp (will not retry)")
            lines.append("-" * 40)
            for item in invalid:
                lines.append(f"  {item['name']}  |  {item['phone']}")
        else:
            lines.append("INVALID NUMBERS: None")

        lines += [
            "",
            "=" * 52,
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]

        report_text = "\n".join(lines)

        # ── Save to file ───────────────────────────────────────
        Path("reports").mkdir(exist_ok=True)
        report_path = Path("reports") / f"daily_report_{today_str}.txt"
        report_path.write_text(report_text, encoding="utf-8")
        self._log.info(f"Report saved: {report_path}")

        return report_text

    def send_email(self, report_text: str, cfg) -> bool:
        """
        Email the daily report via Gmail SMTP with STARTTLS.

        IMPORTANT: This method NEVER raises an exception.
        Email failure is logged and False is returned.
        The automation must not crash because of a failed email.

        Gmail App Password setup:
          myaccount.google.com → Security → App Passwords
          Generate one for "Mail" and add it to config.py smtp_password.
          Do NOT use your regular Gmail password here.

        Args:
            report_text: The plain text report string.
            cfg:         AppConfig instance (provides SMTP settings).

        Returns:
            True if email sent successfully.
            False if email failed (check logs for details).
        """
        if not cfg.has_email():
            self._log.info(
                "Email not configured (smtp_password is empty) — skipping."
            )
            return False

        try:
            today_str   = date.today().strftime("%Y-%m-%d")
            report_path = Path("reports") / f"daily_report_{today_str}.txt"

            # ── Build the email ────────────────────────────────
            msg            = MIMEMultipart()
            msg["From"]    = cfg.smtp_email
            msg["To"]      = cfg.smtp_to
            msg["Subject"] = f"WhatsApp Automation Report — {today_str}"

            # Plain text body
            msg.attach(MIMEText(report_text, "plain"))

            # Attach the saved report file if it exists
            if report_path.exists():
                with open(report_path, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition",
                        f"attachment; filename={report_path.name}"
                    )
                    msg.attach(part)

            # ── Send via Gmail SMTP ────────────────────────────
            # Port 587 with STARTTLS is the secure Gmail standard.
            # Do not use port 465 (SSL) — STARTTLS is preferred.
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(cfg.smtp_email, cfg.smtp_password)
                smtp.sendmail(
                    cfg.smtp_email,
                    cfg.smtp_to,
                    msg.as_string()
                )

            self._log.info(f"Report emailed successfully to {cfg.smtp_to}")
            return True

        except Exception as e:
            # Log the full error but DO NOT raise
            # Email failure must never crash the automation
            self._log.error(f"Email report failed: {e}", exc_info=True)
            return False