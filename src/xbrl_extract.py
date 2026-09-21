"""Extract every inline XBRL fact from the JPMC filings using Arelle.

Arelle does all context/unit/dimension/transform resolution. The filings'
extension taxonomies (jpm-*.xsd + linkbases) must sit next to the .htm files,
and the Arelle EDGAR plugin (vendor/EDGAR) supplies the ixt-sec transforms
(numwordsen, durmonth, ...) that the core package does not ship.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from arelle import Cntlr, ModelValue, PluginManager, ValidateDuplicateFacts, XbrlConst
from arelle.ModelInstanceObject import ModelInlineFact
from arelle.ValidateDuplicateFactsConst import DeduplicationType, DuplicateType

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "jpmc"
EDGAR_TRANSFORM_PLUGIN = ROOT / "vendor" / "EDGAR" / "transform"

SOURCE_DOCUMENTS = {
    "jpmc_10k_2025.htm": "JPMC_10-K_FY2025",
    "jpmc_10q_q2_2026.htm": "JPMC_10-Q_Q2-2026",
}


@dataclass
class Fact:
    concept: str
    value: str | None
    value_numeric: float | None
    is_numeric: bool
    is_nil: bool
    unit: str | None
    decimals: str | None
    scale: str | None
    period_type: str  # instant | duration | forever
    period_start: str | None  # None for instant/forever
    period_end: str | None  # instant date for instant periods
    entity: str
    dimensions: dict[str, str] = field(default_factory=dict)
    context_id: str = ""
    source_document: str = ""


@dataclass
class ConceptInfo:
    concept: str
    label: str | None  # standard label
    labels: list[str]  # every label role (terse, total, negated, ...) - the filer's own wording
    period_type: str | None
    data_type: str | None
    balance: str | None


@dataclass
class ExtractResult:
    source_document: str
    facts: list[Fact]
    raw_fact_count: int
    unresolved_concepts: list[str]
    inconsistent_duplicates: list[tuple]
    error_codes: dict[str, int]
    concepts: dict[str, ConceptInfo] = field(default_factory=dict)


def _make_controller() -> Cntlr.Cntlr:
    cntlr = Cntlr.Cntlr(logFileName="logToBuffer")
    if not EDGAR_TRANSFORM_PLUGIN.exists():
        raise FileNotFoundError(
            f"Arelle EDGAR plugin missing at {EDGAR_TRANSFORM_PLUGIN}; "
            "see README (git clone https://github.com/Arelle/EDGAR vendor/EDGAR)"
        )
    PluginManager.init(cntlr, loadPluginConfig=False)
    info = PluginManager.addPluginModule(str(EDGAR_TRANSFORM_PLUGIN))
    if not info:
        raise RuntimeError("Failed to load Arelle EDGAR transform plugin")
    PluginManager.reset()
    cntlr.modelManager.customTransforms = None
    cntlr.modelManager.loadCustomTransforms()  # the CLI does this; the API does not
    if not cntlr.modelManager.customTransforms:
        raise RuntimeError("EDGAR plugin loaded but registered no ixt-sec transforms")
    return cntlr


def _unit_string(unit) -> str | None:
    if unit is None:
        return None
    num, den = unit.measures
    s = "*".join(str(q) for q in num)
    if den:
        s += "/" + "*".join(str(q) for q in den)
    return s


def _period(ctx) -> tuple[str, str | None, str | None]:
    if ctx.isForeverPeriod:
        return "forever", None, None
    if ctx.isInstantPeriod:
        # Arelle stores instants as the following midnight; undo that.
        return "instant", None, str(ModelValue.dateunionDate(ctx.instantDatetime, subtractOneDay=True))
    return (
        "duration",
        str(ModelValue.dateunionDate(ctx.startDatetime)),
        str(ModelValue.dateunionDate(ctx.endDatetime, subtractOneDay=True)),
    )


def _dimensions(ctx) -> dict[str, str]:
    dims = {}
    for dim_qname, dim_value in ctx.qnameDims.items():
        if dim_value.isExplicit:
            dims[str(dim_qname)] = str(dim_value.memberQname)
        else:  # typed dimension: keep the typed member's text content
            dims[str(dim_qname)] = dim_value.typedMember.stringValue.strip()
    return dict(sorted(dims.items()))


def _concept_info(model, concept) -> ConceptInfo:
    rels = model.relationshipSet(XbrlConst.conceptLabel).fromModelObject(concept)
    labels = []
    for rel in rels:
        res = rel.toModelObject
        if res is not None and (res.xmlLang or "en").startswith("en"):
            text = " ".join(res.textValue.split())
            if text and text not in labels:
                labels.append(text)
    return ConceptInfo(
        concept=str(concept.qname),
        label=concept.label(lang="en", fallbackToQname=False),
        labels=labels,
        period_type=concept.periodType,
        data_type=str(concept.typeQname) if concept.typeQname is not None else None,
        balance=concept.balance,
    )


def _referenced_concepts(model, facts) -> dict[str, ConceptInfo]:
    """Labels for every fact concept plus every axis and member used in contexts."""
    seen = {}
    for f in facts:
        seen.setdefault(f.qname, f.concept)
        for dim_qname, dv in f.context.qnameDims.items():
            seen.setdefault(dim_qname, dv.dimension)
            if dv.isExplicit:
                seen.setdefault(dv.memberQname, dv.member)
    return {str(q): _concept_info(model, c) for q, c in seen.items() if c is not None}


def _to_fact(f: ModelInlineFact, source_document: str) -> Fact:
    ctx = f.context
    period_type, start, end = _period(ctx)
    is_nil = f.isNil
    value = None if is_nil else f.value
    value_numeric = None
    if f.isNumeric and not is_nil and f.xValue is not None:
        value_numeric = float(f.xValue)
    return Fact(
        concept=str(f.qname),
        value=value,
        value_numeric=value_numeric,
        is_numeric=bool(f.isNumeric),
        is_nil=bool(is_nil),
        unit=_unit_string(f.unit),
        decimals=f.decimals,
        scale=f.get("scale"),
        period_type=period_type,
        period_start=start,
        period_end=end,
        entity=ctx.entityIdentifier[1],
        dimensions=_dimensions(ctx),
        context_id=f.contextID,
        source_document=source_document,
    )


def extract_document(path: Path, source_document: str, cntlr: Cntlr.Cntlr | None = None) -> ExtractResult:
    cntlr = cntlr or _make_controller()
    cntlr.logHandler.clearLogBuffer()
    model = cntlr.modelManager.load(str(path))
    if model is None or model.modelDocument is None:
        raise RuntimeError(f"Arelle could not load {path}")
    try:
        raw = list(model.factsInInstance)
        unresolved = sorted({str(f.qname) for f in raw if f.concept is None})
        # XBRL Duplicates spec via Arelle: consistent numeric duplicates (same
        # fact reported rounded in different tables) collapse to the most
        # precise one; inconsistent sets are kept whole and reported.
        kept = ValidateDuplicateFacts.getDeduplicatedFacts(model, DeduplicationType.CONSISTENT_SETS)
        inconsistent = [
            (str(ds.facts[0].qname), ds.facts[0].contextID, sorted(str(f.value) for f in ds.facts))
            for ds in ValidateDuplicateFacts.getDuplicateFactSetsWithType(raw, DuplicateType.INCONSISTENT)
        ]
        facts = [_to_fact(f, source_document) for f in kept]
        codes = Counter(
            getattr(r, "messageCode", "") for r in cntlr.logHandler.logRecordBuffer
            if r.levelname in ("ERROR", "CRITICAL")
        )
        return ExtractResult(
            source_document=source_document,
            facts=facts,
            raw_fact_count=len(raw),
            unresolved_concepts=unresolved,
            inconsistent_duplicates=inconsistent,
            error_codes=dict(codes),
            concepts=_referenced_concepts(model, raw),
        )
    finally:
        cntlr.modelManager.close(model)


def extract_all(raw_dir: Path = RAW_DIR) -> list[ExtractResult]:
    cntlr = _make_controller()
    return [extract_document(raw_dir / name, doc, cntlr) for name, doc in SOURCE_DOCUMENTS.items()]


if __name__ == "__main__":
    for r in extract_all():
        print(json.dumps({
            "document": r.source_document,
            "raw_facts": r.raw_fact_count,
            "unique_facts": len(r.facts),
            "unresolved_concepts": r.unresolved_concepts,
            "inconsistent_duplicates": len(r.inconsistent_duplicates),
            "arelle_errors": r.error_codes,
        }))
        print(json.dumps(asdict(r.facts[0]))[:400])
