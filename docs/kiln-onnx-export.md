# 窑炉通用 ONNX 交付

## 实现文件

- `packages/common/kiln_onnx_graph.py`：标准图构建及各算法转换。
- `packages/common/kiln_onnx_examples.py`：真实历史窗口与平台核对值。
- `packages/common/kiln_onnx_preprocess.py`：可独立交付的历史清洗和输入构造。
- `packages/common/kiln_onnx_runtime.py`：独立运行程序模板。
- `packages/common/kiln_onnx_export.py`：包清单、逐点核对、报告及 ZIP。
- `apps/api/routers/kiln_studio.py`、`apps/api/services/kiln_studio.py`：能力检查与下载接口。
- `pyproject.toml`、`poetry.lock`：可选构图依赖。
- `tests/test_kiln_onnx_export.py`：新转换、独立运行、历史规则、旧模型与失败拦截测试。
- `tests/test_kiln_model_export.py`：原导出格式验证更新，保留原 JSON/Python 验收。
- `scripts/verify_kiln_onnx_export.py`：实际模型离线验收工具。
- 前端仓库 `src/config/package-formats.ts`、`src/lib/model-package-api.ts`、
  `src/views/ModelPackageView.vue`、`src/views/KilnStudioView.vue`、`tests/model-package.test.mjs`：
  配置、鉴权下载、能力状态、第五步入口提示及配置回归。

## 安装与接口

构图依赖为可选项，已锁定 ONNX 1.17.0；不引入 PyTorch/sklearn/TensorRT。
项目安装时选择 `onnx-export` extra，或在现有环境安装 `onnx>=1.16,<1.18`。
测试仍使用项目 `venv/Scripts/python.exe -m pytest`，不创建 Poetry 环境。

- `GET /api/v1/kiln-studio/bundles/{id}/export`：原 JSON，契约不变。
- `GET .../export?format=onnx`：带 base64 二进制条目的文件清单及真实核对报告。
- `GET .../export/onnx.zip`：已核对的完整部署包。
- `GET .../export/capabilities`：模型兼容性和服务端依赖能力。
- 原 `.../export/python.zip` 保持可用。

上述接口仍按当前用户查询 bundle 和绑定 dataset；不新增表或回写模型。
非法格式和模型转换/核对失败返回 422；构图依赖缺失返回 503。

## 转换边界

基础算子、FP32、opset 13、IR 8，每个子模型一个独立 ONNX 文件。
支持 baseline/physics/ridge/lasso/boosting/fusion/lstm，以及以这些实现为基础的保存预设。
外部导入的 ONNX 不经过当前内置权重转换器，保留 JSON/Skill 原有导出。

LSTM 按保存的门序和全部 offsets 输出，不重新训练或插值。
Ridge/Fusion 仅在具有完整分钟预测头时生成完整曲线，其余只输出原生预测时距。
历史序列固定长度，批次动态；预处理标准化、缺测填充和输出还原在图内。
历史清洗与窗口构造在独立 preprocess.py 中，测试对齐平台 clean_rows/make_features。

生成时从绑定数据中最多选六个窑次、每次最多四个分散窗口，逐时距比较平台预测与 ORT CPU。
同时核对历史预处理和动态批次；默认数值容差 atol=0.001、rtol=0.00001，记录每时距最大差异。
这是浮点转换一致性，不是业务准确率；验证报告记录实际起点、环境、Provider 和样本数。
不通过核对时不返回有效下载包。

## 包与验证

模型在 models/；manifest.json 给出真实形状、量纲、时距、版本和 SHA256。
run.py 只依赖 NumPy、ONNX Runtime；运行无需 ONNX 构图库或平台服务。
README、两种输入示例和 validation.json 一并交付。按 README 执行 `python run.py --verify`，
或 `python run.py --input sensor_input_example.json --verify`。可用 `--benchmark 100` 测本机已加载 CPU 会话。

本机验证命令（PowerShell）：

```powershell
$env:PYTHONPATH='apps;packages'
.\venv\Scripts\python.exe scripts/verify_kiln_onnx_export.py --bundle-id <真实ID> --output runs/onnx-check
```

本次没有量化模型、TensorRT 引擎、现场数据连接或设备控制能力。
不同 OS/CPU 的运行时需分别安装兼容版本；目标设备、性能及业务误差门限仍需客户硬件确定后验证。
