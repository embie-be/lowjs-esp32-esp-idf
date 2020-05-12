import json
import os
import sys

import click

from idf_py_actions.errors import FatalError
from idf_py_actions.global_options import global_options
from idf_py_actions.tools import ensure_build_directory, run_tool, run_target, get_sdkconfig_value

PYTHON = sys.executable


def action_extensions(base_actions, project_path):
    def _get_default_serial_port():
        """ Return a default serial port. esptool can do this (smarter), but it can create
        inconsistencies where esptool.py uses one port and idf_monitor uses another.

        Same logic as esptool.py search order, reverse sort by name and choose the first port.
        """
        # Import is done here in order to move it after the check_environment() ensured that pyserial has been installed
        import serial.tools.list_ports

        ports = list(reversed(sorted(p.device for p in serial.tools.list_ports.comports())))
        try:
            print("Choosing default port %s (use '-p PORT' option to set a specific serial port)" %
                  ports[0].encode("ascii", "ignore"))
            return ports[0]
        except IndexError:
            raise RuntimeError(
                "No serial ports found. Connect a device, or use '-p PORT' option to set a specific port.")

    def _get_esptool_args(args):
        esptool_path = os.path.join(os.environ["IDF_PATH"], "components/esptool_py/esptool/esptool.py")
        if args.port is None:
            args.port = _get_default_serial_port()
        result = [PYTHON, esptool_path]
        result += ["-p", args.port]
        result += ["-b", str(args.baud)]

        with open(os.path.join(args.build_dir, "flasher_args.json")) as f:
            flasher_args = json.load(f)

        extra_esptool_args = flasher_args["extra_esptool_args"]
        result += ["--before", extra_esptool_args["before"]]
        result += ["--after", extra_esptool_args["after"]]
        result += ["--chip", extra_esptool_args["chip"]]
        if not extra_esptool_args["stub"]:
            result += ["--no-stub"]
        return result

    def _get_commandline_options(ctx):
        """ Return all the command line options up to first action """
        # This approach ignores argument parsing done Click
        result = []

        for arg in sys.argv:
            if arg in ctx.command.commands_with_aliases:
                break

            result.append(arg)

        return result

    def monitor(action, ctx, args, print_filter, monitor_baud, encrypted):
        """
        Run idf_monitor.py to watch build output
        """
        if args.port is None:
            args.port = _get_default_serial_port()
        desc_path = os.path.join(args.build_dir, "project_description.json")
        if not os.path.exists(desc_path):
            ensure_build_directory(args, ctx.info_name)
        with open(desc_path, "r") as f:
            project_desc = json.load(f)

        elf_file = os.path.join(args.build_dir, project_desc["app_elf"])
        if not os.path.exists(elf_file):
            raise FatalError("ELF file '%s' not found. You need to build & flash the project before running 'monitor', "
                             "and the binary on the device must match the one in the build directory exactly. "
                             "Try '%s flash monitor'." % (elf_file, ctx.info_name), ctx)
        idf_monitor = os.path.join(os.environ["IDF_PATH"], "tools/idf_monitor.py")
        monitor_args = [PYTHON, idf_monitor]
        if args.port is not None:
            monitor_args += ["-p", args.port]

        if not monitor_baud:
            if os.getenv("IDF_MONITOR_BAUD"):
                monitor_baud = os.getenv("IDF_MONITOR_BAUD", None)
            elif os.getenv("MONITORBAUD"):
                monitor_baud = os.getenv("MONITORBAUD", None)
            else:
                monitor_baud = project_desc["monitor_baud"]

        monitor_args += ["-b", monitor_baud]
        monitor_args += ["--toolchain-prefix", project_desc["monitor_toolprefix"]]

        coredump_decode = get_sdkconfig_value(project_desc["config_file"], "CONFIG_ESP32_CORE_DUMP_DECODE")
        if coredump_decode is not None:
            monitor_args += ["--decode-coredumps", coredump_decode]

        if print_filter is not None:
            monitor_args += ["--print_filter", print_filter]
        monitor_args += [elf_file]

        if encrypted:
            monitor_args += ['--encrypted']

        idf_py = [PYTHON] + _get_commandline_options(ctx)  # commands to re-run idf.py
        monitor_args += ["-m", " ".join("'%s'" % a for a in idf_py)]

        if "MSYSTEM" in os.environ:
            monitor_args = ["winpty"] + monitor_args
        run_tool("idf_monitor", monitor_args, args.project_dir)

    def flash(action, ctx, args):
        ensure_build_directory(args, ctx.info_name)
        """
        Run esptool to flash the entire project, from an argfile generated by the build system
        """
        if args.port is None:
            args.port = _get_default_serial_port()

        run_target(action, args, {"ESPPORT": args.port,
                                  "ESPBAUD": str(args.baud)})

    def erase_flash(action, ctx, args):
        esptool_args = _get_esptool_args(args)
        esptool_args += ["erase_flash"]
        run_tool("esptool.py", esptool_args, args.build_dir)

    def global_callback(ctx, global_args, tasks):
        encryption = any([task.name in ("encrypted-flash", "encrypted-app-flash") for task in tasks])
        if encryption:
            for task in tasks:
                if task.name == "monitor":
                    task.action_args["encrypted"] = True
                    break

    baud_rate = {
        "names": ["-b", "--baud"],
        "help": "Baud rate for flashing.",
        "scope": "global",
        "envvar": "ESPBAUD",
        "default": 460800,
    }

    port = {
        "names": ["-p", "--port"],
        "help": "Serial port.",
        "scope": "global",
        "envvar": "ESPPORT",
        "default": None,
    }

    serial_actions = {
        "global_action_callbacks": [global_callback],
        "actions": {
            "flash": {
                "callback": flash,
                "help": "Flash the project.",
                "options": global_options + [baud_rate, port],
                "order_dependencies": ["all", "erase_flash"],
            },
            "erase_flash": {
                "callback": erase_flash,
                "help": "Erase entire flash chip.",
                "options": [baud_rate, port],
            },
            "monitor": {
                "callback":
                monitor,
                "help":
                "Display serial output.",
                "options": [
                    port, {
                        "names": ["--print-filter", "--print_filter"],
                        "help":
                        ("Filter monitor output.\n"
                         "Restrictions on what to print can be specified as a series of <tag>:<log_level> items "
                         "where <tag> is the tag string and <log_level> is a character from the set "
                         "{N, E, W, I, D, V, *} referring to a level. "
                         'For example, "tag1:W" matches and prints only the outputs written with '
                         'ESP_LOGW("tag1", ...) or at lower verbosity level, i.e. ESP_LOGE("tag1", ...). '
                         'Not specifying a <log_level> or using "*" defaults to Verbose level.\n'
                         'Please see the IDF Monitor section of the ESP-IDF documentation '
                         'for a more detailed description and further examples.'),
                        "default":
                        None,
                    }, {
                        "names": ["--monitor-baud", "-B"],
                        "type":
                        click.INT,
                        "help": ("Baud rate for monitor.\n"
                                 "If this option is not provided IDF_MONITOR_BAUD and MONITORBAUD "
                                 "environment variables and project_description.json in build directory "
                                 "(generated by CMake from project's sdkconfig) "
                                 "will be checked for default value."),
                    }, {
                        "names": ["--encrypted", "-E"],
                        "is_flag": True,
                        "help": ("Enable encrypted flash targets.\n"
                                 "IDF Monitor will invoke encrypted-flash and encrypted-app-flash targets "
                                 "if this option is set. This option is set by default if IDF Monitor was invoked "
                                 "together with encrypted-flash or encrypted-app-flash target."),
                    }
                ],
                "order_dependencies": [
                    "flash",
                    "encrypted-flash",
                    "partition_table-flash",
                    "bootloader-flash",
                    "app-flash",
                    "encrypted-app-flash",
                ],
            },
            "partition_table-flash": {
                "callback": flash,
                "help": "Flash partition table only.",
                "options": [baud_rate, port],
                "order_dependencies": ["partition_table", "erase_flash"],
            },
            "bootloader-flash": {
                "callback": flash,
                "help": "Flash bootloader only.",
                "options": [baud_rate, port],
                "order_dependencies": ["bootloader", "erase_flash"],
            },
            "app-flash": {
                "callback": flash,
                "help": "Flash the app only.",
                "options": [baud_rate, port],
                "order_dependencies": ["app", "erase_flash"],
            },
            "encrypted-app-flash": {
                "callback": flash,
                "help": "Flash the encrypted app only.",
                "order_dependencies": ["app", "erase_flash"],
            },
            "encrypted-flash": {
                "callback": flash,
                "help": "Flash the encrypted project.",
                "order_dependencies": ["all", "erase_flash"],
            },
        },
    }

    return serial_actions
