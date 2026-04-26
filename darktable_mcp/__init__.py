"""Darktable MCP Server - Model Context Protocol server for darktable."""

import asyncio
import logging
import sys

from .server import DarktableMCPServer

__version__ = "0.1.0"
__author__ = "w1ne"
__email__ = "14119286+w1ne@users.noreply.github.com"

logger = logging.getLogger(__name__)


def main() -> None:
    """Main entry point for the darktable MCP server (stdio transport)."""
    import argparse

    parser = argparse.ArgumentParser(description="Darktable MCP Server (stdio)")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    try:
        asyncio.run(DarktableMCPServer().run())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error("Server error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
