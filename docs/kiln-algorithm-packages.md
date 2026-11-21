# Python 算法包接入

管理员可在「算法管理」上传 ZIP。直接上传会创建一个待验证版本，已有登记可以用「为此记录上传算法包」。上传会固定代码摘要、包版本和执行镜像 ID。选择数据版本和与之匹配的机理版本，填写预测任务、窗口及算法参数，运行验证。核对留出集误差和覆盖率后填写发布说明，再到窑炉建模的算法库点击「刷新已发布算法」。

「下载示例包」提供可训练的温度趋势回归及清单。示例用于练习接入流程，发布业务模型前请在适用现场数据上评估。

## 部署执行环境

在运行窑炉 API 的机器上准备 Linux Docker 引擎，在仓库根目录构建执行镜像。

```powershell
docker build -f deploy/kiln-algorithm-runner.Dockerfile -t zhongji-kiln-algorithm-runner:1 workers
```

设置 `KILN_ALGORITHM_RUNNER_IMAGE=zhongji-kiln-algorithm-runner:1` 后重启 API，部署更新后的前端。API 进程须能通过 `docker` CLI 访问部署侧的引擎。使用独立执行主机时，由部署方配置 API 的 Docker 客户端连接和凭据。历史模型复现依赖其对应 ZIP、存储及镜像 ID，请一并保留，以便回放或备份恢复。

执行容器使用独立网络空间、非 root 用户和只读根文件系统，无宿主机数据挂载或应用凭据。单次执行限制为 1 CPU、512 MiB 内存、32 个进程和约 100 秒。运行时通过标准输入接收当次所需数据，通过有大小限制的 JSON 返回权重或预测。执行镜像预装 Python 标准库和 NumPy。其他依赖由部署方准备在受控执行镜像中，上传包内的依赖声明不会触发安装。

## ZIP 格式

ZIP 根目录放 `algorithm.json`、`algorithm.py` 和按需提供的同级 Python 模块。最多 24 个文件，压缩前后均限 1 MB，文本用 UTF-8。

```json
{
  "apiVersion": 1,
  "name": "我的窑温模型",
  "version": "1.0.0",
  "description": "从最近窗口预测指定时距的窑温",
  "tasks": ["temperature"],
  "options": {"alpha": 0.5}
}
```

`tasks` 可以声明温度 `temperature`、燃气流量 `energy` 和空燃比 `afr`。`options` 可省略，支持小数、整数、字符串及布尔值。页面按默认值类型生成参数表单。每次实验都可设置训练时距。

`algorithm.py` 提供以下入口。

- `fit(x, y, config, parameters, train_rows)` 接收仅在训练窗口拟合过标准化的 NumPy 特征 `x`、对应训练标签 `y`、训练设置、机理参数和训练输入。返回可保存的 JSON 字典或数组，NumPy 数组和标量需转换为标准 JSON 类型。
- `predict(artifact, example)` 接收包含 `model`、`preprocessor`、`config`、`parameters` 和 `features` 的模型对象与一条原始预测输入，返回一个有限数值，无法计算时返回 `None`。以标准化特征训练的模型应使用 `artifact["preprocessor"]` 的 `mean` 和 `scale` 在推理时做同样变换。
- `predict_many(artifact, rows)` 可选，返回与输入等长的预测序列。验证会核对它与逐条 `predict` 的结果。

`config` 是字典，含 `task`、`zone`、`historyMinutes`、`horizonMinutes` 和 `options`。`example` 与 `train_rows` 的元素含原始 `x`、`y0`、`sp0`、`sp1`、`slope`。原始 `x` 按平台统一的 12 个特征排列，缺测以 `null` 表示。训练标签、留出集标签和用于评分的数据分别由平台处理，`predict` 与 `predict_many` 的输入仅包含预测起点可用的观测与已知计划。

当前协议支持平台内定时距训练、回测与预测。逐分钟输出、独立 ONNX 导出和离线 Python 包不属于该协议。已发布版本需要变更时，上传新版本并重新验证。停用旧发布后，历史模型仍可用其固定包和镜像进行推理。
