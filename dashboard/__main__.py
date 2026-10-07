import argparse

import uvicorn

from dashboard.app import DEFAULT_PORT, create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Review dashboard for generated outreach emails.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port on 127.0.0.1 (default: {DEFAULT_PORT}).")
    args = parser.parse_args()
    print(f"Email review dashboard: http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(port=args.port), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
