from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

from assistant.contracts import SkillCall, SkillResult
from assistant.policy.network import offline_lock
from assistant.skills.base import Skill


async def run(package_dir: Path, entry_point: str) -> dict:
    package_dir = package_dir.resolve()
    sys.path.insert(0, str(package_dir))
    module_name, object_name = entry_point.split(":", 1)
    skill = getattr(importlib.import_module(module_name), object_name)()
    if not isinstance(skill, Skill):
        raise TypeError("Entry point does not create a Skill")
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    if request.get("operation") != "execute":
        raise ValueError("Unsupported worker operation")
    call = SkillCall.model_validate(request["call"])
    if call.skill_id != skill.manifest.skill_id:
        raise ValueError("Skill ID mismatch")
    skill.validate(call.arguments)
    with offline_lock(os.environ.get("ASSISTANT_OFFLINE") == "1"):
        result = await skill.execute(call, asyncio.Event())
    if not isinstance(result, SkillResult):
        raise TypeError("Skill returned an invalid result")
    skill.validate_result(result)
    return {"ok": True, "result": result.model_dump(mode="json")}


def main() -> int:
    try:
        payload = asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
    except Exception as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(payload))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
