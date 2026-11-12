import logging
import os


def configure_logging(component: str) -> None:
    """Idempotent process-wide logging setup tagged with the component name."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format=f"%(asctime)s %(levelname)s [{component}] %(name)s: %(message)s",
    )
