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
  --serial SERIAL_NUMBER     J-Link serial number.
  --device DEVICE            J-Link Device name.
  --speed SPEED              J-Link clock speed in kHz. [default: 2000]
  --reset                    Reset application firmware.
  --address ADDRESS          RTT block address.
  --terminal-buffer INTEGER  RTT Terminal buffer index. [default: 0]
  --logger-buffer INTEGER    RTT Logger buffer index. [default: 1]
  --latency INTEGER          Latency for RTT readout in ms. [default: 50]
  --history-file PATH        Path to history file. [default: ~/.rttt_history]
  --console-file PATH        Path to console file. [default: ~/.rttt_console]
  --mcp / --no-mcp           Enable MCP server. [default: no-mcp]
  --mcp-listen TEXT           MCP server listen address [host:]port. [default: 127.0.0.1:8090]
  --substitutions / --no-substitutions
                             Enable template substitutions in terminal input.
                             [default: substitutions]
  --trust-shells             Trust shell substitutions in config without
                             interactive prompt (for CI/scripts).
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

### Available MCP Tools

| Tool | Description |
|---|---|
| `send_command(command, timeout)` | Send a shell command to the device and wait for response |
| `read_terminal(lines)` | Read recent terminal output (device responses and sent commands) |
| `read_log(lines, after_cursor, pattern)` | Read log output from the device ring buffer, with optional regex filter |
| `status()` | Get session statistics (line counts, buffer usage, cursors) |
| `flash(file_path, addr)` | Flash a firmware file (.hex, .bin, .elf, .srec) to the target device |

## License

This project is licensed under the [MIT License](https://opensource.org/licenses/MIT/) - see the [LICENSE](LICENSE) file for details.

---

Made with &#x2764;&nbsp; by [**HARDWARIO a.s.**](https://www.hardwario.com/) in the heart of Europe.
