# HARDWARIO Real Time Transfer Terminal Console

[![Main](https://github.com/hardwario/py-rttt/actions/workflows/publish.yaml/badge.svg)](https://github.com/hardwario/py-rttt/actions/workflows/publish.yaml)
[![Release](https://img.shields.io/github/release/hardwario/py-rttt.svg)](https://github.com/hardwario/py-rttt/releases)
[![PyPI](https://img.shields.io/pypi/v/rttt.svg)](https://pypi.org/project/rttt/)
[![License](https://img.shields.io/github/license/hardwario/py-rttt.svg)](https://github.com/hardwario/py-rttt/blob/master/LICENSE)
[![Twitter](https://img.shields.io/twitter/follow/hardwario_en.svg?style=social&label=Follow)](https://twitter.com/hardwario_en)

## Overview

**HARDWARIO Real Time Transfer Terminal Console** (`rttt`) is a Python package that provides an interface for real-time data transfer using **SEGGER J-Link RTT (Real-Time Transfer)** technology. It enables efficient data communication between an embedded system and a host computer via **RTT channels**.

This package is particularly useful for **debugging, logging, and real-time data visualization** in embedded applications.

<a href="https://github.com/hardwario/py-rttt/raw/main/image.png" target="_blank">
    <img src="https://github.com/hardwario/py-rttt/raw/main/image.png" alt="alt text" height="200">
</a>

## Features

- **Real-time communication** with embedded devices via RTT.
- **Support for multiple RTT buffers** (console and logger).
- **Adjustable latency** for optimized readout.
- **J-Link support** with configurable serial numbers, device types, and speeds.
- **Command-line interface (CLI)** for quick and easy access.
- **Easy installation via PyPI**.

## Installation

To install the package, use:

```bash
pip install rttt
```

To verify the installation, run:

```bash
rttt --help
```

## Usage

### Basic Command
To start the RTT console:

```bash
rttt --device <DEVICE_NAME>
```

## Available Options

```bash
Usage: rttt [OPTIONS]

  HARDWARIO Real Time Transfer Terminal Console.

Options:
  --version                  Show the version and exit.
  --serial SERIAL_NUMBER     J-Link serial number, or DEMO for the demo probe.
  --demo                     Use the built-in demo probe (same as --serial DEMO).
  --device DEVICE            J-Link Device name.
  --speed SPEED              J-Link clock speed in kHz. [default: 2000]
  --reset                    Reset application firmware.
  --flash-cmd COMMAND        External command used by the flash operation
                             instead of the built-in J-Link programming.
                             Must contain the {file} placeholder.
  --address ADDRESS          RTT block address.
  --terminal-buffer INTEGER  RTT Terminal buffer index. [default: 0]
  --logger-buffer INTEGER    RTT Logger buffer index. [default: 1]
  --latency INTEGER          Latency for RTT readout in ms. [default: 50]
  --history-file PATH        Path to history file. [default: ~/.rttt_history]
  --console-file PATH        Path to console file. [default: ~/.rttt_console]
  --mcp / --no-mcp           Enable MCP server. [default: no-mcp]
  --mcp-listen TEXT          MCP server listen address [host:]port. [default: 127.0.0.1:8090]
  --mcp-token TOKEN          Require "Authorization: Bearer TOKEN" on the MCP
                             server and upload endpoint.
  --substitutions / --no-substitutions
                             Enable template substitutions in terminal input.
                             [default: substitutions]
  --trust-shells             Trust shell substitutions in config without
                             interactive prompt (for CI/scripts).
  --headless                 Run without the interactive console, MCP server
                             only (requires --mcp).
  --help                     Show this message and exit.
```


## Examples

Connect to a device (replace NRF52840_xxAA with your actual device name):

```bash
rttt --device NRF52840_xxAA
```

Use a specific J-Link serial number:

```bash
rttt --device NRF52840_xxAA --serial 123456789
```

### Demo probe (no hardware)

For GUI development without a J-Link, select the built-in demo probe by serial number (same CLI path as a real probe):

```bash
rttt --serial DEMO
# or
rttt --demo
```

The demo emits synthetic terminal/log lines, reports `CONN connected`, and supports reconnect via **F4**, the overlay **Reconnect** button, or by typing `reconnect` in the command field. Type `disconnect` to simulate a drop and exercise the Connection overlay (output pauses until reconnect succeeds).

### Clipboard (SSH / tmux)

Copy uses a hybrid clipboard: **OSC 52** (works over SSH into the local terminal) plus **pyperclip** when a local GUI display is available. Select text with the mouse to auto-copy from **one pane only** (Interactive Terminal *or* Device Log — never both merged). A real drag (mouse move with the button held) **auto-pauses** scroll so streaming lines do not jump the viewport; a plain click does not pause. While paused (manual **F5** or auto during drag / keyboard selection), the status bar shows a highlighted **PAUSED** marker and the F5 hint switches to **F5 Resume** (back to **F5 Pause** when streaming). After a successful select-to-copy, if scroll was running before the selection, streaming **auto-resumes** and the highlight clears (toast `Copied N chars — resumed`). If you had paused with **F5** first, the pause and highlight stay so you can keep reading. Toasts appear **inside** the status bar row (they do not push the panes up). **Ctrl-C** / **Ctrl-Insert** copies the focused pane's selection (or re-toasts the last single-pane copy). **Shift+Arrows / Home / End** and **Ctrl-A** extend or select-all in the focused Log/Terminal pane (Command keeps its normal editing keys); the first extension while streaming auto-pauses like a drag, and **Esc** clears the highlight (and auto-resumes if the pause was automatic). **F6** toggles mouse reporting off so the terminal emulator's own selection works across panes or when the in-app clipboard path is unavailable (Connection overlay still forces mouse on for its buttons). **Right-click** on Log/Terminal copies the selection in that pane (same path as Ctrl-C; no selection → re-toast last copy or `Nothing selected`). **Right-click** on the Command line pastes (local pyperclip when available; otherwise the in-app last copy — no OSC 52 clipboard *read*). Multi-line paste keeps the first line only (Command is single-line). Hold **Shift** while dragging for the terminal's native selection when the emulator supports that bypass. Exit with **Ctrl-Q** if **F10** is captured by the desktop (e.g. XFCE).

Over SSH, pyperclip alone fails because it talks to the remote machine — `rttt` skips it when `SSH_CONNECTION` / `SSH_CLIENT` is set and relies on OSC 52. Prefer a terminal with OSC 52 enabled. With tmux, allow passthrough, for example:

```bash
set -g set-clipboard on
# or allow OSC 52 through: set -g allow-passthrough on   # tmux >= 3.3
```

**Verify OSC 52 over SSH:** from a local machine with clipboard tools, SSH in, run `rttt --demo`, select a known string, then on the *local* host check the clipboard (`pbpaste` / `xclip -o` / paste into an editor). Unit tests cover the SSH path by setting `SSH_CONNECTION` and asserting the OSC 52 escape sequence (including tmux DCS wrapping) without needing a real remote session.

On a local Linux desktop without a working OSC 52 terminal, pyperclip needs a clipboard helper such as **xclip** or **xsel** (install separately, e.g. `sudo apt install xclip`). They are not a hard dependency of `rttt`.

### GUI retest checklist (mouse / clipboard / selection)

Unit tests cover HybridClipboard and selection helpers. A **PTY integration suite** (`tests/test_tui_pty.py`) additionally spawns `rttt --demo` under a pseudo-terminal, renders with `pyte`, injects real xterm SGR mouse sequences, and asserts OSC 52 clipboard payloads — run it with the rest of the suite (`pytest`). Those tests skip automatically where `pty` is unavailable (e.g. Windows CI).

A green pytest run is still **not** enough after mouse / toast / clipboard changes: also retest in a **real terminal** (desktop or SSH). The PTY harness does not cover every emulator quirk (VTE OSC 52 limits, Shift-drag native selection, desktop key grabs like F10).

```bash
rttt --demo
# or: pytest tests/test_tui_pty.py -v
```

Manual checklist:

1. **First drag** (app just started, Command focused): slow-drag in Log *or* Terminal → toast `Copied N chars — resumed`, highlight clears, scroll running again. Must **not** stop at only `Paused for selection`.
2. **Focus switch**: after selecting in Log, the *first* drag in Terminal (and the other way around) must copy on that same gesture — not only move focus.
3. **Selection start**: press on line N, release on line N+k → highlight/copy starts on line N (not near the bottom / old scroll tip). End follows the release point.
4. **Plain click** (no drag): stream stays running; no 1-cell highlight; no pause toast.
5. **After F5 resume**: first drag again still copies and auto-resumes.
6. **Manual F5 then drag**: press F5 → status bar shows **PAUSED** and **F5 Resume**; then drag-copy → stays paused, highlight sticks, toast without `— resumed` (still **PAUSED**).
7. **Streaming mid-drag**: with lines flying, drag still selects and copies (auto-pause on *move*, not press); **PAUSED** appears during the drag, then clears on auto-resume. Toast must include `— resumed` when scroll was on before the drag.
8. **One pane only**: selection in Log must not merge Terminal text (and vice versa).
9. **Ctrl-C / Ctrl-Insert**: after auto-resume (no highlight), re-toasts the last single-pane copy **inside** the status bar (pane heights must not jump).
10. **Toast layout**: while a toast is visible, both panes stay the same height as without a toast (toast replaces hints in the status bar, not a new row).
11. **Local clipboard**: `xclip -o` / `pbpaste` (or paste into an editor) shows the copied text.
12. **SSH / OSC 52** (when available): select in remote `rttt --demo`, paste on the *local* host.
13. **Reconnect**: F4 / overlay button / typed `reconnect`; `disconnect` silences demo output until reconnect.
14. **Right-click copy**: with a sticky highlight (e.g. after F5 + drag), right-click in that pane → `Copied N chars`; streaming/pause state unchanged; focus stays put. With no selection and a prior copy → re-toast; with nothing ever copied → `Nothing selected`.
15. **Right-click paste**: right-click the Command line → focuses Command, inserts clipboard text (trailing newline stripped; first line only if multi-line), toast `Pasted N chars` or `Clipboard empty`. Over SSH, paste uses the last in-app copy (select something first).
16. **F6 mouse toggle**: press F6 → status shows **F6 Mouse OFF**, terminal native drag-select works; F6 again restores in-app mouse. With mouse off, open a disconnect overlay → buttons still clickable (mouse forced on).
17. **Keyboard selection**: focus Log or Terminal (F3/Tab/click), Shift+Right/Down (or Ctrl-A) → **PAUSED**, highlight grows; Ctrl-C → `Copied N chars — resumed` when scroll was on before selecting. After F5 first, keyboard select + Ctrl-C stays **PAUSED** (no `— resumed`). Esc clears highlight; auto-pause resumes, manual F5 stays paused. On the Command line, Ctrl-A / Shift+arrows still edit normally.

**Limits:** some VTE-based terminals (older GNOME Terminal) ignore or cap OSC 52; very large selections are truncated (~60k characters).

## Configuration File

RTTT supports configuration via `.rttt.yaml` files. All existing files are loaded and deep-merged, so you can keep user-wide defaults in your home directory and override specific keys per project. The load order, from lowest to highest priority, is:

1. `~/.config/rttt.yaml` — user defaults
2. `~/.rttt.yaml` — user defaults (alternative location)
3. `./.rttt.yaml` — project-specific overrides
4. `RTTT_*` environment variables
5. Command-line flags

Nested mappings (like `substitutions:`) merge per-key — a project config can add new substitutions without losing the ones defined in your home config, or override specific ones by name.

### Example Configuration:

```yaml
device: NRF9151_XXCA
console_file: "test.log"
substitutions:
  RTC_SET: "rtc set {{UTC_NOW}}"
```

With this configuration, simply running:
```bash
rttt
```

## Input Substitutions

RTTT can expand `{{NAME}}` placeholders in commands you type in the terminal before they are sent to the device. This is handy for things like setting the current time on the device without typing it manually:

```
rtc set {{UTC_NOW}}
```

gets expanded to (example):

```
rtc set 2026/04/20 10:08:00
```

### Built-in Substitutions

| Placeholder | Output | Notes |
|---|---|---|
| `{{UTC_NOW}}` | `2026/04/20 10:08:00` | UTC, default format `%Y/%m/%d %H:%M:%S` |
| `{{UTC_NOW:<fmt>}}` | e.g. `2026-04-20` | Any `strftime` format, e.g. `{{UTC_NOW:%Y-%m-%d}}` |
| `{{LOCAL_NOW}}` | `2026/04/20 12:08:00` | Local time, same default format |
| `{{LOCAL_NOW:<fmt>}}` | e.g. `12:08:00` | Any `strftime` format |
| `{{UNIX_NOW}}` | `1776636480` | Unix timestamp (seconds), format is ignored |

Placeholder names must be upper-case letters, digits, and underscores, and start with a letter or underscore.

### Custom Substitutions

Define your own values in `.rttt.yaml` under the `substitutions` key. Values are strings and may reference other substitutions (built-in or custom):

```yaml
substitutions:
  RTC_SET: "rtc set {{UTC_NOW}}"
  PROJECT: "nrf9151-demo"
  HEADER: "DEV={{PROJECT}} T={{UTC_NOW}}"
```

Then typing `{{RTC_SET}}` in the terminal sends e.g. `rtc set 2026/04/20 10:08:00` to the device.

Custom names take precedence over built-ins, so you can override `UTC_NOW` with a fixed value if needed.

### Multi-Line Substitutions

A substitution value may contain newlines. When the expanded text spans multiple lines, each line is sent as a separate input event to the device. This is useful for grouping a batch of commands under a single placeholder:

```yaml
substitutions:
  CONFIG: |
    app config interval-sample 60
    app config interval-aggreg 300
    app config interval-report 1800
    {{RTC_SET}}
```

Typing `{{CONFIG}}` sends all five commands in order. Nested placeholders (like `{{RTC_SET}}` above) are expanded recursively before the fan-out.

### Shell Substitutions

A substitution value can also be a shell command, evaluated lazily each time the placeholder is expanded:

```yaml
substitutions:
  GIT_SHA:
    shell: "git rev-parse --short HEAD"
  BUILD_HEADER: "build={{GIT_SHA}} at {{UTC_NOW}}"
```

Options for a `shell:` entry:

| Key | Default | Description |
|---|---|---|
| `shell` | (required) | Command passed to `/bin/sh -c`. |
| `cwd` | current working directory | Directory to run the command in. `~` is expanded. |
| `multiline` | `false` | If `true`, the full output is used (newlines in it cause the command to be split and sent as multiple lines to the device). If `false`, only the first line is used. |

Commands have a 5 second timeout. On failure (non-zero exit, timeout, or missing binary), the placeholder is left in the text and a warning is logged.

**Trust prompt.** Because a `.rttt.yaml` in a project you didn't write could run commands on your machine, `rttt` asks for confirmation the first time it sees a new set of shell substitutions. The approval is cached in `~/.hardwario/rttt_allowed_shells` as a hash of the command list plus the absolute config path — you only get asked again if the commands actually change. For non-interactive use (CI, scripts), pass `--trust-shells` to skip the prompt.

### Enabling / Disabling

Substitutions are **enabled by default**, so built-ins like `{{UTC_NOW}}` work out of the box. Use `--no-substitutions` on the command line to disable them for a single session (for example if you actually need to send the literal text `{{UTC_NOW}}` to the device).

### Errors

If a placeholder name is unknown, references itself (cycle), or the format string fails, the placeholder is left in the text as-is and a warning is written to `~/.hardwario/rttt.log`. The command is still sent to the device so you don't lose keystrokes.

## MCP Server (AI Integration)

RTTT includes a built-in [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server that allows AI tools (Claude, Cursor, etc.) to interact with your embedded device via RTT.

MCP server is enabled by default. Start RTTT as usual:

```bash
rttt --device NRF52840_xxAA --mcp
```

### Claude Code Configuration

Add to your `.mcp.json`:

```json
{
    "mcpServers": {
        "rttt": {
            "type": "http",
            "url": "http://127.0.0.1:8090/mcp"
        }
    }
}
```

When the server runs with `--mcp-token`, add the matching header:

```json
{
    "mcpServers": {
        "rttt": {
            "type": "http",
            "url": "http://127.0.0.1:8090/mcp",
            "headers": {
                "Authorization": "Bearer <TOKEN>"
            }
        }
    }
}
```

### Authentication

By default the MCP server has no authentication and binds to `127.0.0.1`,
which is fine for local use. When exposing it to a network (e.g.
`--mcp-listen 0.0.0.0:8090` on a shared debug box), set a token:

```bash
rttt --device NRF9151_XXCA --mcp --mcp-token "$(openssl rand -hex 16)"
```

Every HTTP request — both the `/mcp` endpoint and `/upload` — must then carry
`Authorization: Bearer <TOKEN>`; anything else gets `401 Unauthorized`. The
token can also come from the `RTTT_MCP_TOKEN` environment variable or the
`mcp_token` key in `.rttt.yaml`. Note the transport is plain HTTP, so on an
untrusted network the token (and everything else) is visible on the wire —
use an SSH tunnel or a TLS reverse proxy for anything beyond a lab LAN.

### Available MCP Tools

| Tool | Description |
|---|---|
| `send_command(command, timeout)` | Send a shell command to the device and wait for response |
| `read_terminal(lines)` | Read recent terminal output (device responses and sent commands) |
| `read_log(lines, after_cursor, pattern)` | Read log output from the device ring buffer, with optional regex filter |
| `status()` | Get session statistics (line counts, buffer usage, cursors) |
| `flash(file_path, addr)` | Flash a firmware file (.hex, .bin, .elf, .srec) to the target device |
| `reconnect()` | Re-attach a stuck RTT session without resetting the device |
| `reset(halt)` | Reset the target; RTT re-attaches automatically (unless halting) |
| `halt()` / `go()` | Stop / resume the target CPU |
| `target_status()` | CPU halted flag and core identification |
| `read_memory(address, length, width)` | Hexdump of RAM, peripherals or memory-mapped flash |
| `write_memory(address, data, width)` | Write RAM or peripheral registers |
| `write_flash(address, data)` | Program internal flash bytes (reset+halt, program, reboot) |
| `read_registers()` | Core CPU registers (requires a halted target) |
| `memory_zones()` | Memory zones supported by the J-Link for the target |

The server also exposes a `debug_device` MCP prompt describing typical
debugging workflows and RTT troubleshooting for agent clients.

### External Flash Command

By default the `flash` operation programs the device through the J-Link DLL.
When a different tool works better for your target (e.g. `nrfjprog` for nRF91,
`hardwario` CLI, `west flash`), override it with `--flash-cmd` or the
`flash_cmd` key in `.rttt.yaml`:

```bash
rttt --device NRF9151_XXCA --mcp \
     --flash-cmd 'nrfjprog --family NRF91 --program {file} --sectorerase --verify --reset'
```

```yaml
flash_cmd: "nrfjprog --family NRF91 --program {file} --sectorerase --verify --reset"
```

The command runs through the shell with these placeholders (values are
shell-quoted automatically):

| Placeholder | Value |
|---|---|
| `{file}` | Absolute path of the firmware file (required in the template) |
| `{addr}` | Start address as hex, e.g. `0x0` |
| `{device}` | J-Link device name |
| `{serial}` | J-Link serial number (empty if not set) |

The J-Link connection is released for the duration of the command so the
external tool can claim the debug probe, and RTT re-attaches afterwards. The
tool's output is streamed to the console and log. With an external command
the `.zip` extension is also accepted (nrfjprog modem firmware packages).

Because `flash_cmd` from a config file is an arbitrary shell command, it goes
through the same trust prompt as [shell substitutions](#shell-substitutions)
— you approve it once per config file (or pass `--trust-shells` in CI).

### Headless Mode

For CI boxes, remote debug servers or fully agent-driven sessions, run the
MCP server without the interactive console:

```bash
rttt --device NRF9151_XXCA --mcp --headless
```

### Uploading Firmware from a Remote Client

The `flash` tool resolves paths on the machine `rttt` runs on. When the MCP
client runs elsewhere, upload the firmware first via the HTTP endpoint served
on the same port:

```bash
curl --data-binary @fw.hex 'http://<host>:8090/upload?filename=fw.hex'
# → {"status": "ok", "path": "/tmp/rttt-uploads-8090/fw.hex", "size": 123456}
```

Then pass the returned `path` to the `flash` tool. Allowed extensions are
`.hex`, `.bin`, `.elf` and `.srec`; the body is limited to 64 MiB.

With `--mcp-token` set, include the header:

```bash
curl -H 'Authorization: Bearer <TOKEN>' \
     --data-binary @fw.hex 'http://<host>:8090/upload?filename=fw.hex'
```

> **Note:** without `--mcp-token` the MCP server and the upload endpoint have
> no authentication. The default bind is `127.0.0.1`; set a token before
> exposing them with `--mcp-listen 0.0.0.0:8090` outside a trusted network
> (see [Authentication](#authentication)).

## License

This project is licensed under the [MIT License](https://opensource.org/licenses/MIT/) - see the [LICENSE](LICENSE) file for details.

---

Made with &#x2764;&nbsp; by [**HARDWARIO a.s.**](https://www.hardwario.com/) in the heart of Europe.
