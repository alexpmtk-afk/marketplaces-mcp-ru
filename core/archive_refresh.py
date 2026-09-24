"""Dataset-generic refresh coordination for canonical marketplace archives.

The coordinator owns the lifecycle rule that an annual archive job is a reusable
refresh cursor, not a one-shot task. A COMPLETE job means "all data discovered
in that refresh cycle was committed"; it must be reopened for the next refresh
cycle so the dataset-specific discovery logic can compare provider truth with
canonical coverage and ingest only what is missing/corrected.

Dataset workers remain authoritative for source discovery, stable-key merge,
coverage proof, exact Drive/REMOTE-durable verification and final commit. This module
only provides the common lifecycle and the contract catalog future archive
datasets must register against.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ArchiveRefreshContract:
    marketplace: str
    dataset_family: str
    datasets: tuple[str, ...]
    coverage_model: str
    refresh_strategy: str
    freshness_evidence: tuple[str, ...]
    stable_keys: dict[str, tuple[str, ...]]
    completion_invariants: tuple[str, ...]


REFRESH_CONTRACTS: dict[str, ArchiveRefreshContract] = {
    "finance": ArchiveRefreshContract(
        marketplace="wb",
        dataset_family="finance",
        datasets=("wb_weekly_finance_main",),
        coverage_model="provider_report_registry",
        refresh_strategy="rediscover_provider_reports_then_ingest_only_missing_report_ids",
        freshness_evidence=(
            "provider report list",
            "reports_registry.csv COMPLETE report_id coverage",
            "canonical annual CSV date/reportId/rrdId content",
        ),
        stable_keys={"wb_weekly_finance_main": ("reportId", "rrdId")},
        completion_invariants=(
            "every discovered reportId is either already COMPLETE in registry or committed in this cycle",
            "annual CSV merge is idempotent on (reportId, rrdId)",
            "verified canonical Drive bytes and REMOTE durable backup exist before registry/progress COMMIT",
        ),
    ),
    "advertising": ArchiveRefreshContract(
        marketplace="wb",
        dataset_family="advertising",
        datasets=(
            "ads_campaign_roster_snapshots",
            "ads_campaign_daily",
            "ads_product_daily",
            "ads_search_cluster_daily",
            "ads_campaign_snapshots",
            "ads_product_identity_snapshots",
            "ads_expenses",
            "ads_payments",
        ),
        coverage_model="bounded_request_coverage_registry",
        refresh_strategy="reconcile_closed_provider_history_and_upsert_by_dataset_stable_key",
        freshness_evidence=(
            "closed provider period ending yesterday Europe/Moscow",
            "dataset_coverage_registry.csv COMPLETE request coverage",
            "canonical annual dataset stable-key content",
        ),
        stable_keys={
            "ads_campaign_roster_snapshots": ("observed_at", "campaign_id"),
            "ads_campaign_daily": ("date", "campaign_id"),
            "ads_product_daily": ("date", "campaign_id", "app_type", "nm_id"),
            "ads_search_cluster_daily": ("date", "campaign_id", "nm_id", "norm_query"),
            "ads_campaign_snapshots": ("observed_at", "campaign_id"),
            "ads_product_identity_snapshots": ("observed_at", "nm_id"),
            "ads_expenses": ("event_fingerprint",),
            "ads_payments": ("event_key",),
        },
        completion_invariants=(
            "provider request plan is committed to dataset_coverage_registry.csv only after canonical publication",
            "annual dataset merge is an upsert on the dataset-specific stable key",
            "verified canonical Drive bytes and REMOTE durable backup exist before coverage COMMIT",
        ),
    ),
    "ozon_final": ArchiveRefreshContract(
        marketplace="ozon",
        dataset_family="final",
        datasets=("ozon_final_realization",),
        coverage_model="closed_month_realization_registry",
        refresh_strategy="refetch_every_closed_month_then_replace_monthly_source_and_rebuild_annual",
        freshness_evidence=(
            "provider /v1/finance/realization/posting report exists for every closed month",
            "final_coverage_registry.csv COMPLETE row for every closed month",
            "annual realization CSV is rebuilt only from verified monthly_source files",
        ),
        stable_keys={
            "ozon_final_realization": ("report_year", "report_month", "row_number"),
        },
        completion_invariants=(
            "every closed month through the month before the current Moscow month is fetched successfully",
            "every monthly source passes complete and unique (report_year, report_month, row_number)",
            "annual CSV is the exact union of verified monthly sources and has no duplicate/incomplete stable keys",
            "canonical Drive files and REMOTE durable backups are verified before FINAL coverage is committed",
        ),
    ),
    "ozon_current": ArchiveRefreshContract(
        marketplace="ozon",
        dataset_family="current",
        datasets=(
            "ozon_current_orders_fbo",
            "ozon_current_orders_fbs",
            "ozon_current_accruals",
        ),
        coverage_model="open_month_snapshot_registry",
        refresh_strategy="full_refetch_open_month_then_atomically_replace_current_snapshot",
        freshness_evidence=(
            "provider snapshot covers first day of open month through current Moscow calendar day",
            "current_coverage_registry.csv COMPLETE row for every CURRENT dataset",
            "canonical CURRENT CSV SHA256/row count matches committed coverage",
        ),
        stable_keys={
            "ozon_current_orders_fbo": ("posting_number",),
            "ozon_current_orders_fbs": ("posting_number",),
            "ozon_current_accruals": ("accrual_id",),
        },
        completion_invariants=(
            "all three open-month provider snapshots pass stable-key completeness and uniqueness",
            "canonical Drive files are verified before current_coverage_registry.csv is published",
            "CURRENT refresh always refetches the complete open month so retrospective status/finance corrections replace stale rows",
            "coverage is committed only after canonical Drive and REMOTE durable backup writes succeed",
        ),
    ),
}


def refresh_catalog() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name in sorted(REFRESH_CONTRACTS):
        payload = asdict(REFRESH_CONTRACTS[name])
        payload["stable_keys"] = {
            dataset: list(fields) for dataset, fields in payload["stable_keys"].items()
        }
        payload["datasets"] = list(payload["datasets"])
        payload["freshness_evidence"] = list(payload["freshness_evidence"])
        payload["completion_invariants"] = list(payload["completion_invariants"])
        result.append(payload)
    return result


def normalize_refresh_family(
    value: str,
    *,
    marketplace: str = "wb",
) -> tuple[str, ...]:
    normalized = str(value or "all").strip().lower().replace("-", "_")
    market = str(marketplace or "wb").strip().lower()

    if market == "wb":
        aliases = {
            "all": ("advertising", "finance"),
            "finance": ("finance",),
            "wb_weekly_finance_main": ("finance",),
            "weekly_finance": ("finance",),
            "weekly": ("finance",),
            "основной": ("finance",),
            "финансы": ("finance",),
            "advertising": ("advertising",),
            "ads": ("advertising",),
            "ad": ("advertising",),
            "promotion": ("advertising",),
            "реклама": ("advertising",),
            "рекламные_кампании": ("advertising",),
        }
    elif market == "ozon":
        aliases = {
            "all": ("ozon_final", "ozon_current"),
            "final": ("ozon_final",),
            "ozon_final": ("ozon_final",),
            "realization": ("ozon_final",),
            "current": ("ozon_current",),
            "ozon_current": ("ozon_current",),
            "orders": ("ozon_current",),
            "accruals": ("ozon_current",),
            "текущая": ("ozon_current",),
            "текущий": ("ozon_current",),
            "закрытые_месяцы": ("ozon_final",),
            "реализация": ("ozon_final",),
        }
    else:
        raise ValueError(f"Unsupported archive marketplace: {marketplace!r}")

    if normalized not in aliases:
        supported = ", ".join(sorted(aliases))
        raise ValueError(
            f"Unknown {market} archive dataset family {value!r}; supported: {supported}"
        )
    return aliases[normalized]


def _reset_finance_state(state: dict[str, Any]) -> None:
    state["phase"] = "DISCOVER"
    state["discovery_offset"] = 0
    state["fragments"] = []
    state["report_index"] = 0
    state["current_rrd_id"] = 0
    state["completed_report_ids"] = []
    state.pop("finalize", None)
    state.pop("finalize_telemetry", None)


def _reset_advertising_state(state: dict[str, Any]) -> None:
    state["phase"] = "DISCOVER"
    state["fetch_plan"] = []
    state["fetch_index"] = 0
    state["identity_plan"] = []
    state["identity_index"] = 0
    state["cluster_plan"] = []
    state["cluster_index"] = 0
    state["completed_requests"] = []
    state["staged_datasets"] = {}
    state["campaign_ids"] = []
    state["fullstats_campaign_ids"] = []
    state.pop("commit", None)


async def enqueue_refresh_cycle(
    queue: Any,
    *,
    family: str,
    year: int,
    seller: str,
) -> dict[str, Any]:
    """Create/resume/reopen one durable annual refresh job.

    Existing non-COMPLETE jobs are simply rescheduled. Existing COMPLETE jobs
    are reopened at DISCOVER while durable canonical registries remain intact;
    those registries, not stale job state, determine what is already covered.
    """
    if family not in REFRESH_CONTRACTS:
        raise ValueError(f"Unsupported refresh family: {family}")

    year = int(year)
    cabinet = queue.normalize_cabinet(seller)
    job_id = queue.job_id(cabinet, year)
    existing = await queue._load(job_id)
    if existing is None:
        created = await queue.enqueue(year=year, seller=cabinet)
        return {
            **created,
            "dataset_family": family,
            "refresh_action": "created",
            "scheduled": True,
            "refresh_generation": int(created.get("refresh_generation", 1) or 1),
        }

    status = str(existing.get("status") or "")
    if status != "COMPLETE":
        await queue._schedule(job_id, 0)
        return {
            "ok": True,
            "job_id": job_id,
            "created": False,
            "reopened": False,
            "dataset_family": family,
            "status": status,
            "phase": existing.get("phase"),
            "cabinet": cabinet,
            "year": year,
            "refresh_action": "resumed_existing",
            "scheduled": True,
            "refresh_generation": int(existing.get("refresh_generation", 1) or 1),
        }

    generation = int(existing.get("refresh_generation", 1) or 1) + 1
    existing["status"] = "QUEUED"
    existing["last_retry_after_seconds"] = 0
    existing["last_error"] = None
    existing["refresh_generation"] = generation
    existing["refresh_started_at_utc"] = _utc_now()
    existing.pop("refresh_completed_at_utc", None)

    if family == "finance":
        _reset_finance_state(existing)
    elif family == "advertising":
        _reset_advertising_state(existing)

    await queue._save(existing)
    await queue._schedule(job_id, 0)
    return {
        "ok": True,
        "job_id": job_id,
        "created": False,
        "reopened": True,
        "dataset_family": family,
        "status": "QUEUED",
        "phase": "DISCOVER",
        "cabinet": cabinet,
        "year": year,
        "refresh_action": "reopened_complete",
        "scheduled": True,
        "refresh_generation": generation,
    }


# The coordinator is imported by every archive-tool surface. Installing the
# planner here guarantees that every advertising queue created by those tools
# uses durable coverage to suppress already committed historical requests.
from .archive_refresh_advertising import install_advertising_coverage_planner

install_advertising_coverage_planner()
