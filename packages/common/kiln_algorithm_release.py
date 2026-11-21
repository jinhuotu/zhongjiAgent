"""受控实现的版本指纹；管理页面只能绑定注册表中的实现。"""

import hashlib
import inspect
import json
import marshal
import platform
from pathlib import Path

import numpy as np
from pydantic import Field, model_validator

from common.kiln_studio_schema import AlgorithmKey, ExperimentRequest, Strict


class AlgorithmDraftRequest(Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1500)
    requestedKey: AlgorithmKey | None = None

    @model_validator(mode="after")
    def nonblank_name(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("请填写算法名称")
        return self


class AlgorithmBindRequest(Strict):
    implementationKey: AlgorithmKey
    version: str = Field(min_length=1, max_length=80)


class AlgorithmValidationRequest(Strict):
    projectId: str = Field(min_length=1, max_length=32)
    config: ExperimentRequest


class AlgorithmPublishRequest(Strict):
    validationId: str = Field(min_length=1, max_length=32)
    notes: str = Field(min_length=1, max_length=1500)

    @model_validator(mode="after")
    def require_review(self):
        if not self.notes.strip():
            raise ValueError("请填写发布说明，确认适用范围及验证结果")
        return self


class AlgorithmRetireRequest(Strict):
    reason: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def require_reason(self):
        if not self.reason.strip():
            raise ValueError("请填写停用原因")
        return self


def builtin_keys():
    from common.kiln_studio_engine import ALGORITHMS

    return {item.key for item in ALGORITHMS}


def implementation_fingerprint(definition):
    """绑定回调代码、所在模块、参数契约和运行版本；不是第三方代码安全签名。"""
    if definition.package:
        from common.kiln_algorithm_packages import read_verified

        read_verified(definition.package)
        contract = definition.model_dump(mode="json", exclude={"capabilities", "parameterSchema"})
        return hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    from common.kiln_studio_engine import algorithm_implementation

    impl = algorithm_implementation(definition.key)
    if impl is None:
        raise ValueError("算法实现未部署，请先由研发注册训练和推理实现")
    digests = []
    for callback in (
        impl.fit,
        impl.predict,
        impl.predict_many,
        impl.predict_path,
        impl.unavailable,
    ):
        if callback is None:
            continue
        code = getattr(callback, "__code__", None)
        module = inspect.getmodule(callback)
        file = getattr(module, "__file__", None)
        if code is None or not file or not Path(file).is_file():
            raise ValueError("当前回调无法生成可核对的受控实现指纹")
        digests.append(hashlib.sha256(marshal.dumps(code) + Path(file).read_bytes()).hexdigest())
    contract = {
        "definition": definition.model_dump(
            exclude={"capabilities", "sourceKind", "parameterSchema", "package"}
        ),
        "callbacks": digests,
        "tasks": impl.tasks,
        "sequence": impl.sequence,
        "parameters": impl.parameter_model.model_json_schema() if impl.parameter_model else None,
        "python": platform.python_version(),
        "numpy": np.__version__,
    }
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
