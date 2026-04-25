# Headless Lua Integration Design Specification

**Date:** 2026-04-25  
**Version:** 1.0  
**Purpose:** Enable headless darktable library operations by enhancing the existing LuaExecutor

## Overview

This design enables the 4 parked MCP tools (`view_photos`, `rate_photos`, `import_batch`, `adjust_exposure`) by leveraging darktable's ability to run as a headless library via Lua's `require("darktable")` functionality. This approach maintains all design principles while providing a much simpler implementation than complex daemon architectures.

## Current Problem

- Existing LuaExecutor uses `darktable --lua script.lua` which always opens GUI
- Violates headless requirement for data queries  
- 4 tools are parked waiting for headless library access
- darktable-cli can export but cannot access user library

## Solution: Enhanced LuaExecutor

Replace complex multi-process architecture with enhanced LuaExecutor that supports both headless and GUI modes using darktable's native capabilities.

## Architecture

### Simple Component Overview
```
Claude Desktop → MCP Server → Enhanced LuaExecutor → darktable (headless OR GUI)
```

**Key insight:** darktable can run headless when loaded as a library via `require("darktable")` in Lua scripts, providing access to most API functionality without GUI.

## Enhanced LuaExecutor Design

### Execution Modes

**Headless Mode (default for data operations):**
```lua
-- Load darktable as library - no GUI
dt = require("darktable")("--library", "/path/to/library.db")
-- Access dt.database, dt.preferences, etc.
-- Perform queries, metadata updates
```

**GUI Mode (when user needs visual feedback):**
```lua
-- Use existing approach - opens darktable GUI
-- darktable --lua script.lua
-- For previews, user interaction, visual confirmation
```

### Implementation Strategy

**Enhanced execute_script method:**
```python
def execute_script(
    self, 
    script_content: str, 
    params: Dict[str, Any] = None, 
    headless: bool = True,
    gui_purpose: Optional[str] = None
) -> str:
    """Execute Lua script in appropriate mode."""
    
    if headless:
        return self._execute_headless(script_content, params)
    else:
        return self._execute_with_gui(script_content, params, gui_purpose)
```

**Auto library detection:**
```python
def _find_darktable_library(self) -> str:
    """Auto-detect user's darktable library database."""
    default_locations = [
        Path.home() / ".config/darktable/library.db",           # Linux
        Path.home() / "Library/Application Support/darktable/library.db",  # macOS  
        Path.home() / "AppData/Local/darktable/library.db"     # Windows
    ]
    
    for path in default_locations:
        if path.exists():
            return str(path)
    
    raise DarktableNotFoundError(
        "Please make sure darktable is installed and you've imported some photos first"
    )
```

## Tool Implementation

### view_photos (Headless)
```lua
dt = require("darktable")(
    "--library", "{library_path}",
    "--configdir", "{config_path}"
)

photos = {}
for _, image in ipairs(dt.database) do
    if matches_filter(image, "{filter}") and 
       (not {rating_min} or image.rating >= {rating_min}) then
        table.insert(photos, {
            id = tostring(image.id),
            filename = image.filename,
            path = image.path,
            rating = image.rating,
            tags = get_image_tags(image)
        })
        
        if #photos >= {limit} then break end
    end
end

print(dt.json.encode(photos))
```

### rate_photos (Headless) 
```lua
dt = require("darktable")(
    "--library", "{library_path}",
    "--configdir", "{config_path}"
)

updated_count = 0
for _, photo_id in ipairs({photo_ids}) do
    image = dt.database[tonumber(photo_id)]
    if image then
        image.rating = {rating}
        updated_count = updated_count + 1
    end
end

print("Updated " .. updated_count .. " photos with " .. {rating} .. " stars")
```

### import_batch (Headless)
```lua  
dt = require("darktable")(
    "--library", "{library_path}",
    "--configdir", "{config_path}"
)

imported_files = dt.database.import("{source_path}", {recursive})
print("Imported " .. #imported_files .. " photos from {source_path}")
```

### adjust_exposure (GUI when preview needed)
```lua
-- For preview: Use GUI mode
-- User can see adjustments before applying
-- Script opens darkroom, applies temporary edits
-- Waits for user confirmation before saving
```

## User Experience

### Installation (Zero Configuration)
```bash
pip install darktable-mcp
# Add to Claude Desktop config - done!
```

### First Use (Automatic Setup)
1. User asks: "Show me my landscape photos"
2. MCP server auto-detects darktable library location
3. Executes headless query, returns results instantly
4. No user configuration required

### Smart Mode Selection
- **Data queries**: Automatic headless execution (fast, invisible)
- **Visual operations**: GUI opens when user needs to see something
- **User control**: "Let me show you the photos I'm selecting" → opens darktable

### Error Handling (User-Friendly)
- ❌ Technical: "DarktableLuaError: library.db access failed"
- ✅ Friendly: "Please make sure darktable is installed and you've imported some photos first"

- ❌ Technical: "subprocess.TimeoutExpired after 30s"  
- ✅ Friendly: "darktable is taking longer than expected. Try opening darktable once to make sure it's working"

## Implementation Plan

### Phase 1: Core Enhancement
1. **Enhance LuaExecutor** with headless mode support
2. **Implement view_photos** and **rate_photos** tools
3. **Auto-detection** of library paths
4. **Update MCP server** tool handlers

### Phase 2: Advanced Features  
1. **import_batch** implementation
2. **adjust_exposure** with GUI preview
3. **apply_preset** functionality
4. **Better error messages** and user guidance

## Technical Requirements

### Dependencies
- **Existing**: Python 3.8+, darktable 4.0+, MCP SDK
- **New**: None! Uses existing darktable Lua capabilities

### Compatibility
- **darktable 4.0+**: Confirmed library mode support
- **All platforms**: Linux, macOS, Windows (where darktable runs)
- **MCP clients**: Claude Desktop, Claude Code, etc.

## Design Principles Compliance

✅ **Rule 1: Use provided darktable APIs only**
- Uses official Lua API via `require("darktable")`
- No direct SQLite access

✅ **Rule 2: Tools that return data must be headless**  
- Data queries use headless library mode
- No GUI spawned for information retrieval

✅ **Rule 3: GUI only when purpose is to show user something**
- GUI opens only for previews, visual confirmation
- Clear user benefit when GUI appears

## Success Criteria

### Technical Success
- All 4 parked tools implemented and functional
- Headless operations complete in <2 seconds
- GUI operations provide clear user value
- Zero configuration required for standard setups

### User Experience Success  
- "Just works" installation experience
- Natural conversation flow with Claude
- Clear visual feedback when helpful
- Intuitive error messages with actionable guidance

### Maintainability Success
- Minimal changes to existing architecture  
- No additional processes to manage
- Leverages standard darktable functionality
- Easy to extend for future tools

## Future Enhancements

### Phase 3: Advanced Editing
- **Filter sets**: Apply noise reduction, filmic RGB, cropping automatically
- **Multiple variations**: Show different processing options to user
- **White balance correction**: Automatic and manual adjustment tools
- **Batch processing**: Apply edits to multiple photos efficiently

### Phase 4: Workflow Integration
- **Export pipelines**: Automated JPEG export with optimal settings
- **Preset management**: Create and apply custom processing presets  
- **Smart selections**: AI-assisted photo filtering and organization
- **Progress feedback**: Real-time status for long-running operations

## Migration Strategy

### From Current State
1. **Enhance existing LuaExecutor** - no breaking changes
2. **Update tool handlers** - change from "not implemented" to actual functionality
3. **Add auto-detection** - library path discovery on first use
4. **Test thoroughly** - ensure existing export_images continues working

### Rollback Plan
- Enhanced LuaExecutor maintains backward compatibility
- Can disable headless mode if issues arise
- Tools can fall back to "not implemented" status if needed

This design provides a robust, user-friendly solution that enables powerful darktable integration while maintaining simplicity and following all design principles.