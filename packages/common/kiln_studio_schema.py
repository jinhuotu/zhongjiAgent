"""Public request contracts, bounds and version references for kiln studio."""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

TemplateKey = Literal[
    "energy", "thermal", "combustion", "pressure", "recovery", "carbon", "uniformity", "stages"
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


AlgorithmKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]


class AlgorithmTuning(Strict):
    ridgeAlpha: float | None = Field(default=None, ge=0.001, le=10000)
    boostingRounds: int | None = Field(default=None, ge=5, le=120)
    learningRate: float | None = Field(default=None, ge=0.01, le=0.5)


class AlgorithmPresetRequest(Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1500)
    baseKey: Literal["ridge", "boosting"]
    tuning: AlgorithmTuning
    parentKey: AlgorithmKey | None = None

    @model_validator(mode="after")
    def validate_tuning(self):
        self.name = self.name.strip()
        expected = {"ridgeAlpha"} if self.baseKey == "ridge" else {"boostingRounds", "learningRate"}
        if not self.name or self.tuning.model_dump(exclude_none=True).keys() != expected:
            raise ValueError("请填写方案名称及基础算法所需的参数")
        return self


class AlgorithmAvailabilityRequest(Strict):
    enabled: bool


class ExternalAlgorithmSpec(Strict):
    task: Literal["temperature", "energy", "afr"]
    zone: int = Field(default=0, ge=0, le=16)
    horizonMinutes: int = Field(ge=5, le=120)
    historyMinutes: int = Field(ge=5, le=240)
    trainedUntil: AwareDatetime
    featureIndices: list[Annotated[int, Field(ge=0, le=11)]] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_feature_mapping(self):
        if len(set(self.featureIndices)) != len(self.featureIndices):
            raise ValueError("输入特征不能重复")
        if self.zone and self.task != "temperature":
            raise ValueError("仅温度预测支持指定温区")
        return self


class ExternalAlgorithmImport(ExternalAlgorithmSpec):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1500)
    probe: list[float] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_probe(self):
        self.name = self.name.strip()
        if not self.name or len(self.probe) != len(self.featureIndices):
            raise ValueError("请填写名称，试跑输入值数量必须与特征列表一致")
        return self


class ExternalAlgorithmMetadata(ExternalAlgorithmSpec):
    fileName: str
    sha256: str
    byteSize: int
    inputName: str
    outputName: str
    inputType: Literal["float32", "float64"]
    probe: list[float]
    probeOutput: float


class AlgorithmCapability(Strict):
    status: Literal["supported", "conditional", "unsupported"]
    detail: str


class AlgorithmCapabilities(Strict):
    trainingMode: Literal["trainable", "calculated", "frozen"]
    trainingDetail: str
    supportedTasks: list[Literal["temperature", "energy", "afr"]]
    inference: AlgorithmCapability
    minuteForecast: AlgorithmCapability
    onnxExport: AlgorithmCapability
    pythonExport: AlgorithmCapability


class AlgorithmPackageRef(Strict):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tasks: list[Literal["temperature", "energy", "afr"]] = Field(min_length=1, max_length=3)
    options: dict[str, str | bool | int | float] = Field(default_factory=dict)


class AlgorithmMetadata(Strict):
    key: AlgorithmKey
    name: str
    category: str
    oneLine: str
    strengths: list[str]
    benefits: list[str]
    fitScenarios: list[str]
    limitations: list[str]
    dataRequirements: list[str]
    interpretability: Literal["high", "medium", "low"]
    sampleSizeRisk: Literal["high", "medium", "low"]
    inferenceComplexity: Literal["high", "medium", "low"] | None = None
    interventionCapable: bool
    status: Literal["enabled", "experimental", "disabled"]
    version: str
    formula: str
    explanation: str
    mechanismRole: str
    baseKey: Literal["ridge", "boosting"] | None = None
    tuning: AlgorithmTuning | None = None
    parentKey: AlgorithmKey | None = None
    external: ExternalAlgorithmMetadata | None = None
    package: AlgorithmPackageRef | None = None
    sourceKind: Literal["builtin", "preset", "external", "package"] | None = None
    capabilities: AlgorithmCapabilities | None = None
    parameterSchema: dict[str, JsonValue] | None = None


class ColumnMapping(Strict):
    column: str = Field(max_length=24)
    field: str = Field(max_length=40)
    scale: float = Field(default=1, gt=0, le=1e6)
    offset: float = Field(default=0, ge=-10000, le=10000)


class FileSelection(Strict):
    fileId: str = Field(max_length=40)
    mappings: list[ColumnMapping] = Field(default_factory=list, max_length=100)
    useStandard: bool = False


class ConfirmRequest(Strict):
    name: str = Field(default="已确认数据", min_length=1, max_length=120)
    kilnCode: str = Field(default="KILN-01", min_length=1, max_length=40)
    zoneCount: int = Field(default=6, ge=1, le=16)
    burnerCount: int = Field(default=6, ge=1, le=64)
    files: list[FileSelection] = Field(min_length=1, max_length=200)
    acknowledgeConflicts: bool = False
    flowBasis: Literal["normal", "unknown"] = "normal"


class PhysicsParameters(Strict):
    ambientC: float = Field(default=25, ge=-40, le=80)
    lhvMJ: float = Field(default=36, ge=10, le=60)
    efficiency: float = Field(default=0.75, ge=0.1, le=1)
    capacityKJ: float = Field(default=20000, ge=100, le=1e7)
    lossKW: float = Field(default=0.3, ge=0.0001, le=100)
    tauMinutes: float = Field(default=30, ge=1, le=600)
    theoreticalAfr: float = Field(default=9.5, ge=1, le=30)
    excessAir: float = Field(default=1.1, ge=0.5, le=5)
    minGas: float = Field(default=0.5, ge=0.01, le=100)


class MechanismRequest(Strict):
    datasetId: str = Field(max_length=32)
    mode: Literal["ai", "template"] = "ai"


CatalogVersionId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
CatalogKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")]
CatalogInput = Annotated[str, Field(min_length=1, max_length=160)]


class MechanismScope(Strict):
    kilnCode: str = Field(default="", max_length=40)
    zoneCount: int | None = Field(default=None, ge=1, le=16)
    burnerCount: int | None = Field(default=None, ge=1, le=64)
    flowBasis: Literal["", "normal", "unknown"] = ""


class CatalogRevisionRequest(Strict):
    name: str = Field(min_length=1, max_length=160)
    explanation: str = Field(max_length=2400)
    formula: str = Field(max_length=1500)
    variables: list[CatalogInput] = Field(max_length=24)
    parameterNotes: str = Field(max_length=2400)
    parameterValues: dict[str, float] = Field(default_factory=dict, max_length=9)
    check: str = Field(max_length=1800)
    scope: MechanismScope = Field(default_factory=MechanismScope)

    @model_validator(mode="after")
    def normalize_text(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("机理名称不能为空")
        return self


class DocumentRecognitionRequest(Strict):
    fileIds: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        default_factory=list, max_length=200
    )
    mode: Literal["rules", "ai"] = "rules"


class DocumentMechanismCandidate(CatalogRevisionRequest):
    templateKey: TemplateKey | None = None
    sourceIds: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        min_length=1, max_length=8
    )


class DocumentMechanismExtraction(Strict):
    mechanisms: list[DocumentMechanismCandidate] = Field(max_length=24)


class SaveMechanismRequest(Strict):
    datasetId: str = Field(max_length=32)
    title: str = Field(min_length=1, max_length=160)
    templateKeys: list[TemplateKey] = Field(max_length=8)
    catalogSelections: dict[CatalogKey, CatalogVersionId] = Field(default_factory=dict)
    catalogAlternatives: list[CatalogVersionId] = Field(default_factory=list)
    parameters: PhysicsParameters = Field(default_factory=PhysicsParameters)
    provenance: dict[str, str] = Field(default_factory=dict, max_length=12)
    notes: str = Field(default="", max_length=1500)
    parentId: str | None = Field(default=None, max_length=32)
    calibrationExperimentId: str | None = Field(default=None, max_length=32)
    acceptAssumptions: bool = False

    @model_validator(mode="after")
    def require_mechanism(self):
        if not (self.templateKeys or self.catalogSelections):
            raise ValueError("请至少启用或收录一条机理")
        return self


class ExperimentRequest(Strict):
    datasetId: str = Field(max_length=32)
    mechanismId: str = Field(max_length=32)
    task: Literal["temperature", "energy", "afr"]
    zone: int = Field(default=0, ge=0, le=16)
    horizonMinutes: int = Field(default=30, ge=1, le=120)
    historyMinutes: int = Field(default=30, ge=5, le=240)
    ridgeAlpha: float = Field(default=1, ge=0.001, le=10000)
    boostingRounds: int = Field(default=40, ge=5, le=120)
    learningRate: float = Field(default=0.1, ge=0.01, le=0.5)
    calibrate: bool = True
    excludeZeroTemperatureRecords: bool = False
    lockedParameters: list[Literal["capacityKJ", "lossKW", "tauMinutes"]] = Field(
        default_factory=list, max_length=3
    )
    selectedAlgorithmKeys: list[AlgorithmKey] | None = Field(
        default=None, min_length=1, max_length=32
    )
    algorithmOptions: dict[AlgorithmKey, dict[str, JsonValue]] = Field(
        default_factory=dict, max_length=32
    )

    @model_validator(mode="after")
    def unique_selected_algorithms(self):
        keys = self.selectedAlgorithmKeys
        if keys is not None and len(set(keys)) != len(keys):
            raise ValueError("对比列表中存在重复算法")
        return self


class AlgorithmRecommendationRequest(ExperimentRequest):
    experimentId: str | None = Field(default=None, min_length=1, max_length=32)


class BacktestMetric(Strict):
    mae: float | None
    rmse: float | None
    coverage: float
    count: int


class AlgorithmBacktestMetrics(Strict):
    validation: BacktestMetric | None
    test: BacktestMetric | None


class SelectionEvidenceRef(Strict):
    id: str
    kind: Literal["dataset", "backtest", "mechanism"]
    assetId: str
    label: str
    detail: str
    file: str | None = None
    location: str | None = None


class AlgorithmRecommendationCard(Strict):
    key: AlgorithmKey
    name: str
    category: str
    oneLine: str
    strengths: list[str]
    benefits: list[str]
    whyNow: str
    limitations: list[str]
    backtestStatus: Literal["not_run", "unavailable", "backtested"]
    metrics: AlgorithmBacktestMetrics | None
    evidenceRefs: list[SelectionEvidenceRef]


class AlgorithmRecommendation(Strict):
    primaryKey: AlgorithmKey | None
    backupKey: AlgorithmKey | None
    reasons: list[str]
    confidence: Literal["high", "medium", "low"]
    confidenceBasis: str
    dataCaveats: list[str]
    disclaimer: str


class AlgorithmRecommendationResponse(Strict):
    mode: Literal["ai", "rules"]
    taskSummary: str
    algorithmCards: list[AlgorithmRecommendationCard]
    recommendation: AlgorithmRecommendation


class AIAlgorithmExplanation(Strict):
    key: AlgorithmKey
    whyNow: str = Field(min_length=1, max_length=300)
    evidenceRefs: list[str] = Field(max_length=4)


class AIAlgorithmRecommendation(Strict):
    algorithmCards: list[AIAlgorithmExplanation]
    primaryKey: AlgorithmKey | None
    backupKey: AlgorithmKey | None
    reasons: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        min_length=1, max_length=3
    )


class MechanismComparisonRequest(Strict):
    datasetId: str = Field(max_length=32)
    task: Literal["temperature", "energy", "afr"]
    zone: int = Field(default=0, ge=0, le=16)
    horizonMinutes: int = Field(default=30, ge=5, le=120)
    historyMinutes: int = Field(default=30, ge=5, le=240)
    currentParameters: PhysicsParameters
    currentTemplateKeys: list[TemplateKey] = Field(max_length=8)
    historyIds: list[str] = Field(min_length=1, max_length=4)


class BundleSelection(Strict):
    experimentId: str = Field(max_length=32)
    algorithm: AlgorithmKey


class BundleRequest(Strict):
    title: str = Field(min_length=1, max_length=160)
    selections: list[BundleSelection] = Field(min_length=1, max_length=3)
    baseBundleId: str | None = Field(default=None, min_length=1, max_length=32)


class SimulateRequest(Strict):
    cursorFraction: float = Field(default=0.6, ge=0.1, le=0.85)
    temperatureOffsetC: float = Field(default=0, ge=-100, le=100)


class TemperatureForecastRequest(Strict):
    cycleId: str = Field(min_length=1, max_length=40)
    cycleSource: Literal["dataset", "upload"] = "dataset"
    cursorFraction: float = Field(default=0.6, ge=0, le=1)
    durationMinutes: int = Field(default=60, ge=5, le=360)
    offsetC: float = Field(default=0, ge=-100, le=100)
    delayMinutes: int = Field(default=0, ge=0, le=360)
    rampMinutes: int = Field(default=15, ge=1, le=360)
    tauMinutes: float | None = Field(default=None, ge=1, le=600)
    deviationC: float = Field(default=30, gt=0, le=300)
    deviationMinutes: int = Field(default=10, ge=1, le=120)
    comparePlans: bool = False
    holdMinutes: int | None = Field(default=None, ge=1, le=360)
    targetC: float | None = Field(default=None, ge=0, le=1800)
    excludedRange: "ReplayExcludedRange | None" = None


class ConfirmTemperatureRecord(Strict):
    fileHash: str = Field(pattern=r"^[a-f0-9]{64}$")
    mappings: list[ColumnMapping] = Field(default_factory=list, max_length=100)
    useStandard: bool = False
    acknowledgeInputs: bool


class ReplayExcludedRange(Strict):
    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")

    def bounds(self):
        local = timezone(timedelta(hours=8))
        return tuple(
            datetime.fromisoformat(value).replace(tzinfo=local).timestamp() / 60
            for value in (self.start, self.end)
        )

    @model_validator(mode="after")
    def ordered(self):
        start, end = self.bounds()
        if start >= end:
            raise ValueError("排除区间的结束时间应晚于开始时间")
        return self


class ReplayRequest(Strict):
    experimentId: str = Field(min_length=1, max_length=32)
    cycleId: str = Field(min_length=1, max_length=40)
    frameStepMinutes: int = Field(default=5, ge=1, le=60)
    horizonMinutes: Literal[5, 10, 30, 60]
    selectedAlgorithms: list[AlgorithmKey] = Field(min_length=1, max_length=32)
    includeMechanism: bool = False
    excludedRange: ReplayExcludedRange | None = None

    @model_validator(mode="after")
    def unique_algorithms(self):
        if len(set(self.selectedAlgorithms)) != len(self.selectedAlgorithms):
            raise ValueError("算法选择不能重复")
        return self


class ReplayPoint(Strict):
    minuteOffset: float
    value: float | None


class ReplayBasePoint(Strict):
    minute: float
    ts: str
    actual: float | None
    setpoint: float | None


class ReplayZone(Strict):
    zone: int
    temp: float | None
    gas: float | None
    air: float | None
    airValve: float | None


class ReplaySnapshot(Strict):
    temp: float | None
    temp_sp: float | None
    gas: float | None
    air: float | None
    afr: float | None
    o2: float | None
    pressure: float | None
    zones: list[ReplayZone]


class ReplayFrame(Strict):
    frameId: int
    originTs: str
    originMinute: float
    snapshot: ReplaySnapshot
    future: dict[AlgorithmKey, list[ReplayPoint]]
    actualFuture: list[ReplayPoint]
    fusionReference: list[ReplayPoint] | None = None
    mechanismFuture: list[ReplayPoint] | None = None
    mechanismIssue: str | None = None


class ReplayAlgorithm(Strict):
    key: AlgorithmKey
    name: str
    mode: Literal["mechanism", "fusion", "recursive"]
    trainedHorizonMinutes: int
    forecastTrajectory: (
        Literal["mechanism", "persistence", "minuteRecursive", "multiOutput", "fixedHorizon"] | None
    ) = None
    temperatureTrajectory: (
        Literal["mechanism", "persistence", "minuteRecursive", "multiOutput", "fixedHorizon"] | None
    ) = None


class ReplayPackage(Strict):
    request: ReplayRequest
    cycleName: str
    zone: int
    targetLabel: str
    task: Literal["temperature", "energy", "afr"] = "temperature"
    unit: str = "℃"
    algorithms: list[ReplayAlgorithm]
    baseSeries: list[ReplayBasePoint]
    frames: list[ReplayFrame]
    notes: list[str]
    excludedCount: int = 0


class TemperatureReport(Strict):
    summary: str = Field(min_length=1, max_length=1200)
    findings: list[str] = Field(max_length=5)
    limitations: list[str] = Field(max_length=5)


class DashboardLayoutRequest(Strict):
    prompt: str = Field(min_length=1, max_length=500)


class PredictionEvaluationRequest(Strict):
    algorithm: AlgorithmKey
    split: Literal["validation", "test"] = "test"
    cycleId: str | None = Field(default=None, min_length=1, max_length=40)
    mapeMinActual: float = Field(default=1, ge=0.001, le=10000)
    overtemperatureC: float | None = Field(default=None, ge=-100, le=2500)
    eventDurationMinutes: int = Field(default=1, ge=0, le=60)
    eventToleranceMinutes: int = Field(default=2, ge=0, le=30)


class DashboardLayout(Strict):
    task: Literal["temperature", "energy", "afr"]
    chart: Literal["line", "bar"]
    windowMinutes: Literal[5, 10, 30]


class AIEvidence(Strict):
    sourceId: str = Field(min_length=1, max_length=80, description="从提供的来源编号中选择，例如S1")
    reason: str = Field(
        min_length=1, max_length=600, description="该来源与候选机理的关联解释，仍需用户确认"
    )
    quote: str = Field(default="", max_length=1200, description="留空，由服务器从来源中提取原文")


class AIInterpretation(Strict):
    summary: str = Field(max_length=1600)
    mechanisms: list[TemplateKey] = Field(max_length=8)
    evidence: list[AIEvidence] = Field(default_factory=list, max_length=18)
    warnings: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def bounded_content(self):
        if any(len(s) > 500 for s in self.warnings):
            raise ValueError("warning too long")
        return self


class AITableMapping(Strict):
    fileId: str = Field(max_length=40)
    mappings: list[ColumnMapping] = Field(max_length=100)
    reason: str = Field(max_length=600)
    sourceIds: list[str] = Field(default_factory=list, max_length=8)


class AIFieldInterpretation(Strict):
    summary: str = Field(max_length=1600)
    files: list[AITableMapping] = Field(max_length=60)
    warnings: list[str] = Field(default_factory=list, max_length=12)
