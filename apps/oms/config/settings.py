import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.oms.shared.exceptions import ConfigurationException
from app.oms.shared.logger import get_logger

log = get_logger(__name__)

# Module-level singleton — settings loaded once, reused everywhere
_settings_instance: Optional["OMSSettings"] = None


# ==============================================================
# SECTION DATACLASSES
# Each section groups related settings.
# Add new settings here — never scatter them across the codebase.
# ==============================================================

@dataclass
class WhatsAppSettings:
    '''
    Settings for WhatsApp group monitoring.

    group_name:    Exact name of the WhatsApp group to monitor.
                   Must match the group name character for character.

    staff_number:  The WhatsApp number of the staff member whose
                   orders this OMS tracks. Format: 234XXXXXXXXXX
                   (13 digits, no +, no spaces)

    monitor_number: The WhatsApp number logged in on the browser.
                   Usually the business number or your personal number.

    poll_interval: How many seconds to wait between checking for
                   new messages in the group. Default: 15 seconds.
                   Lower = more responsive but more browser activity.
                   Higher = less activity but slower detection.

    message_lookback: How many recent messages to scan when checking
                      for new orders. Default: 20.
    '''
    group_name:       str   = ""
    staff_number:     str   = ""
    monitor_number:   str   = ""
    poll_interval:    int   = 15
    message_lookback: int   = 20


@dataclass
class BrowserSettings:
    '''
    Settings for the Playwright browser used by the OMS.
    Note: This is a SEPARATE browser session from the review automation.
    Each system maintains its own persistent session.

    session_dir:   Where to save the browser login session.
                   Default: ".sessions/oms_session"

    headless:      Run browser without visible window.
                   False during development (you can see what it does).
                   True in production (background operation).

    viewport_w/h:  Browser window dimensions.
                   1280x800 is a common laptop resolution.

    timezone:      Browser timezone. Must match your location.
                   Nigeria: "Africa/Lagos"
    '''
    session_dir: str = ".sessions/oms_session"
    headless:    bool = False
    viewport_w:  int  = 1280
    viewport_h:  int  = 800
    timezone:    str  = "Africa/Lagos"
    locale:      str  = "en-US"


@dataclass
class StorageSettings:
    '''
    Settings for order data storage.
    Starts with SQLite (local file). PostgreSQL-ready via URL swap.

    database_url:  SQLAlchemy connection URL.
                   SQLite:     "sqlite:///data/oms.db"
                   PostgreSQL: "postgresql://user:pass@host/oms"

    orders_file:   Path to save order export files (CSV/Excel).
    '''
    database_url: str = "sqlite:///data/oms.db"
    orders_file:  str = "data/orders_export.xlsx"


@dataclass
class SheetsSettings:
    '''
    Settings for Google Sheets synchronisation.
    Leave spreadsheet_id empty to disable sheets sync.

    spreadsheet_id: The Google Sheets document ID from its URL.
                    Example: "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

    sheet_name:     Name of the tab/sheet to write orders to.

    credentials_file: Path to the Google service account JSON key file.
    '''
    spreadsheet_id:   str = ""
    sheet_name:       str = "Orders"
    credentials_file: str = "config/google_credentials.json"


@dataclass
class RetrySettings:
    '''
    Settings for retry behaviour across all infrastructure operations.

    max_retries:  Maximum number of retry attempts before giving up.
    retry_delay:  Seconds to wait between retries.
                  Uses exponential backoff: delay * (attempt + 1)
    '''
    max_retries: int   = 3
    retry_delay: float = 5.0


@dataclass
class LoggingSettings:
    '''
    Logging configuration for the OMS.

    log_dir:   Directory where log files are written.
    log_level: Minimum level to log. Options: DEBUG, INFO, WARNING, ERROR
    '''
    log_dir:   str = "logs"
    log_level: str = "INFO"


# ==============================================================
# MAIN SETTINGS CLASS
# ==============================================================

@dataclass
class OMSSettings:
    '''
    Root configuration object for the OMS.
    Every future module reads settings from this — never from
    environment variables or hardcoded values directly.

    Usage:
        from app.oms.config.settings import get_settings
        cfg = get_settings()
        cfg.whatsapp.group_name
        cfg.browser.headless
        cfg.storage.database_url

    To configure the OMS, edit the values in this file or
    set environment variables (see _load_from_env below).
    '''

    # Application identity
    app_name:    str = "Windwhirl OMS"
    environment: str = "development"   # "development" | "production"

    # Section settings — each section is its own typed object
    whatsapp: WhatsAppSettings = field(default_factory=WhatsAppSettings)
    browser:  BrowserSettings  = field(default_factory=BrowserSettings)
    storage:  StorageSettings  = field(default_factory=StorageSettings)
    sheets:   SheetsSettings   = field(default_factory=SheetsSettings)
    retry:    RetrySettings    = field(default_factory=RetrySettings)
    logging:  LoggingSettings  = field(default_factory=LoggingSettings)

    def __post_init__(self):
        '''Load from environment variables after dataclass init.'''
        self._load_from_env()
        log.debug(f"OMSSettings loaded: env={self.environment}")

    def _load_from_env(self):
        '''
        Override settings from environment variables if set.
        This allows production deployment without editing source code.

        Environment variable naming:
            OMS_WHATSAPP_GROUP_NAME
            OMS_WHATSAPP_STAFF_NUMBER
            OMS_BROWSER_HEADLESS
            OMS_STORAGE_DATABASE_URL
            etc.
        '''
        env_map = {
            "OMS_APP_NAME":                 ("app_name",                  str),
            "OMS_ENVIRONMENT":              ("environment",               str),
            "OMS_WHATSAPP_GROUP_NAME":      ("whatsapp.group_name",       str),
            "OMS_WHATSAPP_STAFF_NUMBER":    ("whatsapp.staff_number",     str),
            "OMS_WHATSAPP_MONITOR_NUMBER":  ("whatsapp.monitor_number",   str),
            "OMS_WHATSAPP_POLL_INTERVAL":   ("whatsapp.poll_interval",    int),
            "OMS_BROWSER_HEADLESS":         ("browser.headless",          bool),
            "OMS_BROWSER_SESSION_DIR":      ("browser.session_dir",       str),
            "OMS_BROWSER_TIMEZONE":         ("browser.timezone",          str),
            "OMS_STORAGE_DATABASE_URL":     ("storage.database_url",      str),
            "OMS_SHEETS_SPREADSHEET_ID":    ("sheets.spreadsheet_id",     str),
            "OMS_RETRY_MAX_RETRIES":        ("retry.max_retries",         int),
            "OMS_RETRY_DELAY":              ("retry.retry_delay",         float),
            "OMS_LOGGING_LOG_DIR":          ("logging.log_dir",           str),
            "OMS_LOGGING_LOG_LEVEL":        ("logging.log_level",         str),
        }

        for env_key, (attr_path, cast) in env_map.items():
            value = os.environ.get(env_key)
            if value is not None:
                parts = attr_path.split(".")
                if len(parts) == 1:
                    setattr(self, parts[0], cast(value))
                elif len(parts) == 2:
                    section = getattr(self, parts[0])
                    # Handle bool from string
                    if cast == bool:
                        value = value.lower() in ("true", "1", "yes")
                    else:
                        value = cast(value)
                    setattr(section, parts[1], value)

    def validate(self):
        '''
        Validate that required settings are present.
        Call this at application startup — fail fast if misconfigured.

        Raises:
            ConfigurationException if required settings are missing.
        '''
        errors = []

        if not self.whatsapp.group_name:
            errors.append(
                "whatsapp.group_name is required. "
                "Set OMS_WHATSAPP_GROUP_NAME or edit settings.py."
            )

        if not self.whatsapp.staff_number:
            errors.append(
                "whatsapp.staff_number is required. "
                "Set OMS_WHATSAPP_STAFF_NUMBER or edit settings.py."
            )

        if self.whatsapp.staff_number:
            num = self.whatsapp.staff_number.replace("+", "").replace(" ", "")
            if not num.startswith("234") or len(num) != 13:
                errors.append(
                    f"whatsapp.staff_number must be 13 digits starting with 234. "
                    f"Got: {self.whatsapp.staff_number!r}"
                )

        if errors:
            raise ConfigurationException(
                f"OMS configuration has {len(errors)} error(s):\n"
                + "\n".join(f"  • {e}" for e in errors)
            )

        log.info(
            f"Configuration validated. "
            f"Group: {self.whatsapp.group_name!r}, "
            f"Staff: +{self.whatsapp.staff_number}"
        )

    def is_production(self) -> bool:
        '''True if running in production environment.'''
        return self.environment.lower() == "production"

    def has_sheets(self) -> bool:
        '''True if Google Sheets sync is configured.'''
        return bool(self.sheets.spreadsheet_id)

    def __repr__(self):
        return (
            f"OMSSettings("
            f"env={self.environment!r}, "
            f"group={self.whatsapp.group_name!r}, "
            f"headless={self.browser.headless}"
            f")"
        )


def get_settings() -> OMSSettings:
    '''
    Get the singleton OMSSettings instance.
    Creates it on first call, returns cached instance on subsequent calls.

    Usage:
        from app.oms.config.settings import get_settings
        cfg = get_settings()
    '''
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = OMSSettings()
    return _settings_instance


def reset_settings():
    '''
    Clear the cached settings instance.
    Used in tests to reset state between test cases.
    '''
    global _settings_instance
    _settings_instance = None