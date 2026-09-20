"""Darktable MCP Server - Model Context Protocol server for darktable."""

import asyncio
import logging
import sys

from .server import DarktableMCPServer

__version__ = "0.3.0"
__author__ = "w1ne"
__email__ = "14119286+w1ne@users.noreply.github.com"

logger = logging.getLogger(__name__)


def _run_server() -> None:
    """Run the MCP server over stdio (default subcommand / no-args behavior)."""
    import argparse

    parser = argparse.ArgumentParser(description="Darktable MCP Server")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over Streamable HTTP instead of stdio (for remote/proxied access)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "HTTP bind host (--http only). Default 127.0.0.1; bind to LAN IP behind a reverse proxy"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="HTTP bind port (--http only). Default 8787",
    )
    parser.add_argument(
        "--path",
        default="/mcp",
        help="HTTP mount path for the MCP endpoint (--http only). Default /mcp",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    try:
        if args.http:
            import os

            token = os.environ.get("DTMCP_BEARER_TOKEN") or None
            asyncio.run(
                DarktableMCPServer().run_http(
                    host=args.host,
                    port=args.port,
                    path=args.path,
                    bearer_token=token,
                )
            )
        else:
            asyncio.run(DarktableMCPServer().run())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error("Server error: %s", e)
        sys.exit(1)


def main() -> None:
    """Entry point: run MCP server (default) or dispatch to a subcommand."""
    args = sys.argv[1:]
    if args and args[0] == "install-plugin":
        from .cli.install_plugin import install_main

        sys.exit(install_main(args[1:]))
    if args and args[0] == "uninstall-plugin":
        from .cli.install_plugin import uninstall_main

        sys.exit(uninstall_main(args[1:]))
    if args and args[0] == "install-sidecar":
        from .cli.install_sidecar import install_sidecar_main

        sys.exit(install_sidecar_main(args[1:]))
    _run_server()


if __name__ == "__main__":
    main()
