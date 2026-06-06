from __future__ import annotations

import argparse

import uvicorn

from geng_agent.web.app import app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="耿同学agent 极简 Web：实时阶段进度")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())