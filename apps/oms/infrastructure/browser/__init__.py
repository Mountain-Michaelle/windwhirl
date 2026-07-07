from apps.oms.infrastructure.browser.profile import BrowserProfile
from apps.oms.infrastructure.browser.session_manager import (
    SessionManager,
    SessionState,
)
from apps.oms.infrastructure.browser.health_check import BrowserHealthCheck
from apps.oms.infrastructure.browser.bootstrap import BrowserBootstrap
from apps.oms.infrastructure.browser.raw_message import (
    RawMessage,
    MessageDirection,
)
from apps.oms.infrastructure.browser.message_cache import MessageCache
from apps.oms.infrastructure.browser.checkpoint_store import (
    CheckpointStore,
    Checkpoint,
)
from apps.oms.infrastructure.browser.recovery_manager import RecoveryManager
from apps.oms.infrastructure.browser.dom_observer import DOMObserver

__all__ = [
    "BrowserProfile",
    "SessionManager",
    "SessionState",
    "BrowserHealthCheck",
    "BrowserBootstrap",
    "RawMessage",
    "MessageDirection",
    "MessageCache",
    "CheckpointStore",
    "Checkpoint",
    "RecoveryManager",
    "DOMObserver",
]
"""


# ==============================================================
# ================================================================
#  FILE 7
#  PATH: windwhirl/apps/oms/config/settings.py
# ================================================================
# ADD ObserverSettings dataclass and add to OMSSettings.
# Find the existing settings.py and ADD these blocks.
# Do not replace the whole file — only add what is shown.
# ================================================================
# ADD this dataclass AFTER the RetrySettings dataclass:
# ==============================================================

"""
@dataclass
class ObserverSettings:
    '''
    Settings for the passive message observer and recovery manager.

    poll_interval_seconds: How often Python drains the JS message queue.
                           Lower = more responsive. Higher = less overhead.
                           Default: 2 seconds.

    recovery_max_age_hours: Maximum offline duration to attempt recovery.
                            If the OMS was offline longer than this, recovery
                            is skipped and the live observer starts fresh.
                            Default: 24 hours.

    recovery_max_messages:  Maximum number of missed messages to replay.
                            Prevents unbounded replay after a long outage.
                            Default: 500.

    recovery_max_scrolls:   Maximum scroll operations during recovery search.
                            Prevents infinite scrolling if checkpoint not found.
                            Default: 20.

    message_cache_size:     Maximum fingerprints in the in-memory cache.
                            Default: 1000.

    checkpoint_history_size: How many checkpoint fingerprints to persist.
                             More = higher recovery reliability.
                             Default: 5.
    '''
    poll_interval_seconds:    float = 2.0
    recovery_max_age_hours:   int   = 24
    recovery_max_messages:    int   = 500
    recovery_max_scrolls:     int   = 20
    message_cache_size:       int   = 1000
    checkpoint_history_size:  int   = 5
"""

# ADD this field to the OMSSettings dataclass (after retry field):
"""
    observer: ObserverSettings = field(default_factory=ObserverSettings)
"""

# ADD these to the _load_from_env() env_map dict:
"""
    "OMS_OBSERVER_POLL_INTERVAL":      ("observer.poll_interval_seconds",   float),
    "OMS_OBSERVER_MAX_AGE_HOURS":      ("observer.recovery_max_age_hours",  int),
    "OMS_OBSERVER_MAX_MESSAGES":       ("observer.recovery_max_messages",   int),
    "OMS_OBSERVER_MAX_SCROLLS":        ("observer.recovery_max_scrolls",    int),
    "OMS_OBSERVER_CACHE_SIZE":         ("observer.message_cache_size",      int),
    "OMS_OBSERVER_CHECKPOINT_HISTORY": ("observer.checkpoint_history_size", int),