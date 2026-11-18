"""小型 LSTM 的训练及可序列化 CPU 推理，全部预处理只拟合训练集。"""

import json
from functools import lru_cache

import numpy as np

SEQUENCE_LENGTH = 16
HIDDEN_SIZE = 8


def history_sequence(window, target):
    indices = np.linspace(0, len(window) - 1, SEQUENCE_LENGTH).astype(int)
    keys = (target, "temp", "temp_sp", "gas", "air", "o2", "pressure")
    return [[window[index].get(key) for key in keys] for index in indices]


def _sigmoid(value):
    return 1 / (1 + np.exp(-np.clip(value, -40, 40)))


def _forward(sequence, context, weights, *, remember=False):
    hidden = np.zeros((len(sequence), HIDDEN_SIZE))
    cell = np.zeros_like(hidden)
    steps = []
    for index in range(sequence.shape[1]):
        features = sequence[:, index]
        gates = features @ weights["input"] + hidden @ weights["recurrent"] + weights["bias"]
        forget, incoming, outgoing, proposal = np.split(gates, 4, axis=1)
        forget, incoming, outgoing = map(_sigmoid, (forget, incoming, outgoing))
        proposal = np.tanh(proposal)
        previous_cell, previous_hidden = cell, hidden
        cell = forget * cell + incoming * proposal
        hidden = outgoing * np.tanh(cell)
        if remember:
            steps.append(
                (
                    features,
                    previous_hidden,
                    previous_cell,
                    forget,
                    incoming,
                    outgoing,
                    proposal,
                    cell,
                )
            )
    output = hidden @ weights["output"] + context @ weights["context"] + weights["offset"]
    return output, hidden, steps


def _sequences(rows):
    values = [row.get("sequence") for row in rows]
    if any(value is None for value in values):
        raise ValueError("LSTM 缺少历史序列，请使用该算法重新生成实验")
    return np.asarray(values, dtype=float)


def fit_lstm(context, targets, config, parameters, train):
    # 按时间均匀取样控制交互式演示的训练开销，保留实际采用的样本数。
    indices = np.linspace(0, len(train) - 1, min(len(train), 2048)).astype(int)
    sequence = _sequences([train[index] for index in indices])
    flattened = sequence.reshape(-1, sequence.shape[-1])
    mean = np.array(
        [
            float(np.mean(column[np.isfinite(column)])) if np.isfinite(column).any() else 0
            for column in flattened.T
        ]
    )
    filled = np.where(np.isfinite(flattened), flattened, mean)
    scale = filled.std(axis=0)
    scale[scale < 1e-6] = 1
    sequence = np.clip((np.where(np.isfinite(sequence), sequence, mean) - mean) / scale, -10, 10)
    context = np.clip(context[indices], -10, 10)
    origins = np.asarray([train[index]["y0"] for index in indices])[:, None]
    labels = np.asarray(
        [train[index].get("trajectoryTargets", [targets[index]]) for index in indices], dtype=float
    )
    valid = np.isfinite(labels)
    supported = valid.any(axis=0)
    offsets = [index + 1 for index in range(labels.shape[1]) if supported[index]]
    labels, valid = labels[:, supported], valid[:, supported]
    residual = labels - origins
    target_mean = float(residual[valid].mean())
    target_scale = max(float(residual[valid].std()), 1)
    target = np.where(valid, (residual - target_mean) / target_scale, 0)
    outputs = labels.shape[1]
    random = np.random.default_rng(42)
    weights = {
        "input": random.normal(0, 0.1, (sequence.shape[-1], 4 * HIDDEN_SIZE)),
        "recurrent": random.normal(0, 0.1, (HIDDEN_SIZE, 4 * HIDDEN_SIZE)),
        "bias": np.r_[np.ones(HIDDEN_SIZE), np.zeros(3 * HIDDEN_SIZE)],
        "output": random.normal(0, 0.1, (HIDDEN_SIZE, outputs)),
        "context": np.zeros((context.shape[-1], outputs)),
        "offset": np.zeros(outputs),
    }
    first_moment = {key: np.zeros_like(value) for key, value in weights.items()}
    second_moment = {key: np.zeros_like(value) for key, value in weights.items()}
    iteration = 0
    epochs = 24
    for _epoch in range(epochs):
        order = random.permutation(len(target))
        for first in range(0, len(order), 128):
            batch = order[first : first + 128]
            predicted, hidden, steps = _forward(
                sequence[batch], context[batch], weights, remember=True
            )
            output_gradient = (
                2 * (predicted - target[batch]) * valid[batch] / max(1, valid[batch].sum())
            )
            gradients = {key: np.zeros_like(value) for key, value in weights.items()}
            gradients["output"] = hidden.T @ output_gradient
            gradients["context"] = context[batch].T @ output_gradient
            gradients["offset"] = output_gradient.sum(axis=0)
            hidden_gradient = output_gradient @ weights["output"].T
            cell_gradient = np.zeros_like(hidden_gradient)
            for (
                features,
                old_hidden,
                old_cell,
                forget,
                incoming,
                outgoing,
                proposal,
                cell,
            ) in reversed(steps):
                tanh_cell = np.tanh(cell)
                outgoing_gradient = hidden_gradient * tanh_cell
                cell_gradient += hidden_gradient * outgoing * (1 - tanh_cell**2)
                forget_gradient = cell_gradient * old_cell
                incoming_gradient = cell_gradient * proposal
                proposal_gradient = cell_gradient * incoming
                gate_gradient = np.concatenate(
                    [
                        forget_gradient * forget * (1 - forget),
                        incoming_gradient * incoming * (1 - incoming),
                        outgoing_gradient * outgoing * (1 - outgoing),
                        proposal_gradient * (1 - proposal**2),
                    ],
                    axis=1,
                )
                gradients["input"] += features.T @ gate_gradient
                gradients["recurrent"] += old_hidden.T @ gate_gradient
                gradients["bias"] += gate_gradient.sum(axis=0)
                hidden_gradient = gate_gradient @ weights["recurrent"].T
                cell_gradient *= forget
            iteration += 1
            norm = np.sqrt(sum(float(np.sum(value**2)) for value in gradients.values()))
            for key, gradient in gradients.items():
                gradient = gradient * min(1, 5 / max(norm, 1e-12))
                first_moment[key] = 0.9 * first_moment[key] + 0.1 * gradient
                second_moment[key] = 0.999 * second_moment[key] + 0.001 * gradient**2
                corrected = first_moment[key] / (1 - 0.9**iteration)
                variance = second_moment[key] / (1 - 0.999**iteration)
                weights[key] -= 0.01 * corrected / (np.sqrt(variance) + 1e-8)
    return {
        "weights": {key: value.tolist() for key, value in weights.items()},
        "sequenceMean": mean.tolist(),
        "sequenceScale": scale.tolist(),
        "targetMean": target_mean,
        "targetScale": target_scale,
        "sequenceLength": SEQUENCE_LENGTH,
        "hiddenSize": HIDDEN_SIZE,
        "trainingCount": len(indices),
        "availableTrainingCount": len(train),
        "epochs": epochs,
        "offsets": offsets,
    }


@lru_cache(maxsize=8)
def _weights(encoded):
    weights = {key: np.asarray(value) for key, value in json.loads(encoded).items()}
    weights["output"] = weights["output"].reshape(HIDDEN_SIZE, -1)
    weights["context"] = weights["context"].reshape(12, -1)
    return weights


def _predict_values(artifact, examples):
    sequence = _sequences(examples)
    model = artifact["model"]
    mean, scale = np.asarray(model["sequenceMean"]), np.asarray(model["sequenceScale"])
    sequence = np.clip((np.where(np.isfinite(sequence), sequence, mean) - mean) / scale, -10, 10)
    prep = artifact["preprocessor"]
    context = np.asarray([example["x"] for example in examples], dtype=float)
    context = np.where(np.isfinite(context), context, np.asarray(prep["mean"]))
    context = np.clip((context - prep["mean"]) / prep["scale"], -10, 10)
    weights = _weights(json.dumps(model["weights"], sort_keys=True))
    prediction, _, _ = _forward(sequence, context, weights)
    origins = np.asarray([example["y0"] for example in examples])[:, None]
    return origins + model["targetMean"] + model["targetScale"] * prediction


def predict_lstm_path(artifact, example):
    prediction = _predict_values(artifact, [example])[0]
    model = artifact["model"]
    offsets = model.get("offsets", [artifact["config"]["horizonMinutes"]])
    return {offset: float(value) for offset, value in zip(offsets, prediction, strict=True)}


def predict_lstm(artifact, example):
    return predict_lstm_path(artifact, example).get(artifact["config"]["horizonMinutes"])


def predict_lstm_many(artifact, examples):
    """Bound memory while evaluating the same saved weights over a holdout set."""
    horizon = artifact["config"]["horizonMinutes"]
    offsets = artifact["model"].get("offsets", [horizon])
    if horizon not in offsets:
        return [None] * len(examples)
    column = offsets.index(horizon)
    values = []
    for first in range(0, len(examples), 256):
        prediction = _predict_values(artifact, examples[first : first + 256])
        values.extend(float(value) for value in prediction[:, column])
    return values
