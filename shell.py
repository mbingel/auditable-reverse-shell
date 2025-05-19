#!/usr/bin/env python3
import asyncio
import websockets
import sys
import os
import signal
import json
import termios
import tty
import fcntl
import struct
import base64
import select
import argparse

# Global state
original_term_settings = None
active_websocket = None
should_exit = False

def get_terminal_size():
    """Get the current terminal size."""
    if sys.stdout.isatty():
        s = struct.pack('HHHH', 0, 0, 0, 0)
        x = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, s)
        rows, cols, _, _ = struct.unpack('HHHH', x)
        return rows, cols
    else:
        return 24, 80

def set_terminal_raw():
    """Set terminal to raw mode."""
    global original_term_settings
    if sys.stdin.isatty():
        original_term_settings = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno(), termios.TCSANOW)
        sys.stdout.write("\x1b[?25h")  # Show cursor
        sys.stdout.flush()

def restore_terminal():
    """Restore terminal to original settings."""
    global original_term_settings
    if original_term_settings and sys.stdin.isatty():
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, original_term_settings)
        sys.stdout.write("\x1b[?25h")  # Show cursor
        sys.stdout.flush()
        print("\r\nTerminal restored")

def sigwinch_handler(signum, frame):
    """Handle terminal resize events."""
    global active_websocket

    if active_websocket:
        rows, cols = get_terminal_size()
        asyncio.create_task(send_terminal_size(active_websocket, rows, cols))

async def send_terminal_size(websocket, rows, cols):
    """Send terminal size to client."""
    await websocket.send(json.dumps({
        "type": "resize",
        "rows": rows,
        "cols": cols
    }))

async def read_stdin(websocket):
    """Read from stdin and send to client."""
    global should_exit

    while not should_exit:
        # Wait for stdin to be ready for reading
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0.1)

            if r:
                try:
                    # Read from stdin
                    data = os.read(sys.stdin.fileno(), 1024)

                    if not data:  # Empty data means EOF (Ctrl+D)
                        print("\r\nCtrl+D detected, closing session...")
                        await websocket.send(json.dumps({
                            "type": "exit_request"
                        }))
                        should_exit = True
                        break

                    # Send all keystrokes immediately
                    await websocket.send(json.dumps({
                        "type": "input",
                        "data": base64.b64encode(data).decode('ascii')
                    }))

                except (OSError, IOError) as e:
                    print(f"\r\nError reading from stdin: {e}")
                    should_exit = True
                    break
        except (select.error, ValueError) as e:
            # This can happen if stdin is closed
            print(f"\r\nStdin no longer available: {e}")
            should_exit = True
            break

        await asyncio.sleep(0.01)  # Small sleep to avoid high CPU usage

async def handle_client_session(websocket):
    """Handle the client session."""
    global active_websocket, should_exit

    active_websocket = websocket
    print("Client connected. Setting up terminal emulation...")

    # Set terminal to raw mode
    set_terminal_raw()

    # Set up window change signal handler
    signal.signal(signal.SIGWINCH, sigwinch_handler)

    # Send initial terminal size
    rows, cols = get_terminal_size()
    await send_terminal_size(websocket, rows, cols)

    # Start reading from stdin
    stdin_task = asyncio.create_task(read_stdin(websocket))

    try:
        # Welcome message - ensure proper formatting
        sys.stdout.write("\033[2J\033[H")  # Clear screen and move cursor to top-left
        sys.stdout.write("\r\n")
        sys.stdout.write("Auditable Remote Shell - Terminal\r\n")
        sys.stdout.write("Press Ctrl+D to exit the session.\r\n")
        sys.stdout.write("\r\n")
        sys.stdout.flush()

        # Process incoming messages from client
        async for message in websocket:
            try:
                data = json.loads(message)
                message_type = data.get("type", "")

                if message_type == "output":
                    # Decode base64 output and write to stdout
                    output_data = base64.b64decode(data.get("data", ""))
                    os.write(sys.stdout.fileno(), output_data)

                elif message_type == "error":
                    # Display error message
                    error_msg = data.get("message", "")
                    sys.stderr.write(f"\r\nError: {error_msg}\r\n")
                    sys.stderr.flush()


                elif message_type == "exit":
                    # Client is exiting
                    exit_status = data.get("status", 0)
                    exit_msg = data.get("message", "")
                    if exit_msg:
                        print(f"\r\n{exit_msg}")
                    print(f"\r\nRemote session ended with status: {exit_status}")
                    break

            except json.JSONDecodeError:
                print(f"\r\nReceived invalid message: {message[:50]}...")

    except websockets.exceptions.ConnectionClosed:
        print("\r\nConnection closed by client")
    except Exception as e:
        print(f"\r\nError in session: {e}")
    finally:
        # Mark as exiting
        should_exit = True

        # Cancel the stdin task
        if stdin_task and not stdin_task.done():
            stdin_task.cancel()
            try:
                await stdin_task
            except asyncio.CancelledError:
                pass

        # Reset terminal
        active_websocket = None

        # Make sure terminal is properly restored
        sys.stdout.write("\033c")  # Reset terminal
        sys.stdout.flush()
        restore_terminal()

        print("\r\nShell session terminated.")

async def main(args):
    """Main function."""
    global should_exit

    # Setup signal handlers
    def sigint_handler(sig, frame):
        global should_exit
        print("\r\nShutting down server...")
        should_exit = True
        restore_terminal()
        sys.exit(0)

    signal.signal(signal.SIGINT, sigint_handler)

    # Start the WebSocket server
    async with websockets.serve(handle_client_session, args.host, args.port):
        print(f"Terminal shell server running on ws://{args.host}:{args.port}")
        print("Waiting for connections...")

        # Keep server running until interrupted
        while not should_exit:
            await asyncio.sleep(1)

if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Terminal emulation shell server")
    parser.add_argument("host", help="Host to bind to")
    parser.add_argument("port", type=int, help="Port to listen on")

    args = parser.parse_args()

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\r\nServer terminated by user")
    finally:
        restore_terminal()
