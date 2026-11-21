"""用实际保存的 bundle 生成三种产物，并以隔离解释器核对离线推理。"""

import argparse
import asyncio
import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from sqlalchemy import select

from api.services.kiln_studio import export_bundle
from common.kiln_model_export import zip_python_files
from db.models.kiln_studio import KilnStudioAsset
from db.session import AsyncSessionLocal, engine


async def verify(bundle_id, output, python):
    engine.echo = False
    async with AsyncSessionLocal() as db:
        bundle = (
            await db.execute(
                select(KilnStudioAsset).where(
                    KilnStudioAsset.public_id == bundle_id, KilnStudioAsset.kind == "bundle"
                )
            )
        ).scalar_one()
        numeric = await export_bundle(db, bundle.user_id, bundle_id, "json")
        skill = await export_bundle(db, bundle.user_id, bundle_id, "skill")
        package = await export_bundle(db, bundle.user_id, bundle_id, "python")
    output.mkdir(parents=True, exist_ok=True)
    base = f"kiln-model-{bundle.payload['version']}"
    artifacts = {
        base + ".model.json": json.dumps(numeric, ensure_ascii=False, indent=2).encode("utf-8"),
        base + ".skill.json": json.dumps(skill, ensure_ascii=False, indent=2).encode("utf-8"),
        base + ".python.zip": zip_python_files(package),
    }
    for name, content in artifacts.items():
        (output / name).write_bytes(content)
    unpacked = output / (base + "-python")
    with ZipFile(BytesIO(artifacts[base + ".python.zip"])) as archive:
        archive.extractall(unpacked)
    check = subprocess.run(
        [str(python), "-I", str((unpacked / "predict.py").resolve()), "--verify"],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    distributions = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import importlib.metadata as m,json;"
            "print(json.dumps([d.metadata['Name'] for d in m.distributions()]))",
        ],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    report = {
        "bundleId": bundle.public_id,
        "title": bundle.title,
        "version": bundle.payload["version"],
        "files": [{"filename": name, "bytes": len(content)} for name, content in artifacts.items()],
        "offlinePython": str(python),
        "installedDistributions": json.loads(distributions.stdout),
        "predictions": json.loads(check.stdout),
        "platformVerification": "passed",
    }
    (output / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    try:
        await verify(args.bundle_id, args.output, args.python)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
