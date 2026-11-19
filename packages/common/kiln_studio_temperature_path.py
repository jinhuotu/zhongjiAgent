"""Direct minute heads trained on the same training split as the endpoint model."""

import math

import numpy as np


def horizon_features(sample, setpoint, minutes):
    slope = (
        (setpoint - sample["sp0"]) * 60 / minutes
        if setpoint is not None and sample["sp0"] is not None
        else None
    )
    features = list(sample["x"])
    features[10:12] = [setpoint, slope]
    return {**sample, "x": features, "sp1": setpoint, "slope": slope}


def fit_temperature_heads(train, config, parameters, implementation):
    from common.kiln_studio_engine import _fit_fusion, fit_preprocessor, fit_ridge, transform

    if config.task != "temperature" or implementation not in {"ridge", "fusion"}:
        return {}
    heads = {}
    for minute in range(1, config.horizonMinutes):
        selected = [
            sample
            for sample in train
            if sample.get("trajectoryTargets")
            and sample["trajectoryTargets"][minute - 1] is not None
            and (implementation != "fusion" or sample["trajectoryPlans"][minute - 1] is not None)
        ]
        if len(selected) < 30:
            continue
        rows = [
            horizon_features(sample, sample["trajectoryPlans"][minute - 1], minute)
            for sample in selected
        ]
        prep = fit_preprocessor([row["x"] for row in rows])
        x = transform([row["x"] for row in rows], prep)
        y = np.array([row["trajectoryTargets"][minute - 1] for row in rows])
        model = (
            _fit_fusion(
                x, y, config.model_copy(update={"horizonMinutes": minute}), parameters, rows
            )
            if implementation == "fusion"
            else fit_ridge(x, y, config.ridgeAlpha)
        )
        heads[str(minute)] = {"model": model, "preprocessor": prep, "trainingCount": len(rows)}
    # A partial set must not advertise a continuous minute trajectory.
    return heads if len(heads) == config.horizonMinutes - 1 else {}


def predict_temperature_head(artifact, sample, minute):
    from common.kiln_studio_engine import predict_model

    if minute == artifact["config"]["horizonMinutes"]:
        return predict_model(artifact, sample)
    head = artifact.get("temperatureHeads", {}).get(str(minute))
    if head is None:
        return None
    model = {
        **artifact,
        **head,
        "config": {**artifact["config"], "horizonMinutes": minute},
    }
    value = predict_model(model, sample)
    return value if value is not None and math.isfinite(value) else None
