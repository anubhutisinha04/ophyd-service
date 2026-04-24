"""
Command-line interface for Bluesky Direct Device Control Service.

This module provides CLI entry points for running the direct control service.
After installation via pip, you can run:

    bluesky-direct-control

Or with custom configuration:

    bluesky-direct-control --host 0.0.0.0 --port 8003
"""

import argparse
import sys
from typing import Optional

import uvicorn


def main(argv: Optional[list[str]] = None) -> int:
    """
    Main entry point for the bluesky-direct-control CLI.
    
    This allows the service to be run via:
        pip install bluesky-direct-control
        bluesky-direct-control
    
    Or from a monorepo:
        pip install bluesky-remote[direct-control]
        bluesky-direct-control
    
    Args:
        argv: Command-line arguments (for testing)
    
    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    parser = argparse.ArgumentParser(
        description=(
            "Bluesky Direct Device Control + Monitoring Service (SVC-003): "
            "A4-coordinated commanding plus real-time EPICS PV streaming."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    
    # Server configuration
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8003,
        help="Port to bind to"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of worker processes (use 1 for WebSocket support)"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload on code changes (development only)"
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Logging level"
    )
    
    # SSL/TLS configuration
    parser.add_argument(
        "--ssl-keyfile",
        help="SSL key file path (for HTTPS)"
    )
    parser.add_argument(
        "--ssl-certfile",
        help="SSL certificate file path (for HTTPS)"
    )
    
    # Proxy configuration
    parser.add_argument(
        "--proxy-headers",
        action="store_true",
        help="Enable proxy header support (for deployment behind reverse proxy)"
    )
    parser.add_argument(
        "--forwarded-allow-ips",
        default="127.0.0.1",
        help="Comma-separated list of IPs to trust for proxy headers"
    )
    
    args = parser.parse_args(argv)

    # Display startup information
    print("Starting Direct Device Control + Monitoring Service (SVC-003)")
    print(f"  Host: {args.host}")
    print(f"  Port: {args.port}")
    print(f"  Workers: {args.workers}")
    print(f"  Log Level: {args.log_level}")
    if args.ssl_keyfile:
        print("  SSL: Enabled")
    if args.proxy_headers:
        print("  Proxy Headers: Enabled")
    print()
    print(f"API Documentation: http://{args.host}:{args.port}/docs")
    print(f"Health Check: http://{args.host}:{args.port}/health")
    print()

    # Build uvicorn configuration - use factory=True so Settings() reads env vars after they're set
    uvicorn_config = {
        "app": "direct_control.main:create_app",
        "factory": True,
        "host": args.host,
        "port": args.port,
        "log_level": args.log_level,
        "workers": args.workers if not args.reload else 1,  # reload incompatible with workers
        "reload": args.reload,
        "proxy_headers": args.proxy_headers,
        "forwarded_allow_ips": args.forwarded_allow_ips,
    }

    # Add SSL if configured
    if args.ssl_keyfile and args.ssl_certfile:
        uvicorn_config["ssl_keyfile"] = args.ssl_keyfile
        uvicorn_config["ssl_certfile"] = args.ssl_certfile
    elif args.ssl_keyfile or args.ssl_certfile:
        print("ERROR: Both --ssl-keyfile and --ssl-certfile must be provided together", file=sys.stderr)
        return 1

    try:
        uvicorn.run(**uvicorn_config)
        return 0
    except KeyboardInterrupt:
        print("\nShutting down Direct Device Control Service...")
        return 0
    except Exception as e:
        print(f"ERROR: Failed to start server: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
