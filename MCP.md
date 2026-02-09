# MCP

Created by Claude

5 MCP tools exposed

send_command(command) — Send a shell command, wait for response, return output
read_terminal(lines=50) — Read recent terminal output
read_logs(lines=50) — Read recent logger output
get_status() — Connection status + device info
wait_for_output(pattern, timeout=30) — Wait for a regex match in output

How to use
Demo mode (no hardware):


claude mcp add rttt -- rttt-mcp --demo
Real hardware:


claude mcp add rttt -- rttt-mcp --device NRF52840_xxAA
Or edit .mcp.json to change --demo to --device YOUR_DEVICE and Claude Code will pick it up automatically.

File fallback — when running the normal TUI (rttt), Claude can always read ~/.rttt_console directly with the built-in Read tool. No changes needed for that.
