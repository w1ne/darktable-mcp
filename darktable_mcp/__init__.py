"""Darktable MCP Server - Model Context Protocol server for darktable integration."""

import sys
import asyncio
import logging
from typing import Optional

from .server import DarktableMCPServer

__version__ = "0.1.0"
__author__ = "w1ne"
__email__ = "14119286+w1ne@users.noreply.github.com"

logger = logging.getLogger(__name__)


def main() -> None:
    """Main entry point for the darktable MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="Darktable MCP Server")
    parser.add_argument(
        "--port",
        type=int,
        default=3000,
        help="Port to run the server on (default: 3000)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="localhost",
        help="Host to bind to (default: localhost)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Run server
    try:
        server = DarktableMCPServer()
        asyncio.run(server.run(host=args.host, port=args.port))
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()