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
  --device DEVICE            J-Link Device name. [required]
  --speed SPEED              J-Link clock speed in kHz. [default: 2000]
  --reset                    Reset application firmware.
  --terminal-buffer INTEGER  RTT Terminal buffer index. [default: 0]
  --logger-buffer INTEGER    RTT Logger buffer index. [default: 1]
  --latency INTEGER          Latency for RTT readout in ms. [default: 50]
  --history-file PATH        Path to history file. [default: ~/.rttt_history]
  --console-file PATH        Path to console file. [default: ~/.rttt_console]
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

RTTT supports configuration via a `.rttt.yaml` file, which can be placed in the **working directory**, the **home directory (`~/.rttt.yaml`)**, or the **user configuration directory (`~/.config/rttt.yaml`)**. The file is loaded from the first available location in this order—**working directory first, then home, then config**. If no configuration file is found, default settings are used.

### Example Configuration:

```yaml
device: NRF9151_XXCA
console_file: "test.log"

With this configuration, simply running:
```bash
rttt
```

## License

This project is licensed under the [MIT License](https://opensource.org/licenses/MIT/) - see the [LICENSE](LICENSE) file for details.

---

Made with &#x2764;&nbsp; by [**HARDWARIO a.s.**](https://www.hardwario.com/) in the heart of Europe.
