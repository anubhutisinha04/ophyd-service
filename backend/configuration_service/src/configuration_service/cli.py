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

    args = parser.parse_args()

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
    print(f"Starting Configuration Service (SVC-004)")
    print(f"  Host: {args.host}")
    print(f"  Port: {args.port}")
    print(f"  Workers: {args.workers}")
    print(f"  Log Level: {args.log_level}")
    print(f"  Profile Path: {args.profile_path or 'Not set'}")
    print(f"  Load Strategy: {effective_strategy}")
    if args.ssl_keyfile:
        print(f"  SSL: Enabled")
    if args.proxy_headers:
        print(f"  Proxy Headers: Enabled")
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


if __name__ == "__main__":
    main()
