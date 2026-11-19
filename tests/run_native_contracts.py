"""Compile real pure C# contracts without starting WinUI, HTTP, or audio.

Usage: python tests/run_native_contracts.py --dotnet /path/to/dotnet --output NEW_DIR
The original monolithic NativeState is extracted verbatim for pre-fix red tests;
after the split, the actual production contract source is copied byte-for-byte.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native/SaltPlayer.Native"
PURE = ["Models/NativeState.cs", "Models/ApiProtocol.cs", "Models/NativePlaybackDecision.cs",
        "Models/PlayerPresentation.cs", "Services/PlayerService.cs"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dotnet", default="dotnet")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
    evidence = args.output
    with tempfile.TemporaryDirectory(prefix="native-contract-") as folder:
        work = Path(folder)
        product = work / "Product"
        product.mkdir()
        for name in ("Program.cs", "FrameworkStubs.cs", "NativeContract.Tests.csproj"):
            shutil.copyfile(ROOT / "tests/native-contract" / name, work / name)
        inventory = []
        for relative in PURE:
            source = NATIVE / relative
            if source.is_file():
                data = source.read_bytes()
                (product / source.name).write_bytes(data)
                inventory.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(),
                                  "mode": "verbatim-production-source"})
        if not (product / "NativeState.cs").exists():
            source = NATIVE / "Models/Models.cs"
            text = source.read_text(encoding="utf-8-sig")
            start = "public sealed class NativeState\n{"
            end = "public sealed class FavoriteResponse"
            assert text.count(start) == 1 and text.count(end) == 1
            block = text[text.index(start):text.index(end)]
            (product / "NativeState.cs").write_text(
                "using System.Text.Json.Serialization;\nnamespace SaltPlayer.Native.Models;\n" + block,
                encoding="utf-8", newline="\n")
            inventory.append({"path": "Models/Models.cs", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                              "mode": "verbatim-NativeState-block-for-baseline"})
        env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1")
        command = [args.dotnet, "run", "--project", str(work / "NativeContract.Tests.csproj"),
                   "--configuration", "Release", "--verbosity", "quiet"]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", env=env, timeout=150)
        output = result.stdout + result.stderr
        print(output, end="")
        if evidence:
            (evidence / "output.log").write_text(output, encoding="utf-8", newline="\n")
            (evidence / "result.json").write_text(json.dumps(
                {"command": command, "returncode": result.returncode, "production_sources": inventory,
                 "test_sha256": hashlib.sha256((ROOT / 'tests/native-contract/Program.cs').read_bytes()).hexdigest()},
                ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
