# 窑炉算法接入指南

管理员自助上传可训练 Python ZIP 的接入流程见 [Python 算法包接入](kiln-algorithm-packages.md)。下文的模块注册适用于和项目代码一同部署的受控实现。

当前没有新增生产算法或示例卡片。已有内置算法保持可用；新注册实现须在“算法管理”绑定、验证并发布后，才进入当前账号的业务算法库。

## 三条接入路径

- 参数方案：页面“新建参数方案”，调整现有 Ridge/Boosting 的真实训练参数，不能创建新算法类型。
- 外部已训练模型：页面“导入已训练模型”，上传符合现有契约的 ONNX，检查图、输入输出、大小并试跑后保存；平台只评估，不重新训练。
- 新的可训练算法：研发实现训练/推理函数及 Pydantic 参数契约，通过受控模块注册、部署并回归；管理员在“算法管理”完成绑定、真实验证及发布。页面自动显示元数据和参数表单，无需新增页面分支。

## 注册一个新实现

1. 在 `packages/common/` 新建实现模块，避免依赖 Web 请求、数据库会话或上传目录。
2. 模块提供 `register()`，在其中调用 `common.kiln_studio_engine.register_algorithm(definition, implementation, onnx_exporter=None)`。
3. `definition` 使用 `AlgorithmMetadata`，填写真实名称、分类、说明、数据要求、局限、公式、适用工况、状态和版本。所有必填字段见 schema。独立实现不得设置 `baseKey` 或 `external`。键格式 `[a-z][a-z0-9_]{0,39}`；不得重复或使用 `custom_`、回测字段等保留键。
4. `implementation` 使用公开的 `AlgorithmImplementation`。接口如下；未实现的可选能力不注册。
5. 把模块完整路径加入 `packages/common/kiln_studio_plugins.py` 的 `ALGORITHM_MODULES`。仅加载受控 `common.*` 模块，不扫描文件、不执行用户上传的 Python。
6. 运行下述测试和实际业务样本验收，部署后重启 API 与实际执行实验的工作进程。它们必须加载同一版本代码。
7. 管理员打开 `/algorithm-manage` 登记算法，选择已部署的新实现绑定版本，使用真实项目/数据/机理运行验证。确认适用范围和实际效果后填写说明发布，工作台刷新算法列表即可使用。详见 `docs/kiln-algorithm-management.md`。

### 回调契约

```python
AlgorithmImplementation(
    predict=predict,              # 必填：(artifact, example) -> float | None
    fit=fit,                      # 可选：(x, y, config, parameters, train_rows) -> JSON 权重
    unavailable=check,            # 可选：(task, template_keys, missing_plan) -> 不可用原因或 ""
    parameter_model=Parameters,   # 可选：Pydantic 参数模型
    tasks=("temperature",),       # 实际支持 temperature/energy/afr 中的哪些任务
    sequence=False,               # 需要当前统一历史序列输入时设为 True
    predict_many=batch_predict,   # 可选：(artifact, rows) -> 与 rows 等长的数值/None 列表
    predict_path=path_predict,     # 可选：(artifact, example) -> {分钟偏移: 真实预测值}
)
```

`fit` 只接收隔离后的训练窗口。`x` 是按训练集缺测填充和标准化后的二维 NumPy 数组，`y` 是对应真实标签；`train_rows` 是相同顺序的原始训练样本，包含 `x/y/y0/sp0/sp1/slope/originTime/cycle` 等字段。验证集和测试集不传给 `fit`。不得在实现内重新读完整历史文件获取未来实测。

`predict` 接收保存的完整 artifact 和原始 example，不接收自动标准化后的 x。若训练使用标准化特征，推理应使用 `transform([example["x"]], artifact["preprocessor"])`。权重在 `artifact["model"]`，任务/时距/实际参数在 `artifact["config"]`，机理参数在 `artifact["parameters"]`。

现有特征为 `engine.FEATURES` 的 12 个有序字段，包含起点观测和已知未来设定；不可用未来实测代替计划。能量/空燃比输出统一限制为非负；缺测输出 None，不用插值冒充模型计算。

`sequence=True` 使用现有历史窗口等距抽取的 **16×7** 原始序列，通道依次为目标值、均温、温度设定、燃气、空气、氧含量、窑压。接入者负责仅用训练集拟合序列预处理并保存。导出调用规格需要在 JSON 权重中保存真实 `sequenceLength=16`。该接口没有开放任意序列长度、任意特征工程或任意多输入张量。

多时距算法应把实际输出分钟列表保存为 `model["offsets"]`，按正整数升序、不重复，含原生预测时距。`predict_path` 只返回已训练或实现可计算的时距；端点应与 `predict` 对应。回放按注册回调构造曲线；未注册或未输出的分钟保持缺失，不生成虚构连续值。

### 算法参数

参数模型可继承 `common.kiln_studio_schema.Strict`，它禁止未知字段和非有限数字。当前通用编辑器支持平铺的数字、整数、布尔、字符串、枚举及范围/长度约束，不支持嵌套模型、数组或字段别名。使用 Pydantic `Field` 给出中文 title、description、真实默认值及约束。

页面从后端 `parameterSchema` 生成表单，并通过实验请求 `algorithmOptions[algorithm_key]` 提交。实现通过 `config.algorithmOptions[algorithm_key]` 读取。后端创建任务前和执行实验时均验证，不接受未选择算法的参数；应用默认值后的参数进入任务、实验和模型配置。页面修改参数会使旧实验变为待重新运行。

### 保存与历史复现

训练权重必须是可 JSON 序列化的有限数值结构。NumPy 数组需 `.tolist()`，标量需转 Python 数值；不得返回 pickle、文件句柄或平台不可读取的内存对象。注册实现没有平台独立运行能力的承诺。

新算法 artifact 保存 `algorithmVersion`，推理会核对注册版本；不同版本不静默套用。改变权重结构或推理语义时，用新键和新版本注册，保留旧键的实现处理历史模型。停用只改变可选状态，不能删除历史推理回调。既有内置算法和参数方案保留旧版本行为，本改造不迁移数据库结构。

## 能力和导出

后端 `sourceKind/capabilities` 区分内置、参数方案、外部固定模型，并从实际回调/转换器报告训练、支持任务、分钟输出、ONNX、Python 能力。条件支持仍受本次样本、产物及运行依赖约束；不能单靠改卡片标签宣称支持。

- 数值 JSON/Skill 保留真实权重与调用规格；平台推理仍需要相应实现。
- NumPy 离线模板当前只适配现有七种实现。新算法未适配模板时明确显示“未适配”，导出被拒绝。
- ONNX 可在 `register_algorithm(..., onnx_exporter=convert)` 注册转换器，签名 `(artifact) -> bytes`。转换器生成完整 ONNX 图，包含自身输入预处理和输出还原；不得重新训练。模型需遵守当前交付契约：原始 `features` FP32 动态批次、原名/顺序；时序实现另用 `sequence`，非序列多时距实现另用 `futurePlans`；输出 `predictions` 的第二维与 offsets 一致。准确输入形状及名称见 `kiln_onnx_export._spec` 和导出 manifest。平台检查图并使用真实绑定窗口核对全部输出、动态批次及历史预处理，失败不交付。设备加速和量化需另外验证。
- 外部 ONNX 导入仍是 v1：单个二维输入、每记录一个标量输出、平台 12 特征的有序子集，图中内嵌预处理。当前不接受时序多输入、多时距输出或导出 ZIP 作为算法导入文件。不能把通用 ONNX 交付包直接当成导入契约。

## 接入验收

真实新算法至少验证：受控模块启动注册；参数默认值及非法参数拒绝；仅训练集拟合；留出评估覆盖率；JSON 保存/恢复后推理一致；历史版本兼容或明确拒绝；任务限制；预测不读取未来实测。声明多时距/序列/批量或导出能力时，分别增加相应回归和真实样本核对。

自动化注册测试中的算法只存在于测试隔离注册表，不进入实际算法库。Windows PowerShell 下执行：

```powershell
# 后端，在 zhongjiAgent 下，使用现有虚拟环境
.\venv\Scripts\python.exe -m pytest tests/test_kiln_algorithm_registry.py -q
$kilnTests = @(rg --files tests | Where-Object { $_ -match 'test_kiln.*\.py$' })
.\venv\Scripts\python.exe -m pytest @kilnTests -q

# 前端，在 zhongjivueweb 下
node --experimental-strip-types --test tests/*.test.mjs
npm run build
```

上面的文件列表命令适用于 PowerShell，避免 shell 不展开 pytest 通配参数。
