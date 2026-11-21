"""Versioned uploaded source packages and the isolated fit/predict protocol."""

import hashlib
import json
import math
import re
import stat
import subprocess
from io import BytesIO
from pathlib import Path
from threading import BoundedSemaphore, Thread
from typing import Literal
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from pydantic import ConfigDict, Field, create_model, model_validator

from common.config import get_settings
from common.kiln_studio_schema import AlgorithmMetadata, AlgorithmPackageRef, Strict

MAX_PACKAGE = 1_000_000
MAX_REQUEST = 32_000_000
MAX_REPLY = 4_000_000
BATCH_SIZE = 2048
_SAFE_SOURCE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,59}\.py")
_OPTIONS_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,39}")
_SLOTS = BoundedSemaphore(2)


class PackageManifest(Strict):
    apiVersion: Literal[1]
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.+_-]{0,79}$")
    description: str = Field(min_length=1, max_length=1500)
    tasks: list[Literal["temperature", "energy", "afr"]] = Field(min_length=1, max_length=3)
    options: dict[str, str | bool | int | float] = Field(default_factory=dict, max_length=20)

    @model_validator(mode="after")
    def validate_contract(self):
        self.name = self.name.strip()
        self.description = self.description.strip()
        if not self.name or not self.description or len(set(self.tasks)) != len(self.tasks):
            raise ValueError("请填写名称、说明和不重复的预测任务")
        if any(
            not _OPTIONS_NAME.fullmatch(name)
            or hasattr(Strict, name)
            or name.startswith("model_")
            or (isinstance(value, str) and len(value) > 500)
            or (isinstance(value, float) and not math.isfinite(value))
            for name, value in self.options.items()
        ):
            raise ValueError("参数名须为字母起头的标识符，默认值须为有限数值、布尔值或简短字符串")
        return self


class _Options(Strict):
    model_config = ConfigDict(strict=True)


def read_package(blob):
    if len(blob) > MAX_PACKAGE:
        raise ValueError("算法 ZIP 大小上限为 1 MB")
    try:
        with ZipFile(BytesIO(blob)) as archive:
            entries = archive.infolist()
            if not 2 <= len(entries) <= 24 or sum(info.file_size for info in entries) > MAX_PACKAGE:
                raise ValueError("算法包解压后最多 24 个文件、合计 1 MB")
            files = {}
            for info in entries:
                name = info.filename
                if (
                    info.orig_filename != name
                    or name.casefold() in files
                    or info.flag_bits & 1
                    or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG)
                    or (name != "algorithm.json" and not _SAFE_SOURCE.fullmatch(name))
                ):
                    raise ValueError(
                        "ZIP 根目录只接受普通 .py 文件和 algorithm.json，文件名不得重复"
                    )
                with archive.open(info) as entry:
                    content = entry.read(info.file_size + 1)
                if len(content) != info.file_size:
                    raise ValueError("算法包文件大小与 ZIP 目录不一致")
                files[name.casefold()] = (name, content.decode("utf-8"))
    except (BadZipFile, UnicodeError, RuntimeError, OSError, NotImplementedError) as exc:
        raise ValueError("无法读取算法 ZIP，请检查是否为未加密的 UTF-8 源代码包") from exc
    sources = dict(files.values())
    if "algorithm.json" not in sources or "algorithm.py" not in sources:
        raise ValueError("ZIP 根目录必须包含 algorithm.json 和 algorithm.py")
    manifest = PackageManifest.model_validate_json(sources.pop("algorithm.json"))
    return manifest, sources


def parameter_model(package):
    if not package.options:
        return None
    return create_model(
        "AlgorithmPackageOptions",
        __base__=_Options,
        **{key: (type(value), value) for key, value in package.options.items()},
    )


def configured_image():
    image = get_settings().kiln_algorithm_runner_image.strip()
    if not image:
        raise ValueError("算法执行环境未配置，请联系管理员设置 KILN_ALGORITHM_RUNNER_IMAGE")
    try:
        inspected = (
            subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Os}} {{.Id}}", image],
                capture_output=True,
                timeout=10,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("无法访问 Docker 算法执行镜像，请检查镜像和 Docker 服务") from exc
    if not re.fullmatch(r"linux sha256:[0-9a-f]{64}", inspected):
        raise ValueError("算法执行镜像必须是本地 Linux 镜像")
    return inspected.removeprefix("linux ")


def _path(digest):
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("无效的算法包标识")
    root = Path(get_settings().storage_root).resolve() / "kiln-algorithm-packages"
    return root / f"{digest}.zip"


def save_definition(blob, key):
    manifest, _ = read_package(blob)
    digest = hashlib.sha256(blob).hexdigest()
    package = AlgorithmPackageRef(
        sha256=digest,
        image=configured_image(),
        tasks=manifest.tasks,
        options=manifest.options,
    )
    path = _path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(blob)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    model = parameter_model(package)
    unavailable = {"status": "unsupported", "detail": "当前上传包协议只提供平台内定时距预测。"}
    return AlgorithmMetadata(
        key=key,
        name=manifest.name,
        category="上传算法包",
        oneLine=manifest.description,
        strengths=[],
        benefits=[],
        fitScenarios=[],
        limitations=["仅输出训练时距的预测值"],
        dataRequirements=["平台统一的历史窗口和预测任务标签"],
        interpretability="medium",
        sampleSizeRisk="medium",
        interventionCapable=False,
        status="enabled",
        version=manifest.version,
        formula="由上传算法定义",
        explanation=manifest.description,
        mechanismRole="由训练回调使用可用机理参数",
        package=package,
        sourceKind="package",
        parameterSchema=model.model_json_schema() if model else None,
        capabilities={
            "trainingMode": "trainable",
            "trainingDetail": "在独立容器内拟合并保存 JSON 权重。",
            "supportedTasks": manifest.tasks,
            "inference": {"status": "supported", "detail": "由固定版本的算法包提供推理。"},
            "minuteForecast": unavailable,
            "onnxExport": unavailable,
            "pythonExport": unavailable,
        },
    )


def read_verified(package):
    try:
        with _path(package.sha256).open("rb") as archive:
            blob = archive.read(MAX_PACKAGE + 1)
    except OSError as exc:
        raise ValueError("已绑定算法包不可用") from exc
    if len(blob) > MAX_PACKAGE or hashlib.sha256(blob).hexdigest() != package.sha256:
        raise ValueError("算法包内容与已绑定指纹不一致")
    return blob


def _execute(image, payload):
    name = "zhongji-kiln-" + uuid4().hex
    command = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--init",
        "--pull=never",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--user=65534:65534",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=32",
        "--cpus=1",
        "--memory=512m",
        "--memory-swap=512m",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16m",
        "--log-driver=none",
        image,
    ]
    with _SLOTS:
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            raise ValueError("无法调用 Docker 算法执行环境") from exc
        reply = b""

        def send():
            try:
                with process.stdin as stream:
                    stream.write(payload)
            except OSError:
                pass  # Early container exit is reported by the main thread.

        def receive():
            nonlocal reply
            with process.stdout as stream:
                reply = stream.read(MAX_REPLY + 1)
            if len(reply) > MAX_REPLY and process.poll() is None:
                process.kill()

        Thread(target=send, daemon=True).start()
        reader = Thread(target=receive, daemon=True)
        reader.start()
        try:
            status = process.wait(timeout=110)
            reader.join(timeout=3)
            if reader.is_alive() or len(reply) > MAX_REPLY:
                raise ValueError("算法输出超出大小限制或未关闭输出")
            if status:
                raise ValueError("算法容器运行失败，请检查镜像、内存限制或执行时限")
        except subprocess.TimeoutExpired as exc:
            raise ValueError("算法容器超过执行时限") from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.returncode or reader.is_alive() or len(reply) > MAX_REPLY:
                try:
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=8)
                except (OSError, subprocess.SubprocessError):
                    pass  # The independent container deadline still applies.
    try:
        response = json.loads(reply)
    except (ValueError, RecursionError) as exc:
        raise ValueError("算法容器没有返回有效 JSON") from exc
    if isinstance(response, dict) and isinstance(response.get("error"), str):
        raise ValueError(f"算法包执行失败：{response['error'][:350]}")
    if not isinstance(response, dict) or response.keys() != {"value"}:
        raise ValueError("算法容器返回了未知响应")
    return response["value"]


def _runtime(package, operation, body):
    if not get_settings().kiln_algorithm_runner_image.strip():
        raise ValueError("算法执行环境未配置")
    _, sources = read_package(read_verified(package))
    payload = json.dumps(
        {"sources": sources, "operation": operation, "body": body}, allow_nan=False
    ).encode()
    if len(payload) > MAX_REQUEST:
        raise ValueError("算法任务输入超过 32 MB，请缩小数据范围")
    return _execute(package.image, payload)


def _input(row):
    return {key: row[key] for key in ("x", "y0", "sp0", "sp1", "slope") if key in row}


def _config(config, key):
    return {
        **{name: config[name] for name in ("task", "zone", "historyMinutes", "horizonMinutes")},
        "options": config.get("algorithmOptions", {}).get(key, {}),
    }


def fit_package(definition, x, y, config, parameters, train):
    weights = _runtime(
        definition.package,
        "fit",
        {
            "x": x.tolist(),
            "y": y.tolist(),
            "config": _config(config.model_dump(), definition.key),
            "parameters": parameters,
            "train_rows": [_input(row) for row in train],
        },
    )
    if not isinstance(weights, dict | list):
        raise ValueError("算法 fit 须返回可保存的 JSON 字典或数组")
    return weights


def predict_package(artifact, rows, *, each=False):
    package = AlgorithmPackageRef.model_validate(artifact["algorithmPackage"])
    task = artifact["config"]["task"]
    if task not in package.tasks:
        raise ValueError("算法包不支持当前预测任务")
    context = {
        **{key: artifact[key] for key in ("model", "preprocessor", "parameters", "features")},
        "config": _config(artifact["config"], artifact["algorithm"]),
    }
    nonnegative = task in {"energy", "afr"}
    output = []
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        values = _runtime(
            package,
            "predict_each" if each else "predict_many",
            {
                "artifact": context,
                "rows": [_input(row) for row in batch],
            },
        )
        if not isinstance(values, list) or len(values) != len(batch):
            raise ValueError("算法返回的预测数量与输入不一致")
        try:
            numbers = [float(value) if type(value) in (float, int) else value for value in values]
        except OverflowError as exc:
            raise ValueError("算法预测值超出浮点数范围") from exc
        if any(
            value is not None and (type(value) is not float or not math.isfinite(value))
            for value in numbers
        ):
            raise ValueError("算法须为每条输入返回一个有限数值或 null")
        output.extend(
            max(0.0, value) if value is not None and nonnegative else value for value in numbers
        )
    return output
