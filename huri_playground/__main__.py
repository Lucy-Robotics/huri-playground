"""Run the playground: ``huri-playground`` or ``python -m huri_playground``
(see the README)."""

import logging
import os

import uvicorn

from .gateway import create_app


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_app(),
        host=os.environ.get("PLAYGROUND_HOST", "127.0.0.1"),
        port=int(os.environ.get("PLAYGROUND_PORT", "8080")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
