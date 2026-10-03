"""The ``jobpipeline`` command (spec 7.1). M0.1 supports ``--version`` only."""

import argparse
from collections.abc import Sequence

from app import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobpipeline",
        description="Run the Job Search Pipeline on this computer.",
    )
    parser.add_argument("--version", action="version", version=f"jobpipeline {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    build_parser().parse_args(argv)
