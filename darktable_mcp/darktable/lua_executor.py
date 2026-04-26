"""Lua script executor for darktable integration."""

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from ..utils.errors import DarktableLuaError, DarktableNotFoundError

logger = logging.getLogger(__name__)


class LuaExecutor:
    """Executes Lua scripts in darktable safely."""

    def __init__(self, darktable_path: Optional[str] = None):
        """Initialize the Lua executor.

        Args:
            darktable_path: Path to darktable executable (auto-detect if None)
        """
        self.darktable_path = darktable_path or self._find_darktable()

    def _find_darktable(self) -> str:
        """Find darktable executable in system PATH.

        Returns:
            str: Path to darktable executable

        Raises:
            DarktableNotFoundError: If darktable is not found
        """
        import shutil

        darktable_path = shutil.which("darktable")
        if not darktable_path:
            raise DarktableNotFoundError(
                "darktable executable not found in PATH. " "Please install darktable."
            )

        return darktable_path

    def execute_script(
        self,
        script_content: str,
        params: Optional[Dict[str, Any]] = None,
        headless: bool = True,
        gui_purpose: Optional[str] = None,
    ) -> str:
        """Execute a Lua script in appropriate mode.

        Args:
            script_content: Lua script code to execute
            params: Parameters to pass to the script
            headless: If True, execute in headless mode using lua interpreter.
                     If False, execute with GUI using darktable --lua.
                     Default: True
            gui_purpose: Purpose description for GUI mode (optional)

        Returns:
            str: Script output

        Raises:
            DarktableLuaError: If script execution fails
        """
        if headless:
            return self._execute_headless(script_content, params)
        else:
            return self._execute_with_gui(script_content, params, gui_purpose)

    def _execute_headless(
        self, script_content: str, params: Optional[Dict[str, Any]] = None
    ) -> str:
        """Execute script in headless mode using lua interpreter.

        Args:
            script_content: Lua script code to execute
            params: Parameters to pass to the script

        Returns:
            str: Script output

        Raises:
            DarktableLuaError: If script execution fails
        """
        from .library_detector import LibraryDetector

        params = params or {}
        detector = LibraryDetector()
        library_path = detector.find_library()

        # Inject library path and parameters into script
        param_lua = self._generate_param_lua(params)
        script_with_setup = (
            f'dt = require("darktable")("--library", "{library_path}")\n'
            f"{param_lua}\n{script_content}"
        )

        try:
            result = subprocess.run(
                ["lua", "-e", script_with_setup],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                error_msg = result.stderr or "Unknown error"
                raise DarktableLuaError(f"Headless Lua script execution failed: {error_msg}")

            return result.stdout.strip()

        except subprocess.TimeoutExpired:
            raise DarktableLuaError("Lua script execution timed out")
        except Exception as e:
            raise DarktableLuaError(f"Failed to execute Lua script: {str(e)}")

    def _execute_with_gui(
        self,
        script_content: str,
        params: Optional[Dict[str, Any]] = None,
        gui_purpose: Optional[str] = None,
    ) -> str:
        """Execute script with GUI (existing implementation).

        Args:
            script_content: Lua script code to execute
            params: Parameters to pass to the script
            gui_purpose: Purpose description for GUI mode (optional)

        Returns:
            str: Script output

        Raises:
            DarktableLuaError: If script execution fails
        """
        params = params or {}

        # Create temporary script file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".lua", delete=False) as f:
            # Add parameter injection
            param_lua = self._generate_param_lua(params)
            full_script = f"{param_lua}\n{script_content}"

            f.write(full_script)
            script_path = f.name

        try:
            # Execute via darktable --lua
            result = subprocess.run(
                [self.darktable_path, "--lua", script_path],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                error_msg = result.stderr or "Unknown error"
                raise DarktableLuaError(f"Lua script execution failed: {error_msg}")

            return result.stdout.strip()

        except subprocess.TimeoutExpired:
            raise DarktableLuaError("Lua script execution timed out")
        except Exception as e:
            raise DarktableLuaError(f"Failed to execute Lua script: {str(e)}")
        finally:
            # Clean up temporary file
            try:
                Path(script_path).unlink()
            except Exception as e:
                logger.warning(f"Failed to cleanup temp script file: {e}")

    def _generate_param_lua(self, params: Dict[str, Any]) -> str:
        """Generate Lua code to inject parameters.

        Args:
            params: Parameters to inject

        Returns:
            str: Lua code for parameter injection
        """
        if not params:
            return ""

        def escape_lua_string(s: str) -> str:
            """Escape special characters in Lua string literals."""
            return (
                s.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r")
            )

        lua_lines = ["-- Injected parameters"]
        for key, value in params.items():
            if isinstance(value, str):
                escaped = escape_lua_string(value)
                lua_lines.append(f'{key} = "{escaped}"')
            elif isinstance(value, (int, float)):
                lua_lines.append(f"{key} = {value}")
            elif isinstance(value, bool):
                lua_lines.append(f"{key} = {str(value).lower()}")
            elif isinstance(value, list):
                # Convert list to Lua table
                items = []
                for item in value:
                    if isinstance(item, str):
                        escaped = escape_lua_string(item)
                        items.append(f'"{escaped}"')
                    else:
                        items.append(str(item))
                lua_lines.append(f'{key} = {{{", ".join(items)}}}')

        return "\n".join(lua_lines)

    def execute_script_file(
        self, script_path: Path, params: Optional[Dict[str, Any]] = None
    ) -> str:
        """Execute a Lua script file.

        Args:
            script_path: Path to Lua script file
            params: Parameters to pass to script

        Returns:
            str: Script output
        """
        script_content = script_path.read_text()
        return self.execute_script(script_content, params)
