"""Standalone smoke test - exercise the bridge without Claude Desktop.

Usage: venv/bin/python examples/smoke_test_bridge.py [<image_id>]

Pre-requisite: `darktable-mcp install-plugin` has been run, darktable is
open, and the user's library has at least one image. If you pass an
image_id, that image gets re-rated to 5 stars; without it, only the read
path is exercised.
"""

import sys

from darktable_mcp.bridge.client import (
    Bridge,
    BridgeError,
    BridgePluginNotInstalledError,
    BridgeTimeoutError,
)


def main():
    bridge = Bridge()
    try:
        photos = bridge.call("view_photos", {"limit": 5}, timeout=10.0)
    except BridgePluginNotInstalledError:
        print("FAIL: plugin not installed. Run: darktable-mcp install-plugin")
        sys.exit(1)
    except BridgeTimeoutError:
        print("FAIL: timeout. Is darktable open?")
        sys.exit(1)
    except BridgeError as e:
        print(f"FAIL: bridge error: {e}")
        sys.exit(1)

    print(f"OK: view_photos returned {len(photos)} images")
    for p in photos:
        print(f"  id={p['id']} filename={p['filename']} rating={p['rating']}")

    if len(sys.argv) > 1:
        target_id = sys.argv[1]
        print(f"\nRating image {target_id} as 5 stars...")
        try:
            result = bridge.call(
                "rate_photos",
                {"photo_ids": [target_id], "rating": 5},
                timeout=10.0,
            )
        except BridgeError as e:
            print(f"FAIL: {e}")
            sys.exit(1)
        print(f"OK: rate_photos returned {result}")


if __name__ == "__main__":
    main()
