#!/usr/bin/env python3
import asyncio
import websockets
import sys
import os
import signal
import json
import pty
import termios
import tty
import fcntl
import struct
import base64
import select
import time
import argparse
import datetime
import subprocess

# Global state
master_fd = None
slave_fd = None
child_pid = None
original_term_settings = None
terminal_size = (24, 80)  # Default (rows, cols)
should_exit = False
shell_path = os.environ.get('SHELL', '/bin/bash')

def log(message):
    """Log a message to stdout with timestamp."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] {message}", flush=True)

def get_terminal_size():
    """Get current terminal size."""
    if sys.stdout.isatty():
        s = struct.pack('HHHH', 0, 0, 0, 0)
        x = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, s)
        rows, cols, _, _ = struct.unpack('HHHH', x)
        return rows, cols
    else:
        return 24, 80

def setup_pty():
    """Set up a new pseudoterminal."""
    global master_fd, slave_fd, terminal_size

    # Create a new pty
    master_fd, slave_fd = pty.openpty()

    # Set the terminal size
    rows, cols = terminal_size
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    # Make the master fd non-blocking
    os.set_blocking(master_fd, False)

    return master_fd, slave_fd

def start_shell():
    """Start a shell process in the pty."""
    global master_fd, slave_fd, child_pid

    # Create subprocess using the slave fd
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"

    # Save the slave name
    slave_name = os.ttyname(slave_fd)

    # Close the slave in this process
    os.close(slave_fd)

    # Create subprocess in a new process group
    proc = subprocess.Popen(
        shell_path,
        start_new_session=True,
        stdin=open(slave_name, 'r'),
        stdout=open(slave_name, 'w'),
        stderr=open(slave_name, 'w'),
        env=env,
        shell=False
    )

    child_pid = proc.pid
    log(f"Started shell process with PID {child_pid}")
    return child_pid

def resize_pty(rows, cols):
    """Resize the PTY."""
    global master_fd, terminal_size

    if master_fd:
        terminal_size = (rows, cols)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        log(f"Resized terminal to {cols}x{rows}")

def cleanup():
    """Clean up resources."""
    global master_fd, child_pid, should_exit

    # Set exit flag
    should_exit = True

    # Kill the child process if it's still running
    if child_pid:
        try:
            # Try to gracefully terminate the process
            os.kill(child_pid, signal.SIGTERM)
            log(f"Terminated shell process {child_pid}")

            # Give it a moment to terminate
            time.sleep(0.1)

            # Force kill if still alive
            try:
                os.kill(child_pid, 0)  # This will raise an error if process is gone
                os.kill(child_pid, signal.SIGKILL)
                log(f"Force killed shell process {child_pid}")
            except OSError:
                pass  # Process is already gone

        except OSError:
            pass

    # Close the master PTY
    if master_fd:
        try:
            os.close(master_fd)
            log("Closed terminal connection")
        except OSError:
            pass

    # Reset terminal explicitly if in a terminal
    if sys.stdout.isatty():
        # This sequence resets terminal to normal mode
        sys.stdout.write("\033c\033[!p\033[?3;4l\033[4l\033>")
        sys.stdout.flush()

    log("Shell session terminated")
    print("\r\nSession terminated.")

async def handle_shell_io(websocket):
    """Handle I/O between the PTY and the WebSocket."""
    global master_fd, should_exit

    while not should_exit:
        # Check if there's data to read from the PTY
        try:
            r, _, _ = select.select([master_fd], [], [], 0.1)

            if r:
                try:
                    data = os.read(master_fd, 1024)
                    if data:
                        # Write to stdout for local display
                        sys.stdout.buffer.write(data)
                        sys.stdout.flush()

                        # Send to the WebSocket
                        await websocket.send(json.dumps({
                            "type": "output",
                            "data": base64.b64encode(data).decode('ascii')
                        }))
                except (OSError, IOError) as e:
                    if e.errno != 5:  # Ignore "Input/output error" which can happen when process exits
                        log(f"Error reading from PTY: {e}")
                    break
        except (select.error, OSError) as e:
            log(f"Error in select: {e}")
            break

        # Small sleep to avoid high CPU usage
        await asyncio.sleep(0.01)


async def client(server_address):
    """Connect to the shell server and set up the terminal session."""
    global master_fd, child_pid, terminal_size, should_exit

    try:
        async with websockets.connect(server_address) as websocket:
            log(f"Connected to shell server at {server_address}")

            # Set up the PTY
            master_fd, slave_fd = setup_pty()

            # Start the shell
            child_pid = start_shell()

            # Start the PTY I/O handler
            shell_io_task = asyncio.create_task(handle_shell_io(websocket))

            # Process WebSocket messages
            async for message in websocket:
                try:
                    data = json.loads(message)
                    message_type = data.get("type", "")

                    if message_type == "input":
                        # Write input to the PTY
                        input_data = base64.b64decode(data.get("data", ""))

                        # Log commands when Enter is pressed (contains carriage return)
                        if b'\r' in input_data:
                            try:
                                cmd_text = input_data.decode('utf-8', errors='replace').strip()
                                if cmd_text:  # Only print non-empty commands
                                    log(f"Executing command: {cmd_text}")
                            except:
                                log("Executing binary command")

                        # Write to the PTY
                        if master_fd:
                            try:
                                os.write(master_fd, input_data)
                            except OSError as e:
                                # Check if PTY is closed or process is gone
                                if e.errno == 5:  # Input/output error
                                    log("Shell process has terminated")
                                    await websocket.send(json.dumps({
                                        "type": "exit",
                                        "status": 0,
                                        "message": "Shell process has terminated"
                                    }))
                                    break
                                else:
                                    log(f"Error writing to PTY: {e}")
                                    await websocket.send(json.dumps({
                                        "type": "error",
                                        "message": f"Error writing to terminal: {e}"
                                    }))

                    elif message_type == "resize":
                        # Resize the PTY
                        rows = data.get("rows", terminal_size[0])
                        cols = data.get("cols", terminal_size[1])
                        resize_pty(rows, cols)

                    elif message_type == "exit_request":
                        # Client requested exit (e.g., Ctrl+D)
                        log("Exit requested by shell server")

                        # Send an exit signal to the shell if it's still running
                        if child_pid:
                            try:
                                os.kill(child_pid, signal.SIGTERM)
                                log(f"Sent SIGTERM to child process {child_pid}")
                            except OSError:
                                pass

                        # Send confirmation back
                        await websocket.send(json.dumps({
                            "type": "exit",
                            "status": 0,
                            "message": "Session terminated by user request"
                        }))

                        break

                except json.JSONDecodeError:
                    log(f"Received invalid message: {message[:50]}...")
                except Exception as e:
                    log(f"Error processing message: {e}")

            # Cancel the shell I/O task
            shell_io_task.cancel()
            try:
                await shell_io_task
            except asyncio.CancelledError:
                pass

            # Final cleanup before connection closes
            cleanup()

    except websockets.exceptions.ConnectionClosed:
        log("Connection to shell server closed")
    except Exception as e:
        log(f"Error in client: {e}")
    finally:
        cleanup()

def signal_handler(sig, frame):
    """Handle interrupt signals."""
    # Restore terminal to canonical mode to prevent ;5;99~ output
    print("\r\n")  # Ensure cursor is at start of line

    # Perform cleanup
    cleanup()

    # Force exit
    os._exit(0)  # Use _exit instead of sys.exit for more immediate termination

async def main(args):
    """Main function."""
    log("Starting auditable remote shell")
    log(f"Connecting to server at {args.server_url}")

    try:
        await client(args.server_url)
    except KeyboardInterrupt:
        log("Interrupted by user")
    except Exception as e:
        log(f"Error: {e}")
    finally:
        # Cleanup is handled by signal handlers or in the main __name__ == "__main__" block
        pass

if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Auditable terminal client")
    parser.add_argument("server_url", help="WebSocket server URL (e.g. ws://localhost:8080)")

    args = parser.parse_args()

    # Set up signal handlers at the top level
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        # This should be caught by the signal handler, but just in case:
        print("\r\nClient terminated by user")
        cleanup()
        os._exit(0)
    except Exception as e:
        print(f"\r\nError: {e}")
        cleanup()
        sys.exit(1)
    finally:
        # Make absolutely sure we cleanup
        cleanup()
