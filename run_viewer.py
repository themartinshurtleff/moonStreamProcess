#!/usr/bin/env python3
"""
Run the BTC Perpetual Metrics Viewer.

Usage:
    python run_viewer.py [--exchanges EXCHANGE1,EXCHANGE2,...]

Examples:
    python run_viewer.py                  # Run with Binance only (default)
    python run_viewer.py --all            # Run with all exchanges
    python run_viewer.py --exchanges binance,bybit
"""

import sys
import os
import argparse
import subprocess

def check_dependencies():
    """Check and install required dependencies."""
    required = ['websockets', 'rich']
    missing = []

    for pkg in required:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if missing:
        print(f"Installing missing dependencies: {', '.join(missing)}")
        subprocess.check_call([
            sys.executable, '-m', 'pip', 'install', *missing, '--quiet'
        ])
        print("Dependencies installed successfully.")

def main():
    parser = argparse.ArgumentParser(
        description='BTC Perpetual Metrics Viewer - Real-time display of BTC perp metrics'
    )
    parser.add_argument(
        '--exchanges', '-e',
        type=str,
        default='binance',
        help='Comma-separated list of exchanges (default: binance)'
    )
    parser.add_argument(
        '--all', '-a',
        action='store_true',
        help='Use all available exchanges'
    )
    args = parser.parse_args()

    # Check dependencies
    check_dependencies()

    # Set up environment
    os.environ['EXCHANGES'] = 'binance,okx,bybit' if args.all else args.exchanges

    # Import and run the viewer
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'poc'))

    import asyncio
    from metrics_viewer import main as viewer_main

    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║        BTC PERPETUAL METRICS VIEWER - Proof of Concept       ║
    ╠══════════════════════════════════════════════════════════════╣
    ║  Connecting to exchanges and streaming real-time data...     ║
    ║  Press Ctrl+C to exit                                        ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    try:
        asyncio.run(viewer_main())
    except KeyboardInterrupt:
        print("\nExiting...")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
