from __future__ import annotations

import sys

from .prepare import main as prepare_main
from .qa import main as qa_main


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "prepare":
        return prepare_main(argv[1:])
    return qa_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
