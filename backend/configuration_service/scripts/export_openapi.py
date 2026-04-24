"""
Dump the configuration_service OpenAPI schema to JSON.

Usage:
    python scripts/export_openapi.py
    python scripts/export_openapi.py -o /path/to/configuration_service.openapi.json

The monorepo-level default lives at `<repo>/shared-schema/configuration_service.openapi.json`
so both docker-compose (shared volume) and downstream tooling have a single source of truth.
"""
import argparse
import json
import sys
from pathlib import Path

from configuration_service.main import create_app


def build_schema() -> dict:
    # create_app() accepts optional Settings; defaults are fine for schema-only extraction
    # since handlers aren't invoked.
    app = create_app()
    return app.openapi()


def main() -> int:
    # Default: <monorepo-root>/shared-schema/configuration_service.openapi.json.
    # The service dir is ophyd-service/backend/configuration_service; climb two levels.
    default_output = Path(__file__).resolve().parents[3] / "shared-schema" / "configuration_service.openapi.json"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path, default=default_output)
    args = parser.parse_args()

    schema = build_schema()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Wrote {args.output} ({len(schema['paths'])} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
