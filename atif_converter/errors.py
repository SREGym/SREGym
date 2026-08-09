"""Public exceptions raised by the standalone ATIF converter."""


class AtifConverterError(Exception):
    """Base class for standalone converter failures."""


class UnsupportedAgentError(AtifConverterError):
    """Raised when an explicit agent name is not supported."""


class UnsupportedFormatError(AtifConverterError):
    """Raised when a session file's agent format cannot be detected."""


class ConversionFailedError(AtifConverterError):
    """Raised when a recognized adapter cannot produce an ATIF trajectory."""
