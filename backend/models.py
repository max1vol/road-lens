from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=4000)]
Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_.:-]+$')]
SourceId = Literal['dft_stats19_2025_final', 'ccc_2017_2026h1_20260907']

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Issue(str, Enum):
    visibility_obstruction = 'visibility_obstruction'
    road_surface = 'road_surface'
    crossing_concern = 'crossing_concern'
    access_obstruction = 'access_obstruction'
    speeding_concern = 'speeding_concern'
    signal_or_lighting = 'signal_or_lighting'
    other_road_concern = 'other_road_concern'

class ResidentTurn(StrictModel):
    turn_id: Id
    text: Text
    timestamp: datetime
    role: Literal['resident'] = 'resident'

class PrepareRequest(StrictModel):
    session_id: Id
    turns: list[ResidentTurn] = Field(min_length=1, max_length=30)

    @model_validator(mode='after')
    def bounded_transcript(self):
        if sum(len(t.text) for t in self.turns) > 4000:
            raise ValueError('Total resident transcript exceeds 4000 characters')
        if len({t.turn_id for t in self.turns}) != len(self.turns):
            raise ValueError('Turn IDs must be unique')
        if any(t.timestamp.tzinfo is None for t in self.turns):
            raise ValueError('Turn timestamps must include timezone')
        if any(a.timestamp > b.timestamp for a, b in zip(self.turns, self.turns[1:])):
            raise ValueError('Resident turns must be chronological')
        return self

class EntitySpan(StrictModel):
    text: Annotated[str, Field(max_length=300)]
    start: int = Field(ge=0, le=4000)
    end: int = Field(ge=0, le=4000)
    confidence: float | None = Field(default=None, ge=0, le=1)

class ClassifierSuggestion(StrictModel):
    model: str = 'fastino/gliner2.5-base-v1'
    revision: str = '78cea040597df251eedefa9d7ee2a756af39fe64'
    issue_label: Annotated[str, Field(max_length=100)]
    entities: list[EntitySpan] = Field(default_factory=list, max_length=50)
    runtime_seconds: float = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)

class ResolvedLocation(StrictModel):
    place_id: Id
    display_name: Annotated[str, Field(max_length=200)]
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    easting: int
    northing: int
    coordinate_basis: Annotated[str, Field(max_length=200)]
    resolver_provenance: Annotated[str, Field(max_length=200)]
    ambiguity_status: Literal['resolved_reference_point'] = 'resolved_reference_point'

class EvidenceSpan(StrictModel):
    turn_id: Id
    quote: Annotated[str, Field(min_length=1, max_length=1000)]

class NeedsClarification(StrictModel):
    kind: Literal['needs_clarification'] = 'needs_clarification'
    missing_fields: list[Annotated[str, Field(max_length=80)]] = Field(min_length=1, max_length=5)
    question: Annotated[str, Field(min_length=5, max_length=240)]

class OutOfScope(StrictModel):
    kind: Literal['out_of_scope'] = 'out_of_scope'
    message: Annotated[str, Field(min_length=5, max_length=240)]

class ReadyDraft(StrictModel):
    kind: Literal['ready_for_confirmation'] = 'ready_for_confirmation'
    issue: Issue
    observation: Annotated[str, Field(min_length=5, max_length=1000, description='Exact resident wording describing the concern; no contact information.')]
    location: ResolvedLocation
    evidence_spans: list[EvidenceSpan] = Field(min_length=1, max_length=10)
    readback: Annotated[str, Field(max_length=400)] = ''

IntakeOutput = Annotated[ReadyDraft | NeedsClarification | OutOfScope, Field(discriminator='kind')]

class Confirmation(StrictModel):
    resident_turn_id: Id
    text: Annotated[str, Field(min_length=1, max_length=500)]
    confirmed_at: datetime

class Submission(StrictModel):
    draft_id: Id
    confirmation: Confirmation

class EvidenceQuery(StrictModel):
    source_id: SourceId = 'dft_stats19_2025_final'
    geography: Literal['E07000008'] = 'E07000008'
    start: date = date(2025, 1, 1)
    end: date = date(2025, 12, 31)
    unit: Literal['people', 'collisions', 'people with known age', 'people in that collision'] = 'people'
    road_user: Literal['all', 'cyclist', 'pedestrian'] = 'all'
    severity: Literal['all', 'serious', 'slight'] = 'all'
    age: Literal['all', 'under16', 'unknown'] = 'all'
    collision_id: Annotated[str, Field(max_length=40)] | None = None
    place_id: Id | None = None
    radius_metres: Literal[50, 100, 250] = 100

    @model_validator(mode='after')
    def supported(self):
        bounds = (date(2025, 1, 1), date(2025, 12, 31)) if self.source_id == 'dft_stats19_2025_final' else (date(2017, 1, 1), date(2026, 6, 30))
        if not bounds[0] <= self.start <= self.end <= bounds[1]:
            raise ValueError('Date period is outside the selected snapshot')
        if self.unit == 'collisions' and self.age != 'all':
            raise ValueError('Age filters apply to people, not collisions')
        if self.unit == 'people with known age' and self.age != 'under16':
            raise ValueError('Known-age unit requires the under16 filter')
        if self.unit == 'people in that collision' and not self.collision_id:
            raise ValueError('A specific collision ID is required')
        if self.source_id.startswith('ccc') and (self.road_user != 'all' or self.age != 'all'):
            raise ValueError('Local snapshot does not support casualty type/age filters')
        return self

class SourceNotes(StrictModel):
    source_id: SourceId
    title: str
    url: str
    period: str
    geography: str = 'Cambridge district (E07000008)'
    status: Literal['national final', 'local provisional snapshot']
    caution: str

class MetricResult(StrictModel):
    reference_id: Id
    value: int = Field(ge=0)
    unit: str
    period_start: date
    period_end: date
    geography: str = 'Cambridge district (E07000008)'
    source_id: SourceId
    provisional: bool
    query_hash: str
    row_ids: list[str] = Field(max_length=5000)
    query: EvidenceQuery

class CollisionRecord(StrictModel):
    reference_id: Id
    collision_id: str
    date: date
    latitude: float
    longitude: float
    severity: str
    casualty_count: int = Field(ge=0)
    location_text: str
    junction_detail: str
    crossing: str
    source_id: SourceId
    provisional: bool

class CollisionEvidence(StrictModel):
    reference_id: Id
    location: ResolvedLocation
    radius_metres: Literal[50, 100, 250]
    start: date
    end: date
    collision_count: int
    casualty_total: int
    records: list[CollisionRecord] = Field(max_length=300)
    note: str = 'Projected EPSG:27700 radius, not an exact junction boundary. Historical evidence does not establish causation or present conditions.'

Limitation = Literal['no_causal_evidence', 'no_exposure_denominator', 'current_imagery_not_supplied', 'overlapping_sources', 'historic_conditions', 'unverified_resident_observation']
InspectionCheck = Literal['check_current_sightlines', 'observe_crossing_access', 'check_surface_condition', 'check_signal_or_lighting', 'measure_speeds_and_exposure', 'verify_location_on_site']

class EvidenceAnswer(StrictModel):
    kind: Literal['evidence_answer'] = 'evidence_answer'
    metric_refs: list[Id] = Field(default_factory=list, max_length=10)
    record_refs: list[Id] = Field(default_factory=list, max_length=20)
    limitations: list[Limitation] = Field(default_factory=list, max_length=6)
    inspection_checks: list[InspectionCheck] = Field(default_factory=list, max_length=6)

class InspectionBrief(EvidenceAnswer):
    kind: Literal['inspection_brief'] = 'inspection_brief'
    resident_concern: Annotated[str, Field(max_length=1000)]

EvidenceOutput = Annotated[EvidenceAnswer | InspectionBrief | NeedsClarification, Field(discriminator='kind')]

class EvidenceBundle(StrictModel):
    metrics: list[MetricResult] = Field(default_factory=list, max_length=10)
    records: list[CollisionRecord] = Field(default_factory=list, max_length=300)
    nearby: list[CollisionEvidence] = Field(default_factory=list, max_length=5)
    sources: list[SourceNotes] = Field(max_length=2)
    unknowns: list[Limitation] = Field(default_factory=list, max_length=6)

class ToolEvent(StrictModel):
    name: str
    inputs: dict
    output: dict | list

class RunSummary(StrictModel):
    run_id: Id
    model: str
    model_version: str
    classifier: ClassifierSuggestion | None = None
    tools: list[ToolEvent] = Field(default_factory=list, max_length=20)
    validation_repairs: list[str] = Field(default_factory=list, max_length=10)
    usage: dict
    elapsed_seconds: float
    outcome: str
    data_hashes: dict[str, str]
    prompt_hash: str
    code_commit: str

class IntakeResponse(StrictModel):
    output: IntakeOutput
    run: RunSummary

class AnalysisResult(StrictModel):
    output: EvidenceOutput
    evidence: EvidenceBundle
    run: RunSummary

class OfficerQuestion(StrictModel):
    question: Annotated[str, Field(min_length=3, max_length=2000)]
    report_id: Id | None = None

class JobResult(StrictModel):
    version: Literal[1] = 1
    lease_token: Id
    result: AnalysisResult | None = None
    error: Literal['analysis_unavailable'] | None = None

    @model_validator(mode='after')
    def one_outcome(self):
        if (self.result is None) == (self.error is None):
            raise ValueError('Exactly one result or error required')
        return self
