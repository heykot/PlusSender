#!/usr/bin/env python3
"""Зручний launcher: `python run.py` — еквівалент `python -m plus_sender`."""
from plus_sender.__main__ import main
import asyncio

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
