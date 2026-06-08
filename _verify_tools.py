import asyncio
import json
import sys
sys.path.insert(0, r"d:\BaiduNetdiskDownload\TempProject\pychat\pycat-dev")

from core.tools.manager import ToolManager
from core.tools.catalog import ToolAvailabilityContext, ToolSelectionPolicy

async def main():
    tm = ToolManager()
    schemas = await tm.get_all_tools(
        tool_selection=ToolSelectionPolicy.all(),
        availability_context=ToolAvailabilityContext(search_available=False, mcp_available=False),
    )

    for target in ['subagent__read_analyze', 'subagent__search', 'subagent__custom', 'capability__summarize_text', 'capability__translate']:
        schema = next((s for s in schemas if s['function']['name'] == target), None)
        if schema:
            print(f'=== {target} ===')
            fn = schema['function']
            print(f"description: {fn['description']}")
            print(f"required: {fn['parameters'].get('required', [])}")
            props = fn['parameters'].get('properties', {})
            for k, v in props.items():
                print(f"  {k}: {v['description'][:80]}...")
            print()

asyncio.run(main())
