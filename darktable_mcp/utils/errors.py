"""Custom exceptions for darktable MCP server."""


class DarktableMCPError(Exception):
    """Base exception for darktable MCP server."""
    pass


class DarktableNotFoundError(DarktableMCPError):
    """Raised when darktable executable is not found."""
    pass


class DarktableLuaError(DarktableMCPError):
    """Raised when darktable Lua script execution fails."""
    pass


class InvalidRatingError(DarktableMCPError):
    """Raised when an invalid rating is provided."""
    pass


class PhotoNotFoundError(DarktableMCPError):
    """Raised when a specified photo is not found."""
    pass


class ValidationError(DarktableMCPError):
    """Raised when input validation fails."""
    pass


class ExportError(DarktableMCPError):
    """Raised when photo export fails."""
    pass