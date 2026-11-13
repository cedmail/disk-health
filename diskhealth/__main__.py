"""CLI: python3 -m diskhealth [serve|scan|brew-check|brewed|status]"""
import json
import sys

from . import brew, server


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        server.serve()
    elif cmd == "scan":
        server.full_scan()
    elif cmd == "brew-check":
        server.brew_check()
    elif cmd == "brewed":
        sys.exit(1 if brew.brewed() else 0)
    elif cmd == "status":
        print(json.dumps(server.status(), indent=2))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
