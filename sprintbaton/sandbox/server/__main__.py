"""`python -m sprintbaton.sandbox.server` — the sandbox pod's entrypoint."""

from sprintbaton.sandbox.server.app import serve

if __name__ == "__main__":
    serve()
