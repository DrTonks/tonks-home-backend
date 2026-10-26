"""Application-owned stderr sinks for operational events."""

import logging


COMMUNITY_HANDLER_NAME = "sleepy.community.stderr"


def configure_community_logging() -> None:
    """Give community one INFO stderr sink, independent of root configuration.

    Called during server initialization, before requests/workers are started.
    This application owns the exact community logger's handlers; other loggers
    and their handlers are left alone. A stable handler name survives reloads.
    """
    _configure_logger("community", COMMUNITY_HANDLER_NAME)


def configure_pet_search_logging() -> None:
    """Search events contain outcome/elapsed time only, no queries or secrets."""
    _configure_logger("pet_ai.search", "sleepy.pet_search.stderr")


def _configure_logger(name: str, handler_name: str) -> None:
    logger = logging.getLogger(name)
    handler = next(
        (item for item in logger.handlers if item.get_name() == handler_name),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler()  # stderr, captured by the process manager
        handler.set_name(handler_name)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    # Detach stale/local handlers without closing resources another logger may use.
    for existing in list(logger.handlers):
        if existing is not handler:
            logger.removeHandler(existing)
    logger.addHandler(handler)  # addHandler is idempotent for the same instance
    logger.setLevel(logging.INFO)
    logger.disabled = False
    logger.propagate = False  # avoid both root filtering and duplicate root output
