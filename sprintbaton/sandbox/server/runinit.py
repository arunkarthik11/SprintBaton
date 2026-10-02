"""The first process of a run with egress (hosted-sandbox-isolation spec §8.1).

Executed *inside* the run's bubblewrap sandbox — stdlib only, no imports from
`sprintbaton` at all, because inside the sandbox it is just a script on a
read-only mount. It starts the egress **bridge** (a TCP listener on the run's
private loopback, `127.0.0.1:<port>`) that forwards each connection to the
per-run Unix socket the sandbox service bound into the run, then runs the real
command and exits with its status.

The run token is added by the service's relay on the *other* side of the Unix
socket; nothing in the run ever holds it.

Usage: runinit.py --bridge-port N --socket PATH -- argv...
"""

import socket
import subprocess
import sys
import threading


def _pipe(a: socket.socket, b: socket.socket) -> None:
    def forward(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t = threading.Thread(target=forward, args=(b, a), daemon=True)
    t.start()
    forward(a, b)
    t.join()
    a.close()
    b.close()


def _listen(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(64)
    return listener


def _serve_bridge(listener: socket.socket, sock_path: str) -> None:
    while True:
        client, _ = listener.accept()
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(sock_path)
        except OSError:
            client.close()
            upstream.close()
            continue
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()


def main(argv: list[str]) -> int:
    if "--" not in argv:
        print("runinit: missing -- before the command", file=sys.stderr)
        return 2
    split = argv.index("--")
    opts, command = argv[:split], argv[split + 1:]
    port = int(opts[opts.index("--bridge-port") + 1])
    sock_path = opts[opts.index("--socket") + 1]
    # Listening before the command starts, so its first request never races
    # the bridge.
    listener = _listen(port)
    threading.Thread(target=_serve_bridge, args=(listener, sock_path),
                     daemon=True).start()
    try:
        return subprocess.call(command)
    except FileNotFoundError:
        print(f"runinit: command not found: {command[0]}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
