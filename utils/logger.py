import logging
import json
import sys
from datetime import datetime, timezone

# Lock the root logger so third-party libraries calling logging.basicConfig()
# later (google-auth, httplib2, sqlalchemy etc.) get a no-op. force=True clears
# any handlers already added before this module was imported.
logging.basicConfig(force=True, handlers=[logging.NullHandler()], level=logging.WARNING)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "extra"):
            log_entry.update(record.extra)
        return json.dumps(log_entry)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
