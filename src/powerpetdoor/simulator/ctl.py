# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Control client for the Power Pet Door simulator.

This module provides a command-line tool to send commands to a running
simulator's control port.
"""

import argparse
import socket
import sys
from typing import Optional


def send_command(
    host: str,
    port: int,
    command: str,
    timeout: float = 5.0,
) -> tuple[bool, str]:
    """Send a command to the simulator control port.

    Args:
        host: Simulator host address
        port: Control port number
        command: Command to send
        timeout: Socket timeout in seconds

    Returns:
        Tuple of (success, response_message)
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.sendall(f"{command}\n".encode())

            # Read response
            response = b""
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                    # Check if we got a complete response
                    if response.endswith(b"\n"):
                        break
                except socket.timeout:
                    break

            response_str = response.decode().strip()
            success = response_str.startswith("OK:")
            return success, response_str

    except ConnectionRefusedError:
        return False, f"Connection refused to {host}:{port}"
    except socket.timeout:
        return False, f"Connection timed out to {host}:{port}"
    except Exception as e:
        return False, f"Error: {e}"


def interactive_mode(host: str, port: int):
    """Run in interactive mode, sending commands from stdin."""
    print(f"Connected to simulator at {host}:{port}")
    print("Type 'help' for commands, 'exit' to quit")
    print()

    prompt = f"{host}:{port}> "
    try:
        while True:
            try:
                line = input(prompt).strip()
            except EOFError:
                break

            if not line:
                continue

            if line.lower() in ("exit", "quit", "q"):
                break

            success, response = send_command(host, port, line)
            print(response)

            # If we sent a stop command and it succeeded, exit
            if line.lower() in ("stop", "exit", "quit") and success:
                break

    except KeyboardInterrupt:
        print("\nExiting.")


def main():
    """CLI entry point for simulator control."""
    parser = argparse.ArgumentParser(
        description="Control a running Power Pet Door simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s status                  # Get simulator status
  %(prog)s inside                  # Trigger inside sensor
  %(prog)s run basic_cycle         # Run a script
  %(prog)s -i                      # Interactive mode
  %(prog)s exit                    # Shutdown simulator

Commands:
  Door: inside, outside, close, hold
  Buttons: power, auto, inside_enable, outside_enable
  Simulation: obstruction, pet
  Settings: safety, lockout, autoretract, holdtime <sec>, battery [pct]
  Scripts: list, run <script>
  Info: status, help
  Control: exit, stop
"""
    )
    parser.add_argument(
        "--host", "-H",
        default="127.0.0.1",
        help="Simulator host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=3001,
        help="Control port (default: 3001)"
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Run in interactive mode"
    w)
    parser.add_argument(
        "--timeout", "-t",
        type=float,
        default=5.0,
        help="Command timeout in seconds (default: 5)"
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="Command to send (or use -i for interactive mode)"
    )

    args = parser.parse_args()

    if args.interactive:
        interactive_mode(args.host, args.port)
    elif args.command:
        command = " ".join(args.command)
        success, response = send_command(
            args.host, args.port, command, args.timeout
        )
        print(response)
        sys.exit(0 if success else 1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
