from __future__ import annotations

import argparse
from pathlib import Path

from .delivery import DeliveryError
from .llm import LLMError
from .pipeline import PipelineError, deliver, prepare
from .state import StateError

parser = argparse.ArgumentParser(description="Daily dexterous-hand digest")
commands = parser.add_subparsers(dest="command", required=True)
build = commands.add_parser("prepare")
build.add_argument("--root", type=Path, default=Path("."))
build.add_argument("--config", type=Path, default=Path("digest_config.yaml"))
build.add_argument("--output", type=Path, default=Path(".run"))
build.add_argument("--preview", action="store_true")
build.add_argument("--retry-uncertain", action="store_true")
send = commands.add_parser("deliver")
send.add_argument("--source", type=Path, required=True)
send.add_argument("--output", type=Path, default=Path(".run/result"))
args = parser.parse_args()
try:
    if args.command == "prepare":
        prepare(args.root, args.config, args.output, args.preview, args.retry_uncertain)
    else:
        deliver(args.source, args.output)
except (PipelineError, StateError, LLMError, DeliveryError) as error:
    parser.exit(1, str(error) + "\n")
