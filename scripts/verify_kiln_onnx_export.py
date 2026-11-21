"""从真实保存模型导出 ONNX 包，并在指定独立解释器运行两种输入核对。"""

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
from common.kiln_onnx_export import zip_onnx_files
from db.models.kiln_studio import KilnStudioAsset
from db.session import AsyncSessionLocal, engine


async def verify(args):
    engine.echo = False
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(KilnStudioAsset).where(
                    KilnStudioAsset.public_id == args.bundle_id, KilnStudioAsset.kind == "bundle"
                )
            )
        ).scalar_one()
        package = await export_bundle(db, row.user_id, row.public_id, "onnx")
    args.output.mkdir(parents=True, exist_ok=True)
    filename = f"kiln-model-{package['version']}.onnx.zip"
    blob = zip_onnx_files(package)
    (args.output / filename).write_bytes(blob)
    folder = args.output / f"kiln-model-{package['version']}-onnx"
    with ZipFile(BytesIO(blob)) as archive:
        archive.extractall(folder)
    checks = {}
    for name in ("input_example.json", "sensor_input_example.json"):
        result = subprocess.run(
            [
                str(args.python),
                "-I",
                str((folder / "run.py").resolve()),
                "--input",
                str((folder / name).resolve()),
                "--verify",
            ],
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        checks[name] = json.loads(result.stdout)
    report = {
        "bundleId": args.bundle_id,
        "version": package["version"],
        "filename": filename,
        "bytes": len(blob),
        "python": str(args.python),
        "checks": checks,
        "validation": package["validation"],
    }
    (args.output / "onnx-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    try:
        await verify(parser.parse_args())
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
