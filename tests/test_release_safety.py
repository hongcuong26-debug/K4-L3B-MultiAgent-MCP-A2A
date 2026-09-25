import subprocess
from pathlib import Path, PurePosixPath


def test_repository_contains_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    # Runtime inputs/outputs are expected after `day09 run`; releases must not track them.
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True, text=True
        )
        paths = [PurePosixPath(name) for name in result.stdout.split("\0") if name]
    else:
        paths = [path.relative_to(root) for path in root.rglob("*") if path.is_file()]
    assert not any(path.name == "case-set.json" for path in paths)
    assert not any(
        path.parts[0] in {"inputs", "outputs"} and path.suffix == ".json" for path in paths
    )
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(path.name in forbidden for path in paths)


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
