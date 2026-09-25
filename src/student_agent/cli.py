from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .competition import submission_status, submit_artifact
from .config import Settings
from .contracts import Contracts
from .gateway_compat import connect_compatible_gateway
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


def _gateway(settings: Settings, contracts: Contracts):
    if settings.mcp_transport == "sync":
        return connect_compatible_gateway(settings.mcp_endpoint, settings.team_api_key, contracts)
    return connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts)


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with _gateway(settings, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


def _resume_completed(root: Path, case_ids: tuple[str, ...], contracts: Contracts) -> set[str]:
    trace_path = root / "traces" / "trace.jsonl"
    if not trace_path.exists():
        raise ValueError("Cannot resume without the original trace")
    original = trace_path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in original.splitlines() if line.strip()]
    for event in events:
        contracts.validate_trace(event, "resume trace")
        if event["case_id"] not in case_ids:
            raise ValueError("Resume trace is for a different case set")
    completed = set()
    for case_id in case_ids:
        case_events = [e for e in events if e["case_id"] == case_id]
        if not case_events or case_events[-1]["event_type"] != "case_finalized":
            continue
        output = json.loads((root / "outputs" / f"{case_id}.json").read_text(encoding="utf-8"))
        contracts.validate_output(output, f"resume {case_id}")
        if output["case_id"] != case_id:
            raise ValueError("Resume output case ID mismatch")
        kinds = [e["event_type"] for e in case_events]
        if kinds.count("case_received") != 1 or "verification_completed" not in kinds:
            raise ValueError("Resume output has no verified lifecycle")
        consumed = {
            ref
            for e in case_events
            if e["event_type"] == "tool_result_consumed"
            for ref in e.get("evidence_refs", [])
        }
        output_refs = set(output["evidence_refs"])
        if not output_refs or not output_refs <= consumed:
            continue
        completed.add(case_id)
    if any(e["case_id"] not in completed for e in events):
        # Retain an audit of the interrupted attempt, outside the submission allowlist.
        with trace_path.with_name("interrupted-attempts.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(original)
        trace_path.write_text(
            "".join(
                json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n"
                for e in events
                if e["case_id"] in completed
            ),
            encoding="utf-8",
        )
    return completed


async def _run(root: Path, input_root: Path, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(input_root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    async with _gateway(settings, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        # Do not discard a previous run when authentication/discovery fails.
        completed = _resume_completed(root, case_set.case_ids, contracts) if resume else set()
        if not resume:
            for stale in output_root.glob("*.json"):
                stale.unlink()
            trace_path.unlink(missing_ok=True)
        trace = TraceWriter(trace_path, contracts)
        for case_id in case_set.case_ids:
            if case_id in completed:
                print(f"KEEP: {case_id}", flush=True)
                continue
            case = case_set.cases[case_id]
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            target = output_root / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            print(f"OK: {case_id}", flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    result.add_argument("--input-root", help="directory containing case-set.json and inputs/")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume",
        action="store_true",
        help="keep verified cases; only use while the SAME competition run is active",
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    submit = commands.add_parser(
        "submit", help="validate, package and upload ZIP to the active run"
    )
    submit.add_argument("--output", default="dist/submission.zip")
    status = commands.add_parser("status", help="read scoring status for a submission receipt")
    status.add_argument("receipt")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    input_root = (root / args.input_root).resolve() if args.input_root else root
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(input_root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, input_root, args.resume))
        elif args.command == "validate":
            case_set = load_case_set(input_root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output, input_root)
            print(f"OK: {destination}")
        elif args.command == "submit":
            settings = Settings.load(root)
            destination = package_submission(root, root / args.output, input_root)
            receipt = submit_artifact(settings, destination)
            destination.with_suffix(".receipt.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(receipt, ensure_ascii=False))
        elif args.command == "status":
            print(
                json.dumps(submission_status(Settings.load(root), args.receipt), ensure_ascii=False)
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
