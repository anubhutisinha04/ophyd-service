"""
Dump the direct_control_service OpenAPI schema to JSON.

Usage:
    python scripts/export_openapi.py
    python scripts/export_openapi.py -o /path/to/direct_control.openapi.json

The monorepo-level default lives at `<repo>/shared-schema/direct_control.openapi.json`
so both docker-compose (shared volume) and downstream tooling have a single source of truth.

WebSocket endpoints do not appear in OpenAPI — their contract is documented separately
in the service README.
"""
import argparse
import json
import sys
from pathlib import Path

from direct_control.main import app


def main() -> int:
    default_output = Path(__file__).resolve().parents[3] / "shared-schema" / "direct_control.openapi.json"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path, default=default_output)
    args = parser.parse_args()

    schema = app.openapi()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Wrote {args.output} ({len(schema['paths'])} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
