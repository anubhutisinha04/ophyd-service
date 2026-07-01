"""
CLI entry point for Configuration Service (SVC-004).

Provides command-line interface for running the service via uvicorn.
Matches pattern from SVC-001, SVC-002, and SVC-003 for consistency.
"""

import argparse
import os
import sys

import uvicorn


def main() -> None:
    """
    Main CLI entry point for bluesky-configuration-service.

    Runs the Configuration Service FastAPI application using uvicorn.
    Configuration via environment variables (see config.py) or command-line args.
    """
    parser = argparse.ArgumentParser(
        description="Bluesky Configuration Service (SVC-004) - Static device registry",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Server configuration
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host to bind the server to",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8004,
        help="Port to bind the server to",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of worker processes",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Logging level",
    )

    # SSL/TLS configuration
    parser.add_argument(
        "--ssl-keyfile",
        type=str,
        help="Path to SSL key file",
    )
    parser.add_argument(
        "--ssl-certfile",
        type=str,
        help="Path to SSL certificate file",
    )
    parser.add_argument(
        "--ssl-ca-certs",
        type=str,
        help="Path to SSL CA certificates file",
    )

    # Proxy configuration
    parser.add_argument(
        "--proxy-headers",
        action="store_true",
        help="Enable proxy headers (X-Forwarded-For, X-Forwarded-Proto)",
    )
    parser.add_argument(
        "--forwarded-allow-ips",
        type=str,
        help="Comma-separated list of IPs to trust with proxy headers",
    )

    # Configuration service specific (defaults from environment variables)
    parser.add_argument(
        "--profile-path",
        type=str,
        default=os.environ.get("CONFIG_PROFILE_PATH"),
        help="Path to Bluesky profile collection directory (e.g., /opt/bluesky/profile_collection). Env: CONFIG_PROFILE_PATH",
    )
    parser.add_argument(
        "--load-strategy",
        type=str,
        choices=["auto", "empty", "happi", "bits", "mock"],
        default=os.environ.get("CONFIG_LOAD_STRATEGY", "auto"),
        help="Loading strategy: auto (detect based on files), empty (no devices, populated via CRUD), happi (LCLS/SLAC JSON), bits (BCDA-APS YAML), or mock. Env: CONFIG_LOAD_STRATEGY",
    )
    parser.add_argument(
        "--use-mock-data",
        action="store_true",
        help="Use mock data (shortcut for --load-strategy mock)",
    )

    # Subcommands. With no subcommand the service starts the server (the
    # historical behavior); ``export`` is an offline one-shot that loads a
    # registry and serializes it without starting uvicorn.
    subparsers = parser.add_subparsers(dest="command")
    export_parser = subparsers.add_parser(
        "export",
        help="Export the device registry to happi JSON or BITS devices.yml and exit",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=(
            "Load devices from a profile collection (or mock data) and serialize the "
            "resulting registry to happi JSON (default) or BITS (BCDA-APS guarneri) "
            "devices.yml. Writes to --output or stdout. Does not start the server."
        ),
    )
    export_parser.add_argument(
        "--format",
        type=str,
        choices=["happi", "bits"],
        default="happi",
        help="Output format: happi JSON (default) or bits (guarneri devices.yml)",
    )
    export_parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="Write to this file instead of stdout",
    )
    export_parser.add_argument(
        "--profile-path",
        type=str,
        default=os.environ.get("CONFIG_PROFILE_PATH"),
        help="Path to the profile collection directory. Env: CONFIG_PROFILE_PATH",
    )
    export_parser.add_argument(
        "--load-strategy",
        type=str,
        choices=["auto", "empty", "happi", "bits", "mock"],
        default=os.environ.get("CONFIG_LOAD_STRATEGY", "auto"),
        help="How to load devices before export (see server --load-strategy). Env: CONFIG_LOAD_STRATEGY",
    )
    export_parser.add_argument(
        "--use-mock-data",
        action="store_true",
        help="Use mock data (shortcut for --load-strategy mock)",
    )

    args = parser.parse_args()

    if getattr(args, "command", None) == "export":
        sys.exit(_run_export(args))

    # Strategies that read from disk require a profile path. Fail at parse
    # rather than letting it propagate to a late RuntimeError in loader.py.
    effective_strategy = "mock" if args.use_mock_data else args.load_strategy
    if effective_strategy in ("auto", "happi", "bits") and not args.profile_path:
        parser.error(
            f"--profile-path (or env CONFIG_PROFILE_PATH) is required for "
            f"--load-strategy={effective_strategy}. Use --load-strategy=empty "
            f"(populate via CRUD) or --load-strategy=mock (sample data) if you "
            f"don't have a profile collection directory."
        )

    # Set environment variables for service configuration (matches config.py CONFIG_ prefix)
    if args.profile_path:
        os.environ["CONFIG_PROFILE_PATH"] = args.profile_path

    if args.use_mock_data:
        os.environ["CONFIG_LOAD_STRATEGY"] = "mock"
    elif args.load_strategy:
        os.environ["CONFIG_LOAD_STRATEGY"] = args.load_strategy

    # Build uvicorn configuration
    # Use factory=True so uvicorn calls create_app() AFTER env vars are set
    uvicorn_config = {
        "app": "configuration_service.main:create_app",
        "factory": True,
        "host": args.host,
        "port": args.port,
        "workers": args.workers,
        "log_level": args.log_level,
        "reload": args.reload,
    }

    # Add SSL configuration if provided
    if args.ssl_keyfile and args.ssl_certfile:
        uvicorn_config["ssl_keyfile"] = args.ssl_keyfile
        uvicorn_config["ssl_certfile"] = args.ssl_certfile
        if args.ssl_ca_certs:
            uvicorn_config["ssl_ca_certs"] = args.ssl_ca_certs

    # Add proxy configuration if enabled
    if args.proxy_headers:
        uvicorn_config["proxy_headers"] = True
        if args.forwarded_allow_ips:
            uvicorn_config["forwarded_allow_ips"] = args.forwarded_allow_ips

    # Determine effective load strategy
    effective_strategy = "mock" if args.use_mock_data else args.load_strategy

    # Display startup information
    print("Starting Configuration Service (SVC-004)")
    print(f"  Host: {args.host}")
    print(f"  Port: {args.port}")
    print(f"  Workers: {args.workers}")
    print(f"  Log Level: {args.log_level}")
    print(f"  Profile Path: {args.profile_path or 'Not set'}")
    print(f"  Load Strategy: {effective_strategy}")
    if args.ssl_keyfile:
        print("  SSL: Enabled")
    if args.proxy_headers:
        print("  Proxy Headers: Enabled")
    print()
    print(f"API Documentation: http://{args.host}:{args.port}/docs")
    print(f"Health Check: http://{args.host}:{args.port}/health")
    print()

    try:
        uvicorn.run(**uvicorn_config)
    except KeyboardInterrupt:
        print("\nShutting down Configuration Service...")
        sys.exit(0)
    except Exception as e:
        print(f"Error starting service: {e}", file=sys.stderr)
        sys.exit(1)


def _run_export(args: argparse.Namespace) -> int:
    """Load a device registry and serialize it to happi JSON or BITS devices.yml.

    Reuses ``DeviceRegistryStore``'s serializers by seeding a throwaway on-disk
    SQLite database from the loaded registry, so the CLI produces the same
    registry export as the ``GET /api/v1/registry/export`` endpoint. (The
    rendering differs: this CLI pretty-prints happi JSON with a trailing
    newline for terminal use, whereas the endpoint returns compact JSON.)
    Returns a process exit code.
    """
    import json
    import tempfile
    from pathlib import Path

    import yaml

    from .config import Settings
    from .db import make_engine
    from .device_registry_store import DeviceRegistryStore
    from .loader import create_loader

    effective_strategy = "mock" if args.use_mock_data else args.load_strategy
    if effective_strategy in ("auto", "happi", "bits") and not args.profile_path:
        print(
            f"Error: --profile-path (or env CONFIG_PROFILE_PATH) is required for "
            f"--load-strategy={effective_strategy}. Use --load-strategy=empty or "
            f"--load-strategy=mock if you don't have a profile collection directory.",
            file=sys.stderr,
        )
        return 2

    settings = Settings(
        profile_path=Path(args.profile_path) if args.profile_path else None,
        load_strategy=args.load_strategy,
        use_mock_data=args.use_mock_data,
    )

    try:
        registry = create_loader(settings).load_registry()
    except Exception as e:
        print(f"Error loading devices: {e}", file=sys.stderr)
        return 1

    # A throwaway file-backed SQLite DB (not :memory:, which would be a distinct
    # database per pooled connection) lets us reuse the store's serializers.
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = make_engine(f"sqlite+pysqlite:///{Path(tmpdir) / 'export.db'}")
        store = DeviceRegistryStore(engine)
        try:
            store.initialize()
            store.seed_from_registry(registry)
            device_count = store.device_count()
            if args.format == "bits":
                text = yaml.safe_dump(store.export_bits(), default_flow_style=False, sort_keys=True)
            else:
                text = json.dumps(store.export_happi(), indent=2, sort_keys=True) + "\n"
        except Exception as e:
            print(f"Error exporting registry: {e}", file=sys.stderr)
            return 1
        finally:
            store.close()

    if args.output:
        Path(args.output).write_text(text)
        print(
            f"Exported {device_count} device(s) to {args.output} ({args.format} format)",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")

    return 0


if __name__ == "__main__":
    main()
