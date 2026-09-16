"""Runtime architecture-map extension for the top-level request source router.

The infra repository layers its own later architecture version over the app
snapshot. This extension therefore adds routing policy and server instructions
without independently renumbering the base app map.
"""
from __future__ import annotations

from . import system_map as _system_map

_ROUTER_MAP = {
    "status": "EXECUTION_PLAN_V2",
    "runtime_entry": "marketplace_query_plan",
    "position": "first server-side planning layer before metric-specific Semantic Core",
    "source_families": [
        "CANONICAL_ARCHIVE",
        "LIVE_CABINET_API",
        "PUBLIC_MARKETPLACE_SOURCE",
        "SYSTEM_INTERNAL",
        "HYBRID",
        "UNAVAILABLE",
    ],
    "execution_plan": {
        "version": "marketplace_execution_plan.v2",
        "statuses": ["READY", "READY_WITH_GATES", "NEEDS_CONTEXT", "BLOCKED"],
        "required_leg_policy": "all required legs must be available; otherwise final answer fails closed",
        "join_policy": "no cross-source arithmetic without an explicit Semantic contract",
        "provenance_policy": "every leg keeps its own source family, source status, executor and coverage gate",
        "parallel_policy": "independent multi-source legs may execute in parallel only after each leg is validated",
    },
    "clarification_gate": {
        "position": "after marketplace_query_plan and before any data executor",
        "interaction_state": "CLARIFICATION_REQUIRED",
        "triggers": [
            "execution_plan.status == NEEDS_CONTEXT",
            "semantic_resolution.resolution_type == AMBIGUOUS",
            "semantic_resolution.resolution_type == UNKNOWN",
            "source_status == UNRESOLVED_SOURCE_CLASS when the business meaning is not safely identified",
        ],
        "missing_context_behavior": (
            "ask only for the missing marketplace, seller/cabinet, exact period, product identifier, or other required context"
        ),
        "ambiguous_behavior": (
            "explain that more than one business meaning is possible and ask the user to choose or clarify the intended meaning; preserve candidate routes when available"
        ),
        "unknown_behavior": (
            "state that the requested business meaning is not yet recognized and ask the user what business result they want, without guessing a similar metric"
        ),
        "known_source_gap_behavior": (
            "do not ask a semantic clarification when meaning is already clear and the required source/executor is genuinely unavailable; report the source gap instead"
        ),
        "recognized_multi_source_behavior": (
            "do not ask for clarification merely because several source domains are required; build separate required legs when their meanings are independently clear"
        ),
        "assistant_response_policy": (
            "ChatGPT/Codex must present the clarification to the user in normal language and must not execute data legs while clarification is required"
        ),
        "resume_policy": (
            "after the user answers, call marketplace_query_plan again with the clarified request and preserved marketplace/seller/period context; execution may start only after the new plan is READY or READY_WITH_GATES"
        ),
        "loop_policy": (
            "repeat clarification if the replanned request is still ambiguous or missing required context; never resolve uncertainty by silent inference"
        ),
    },
    "execution_controller": {
        "status": "CONTRACT_DISPATCH_V1",
        "runtime_entry": "marketplace_execution_control",
        "position": "after Clarification Gate and before every provider/archive/card executor",
        "controller_version": "marketplace_execution_controller.v1",
        "leg_contract_version": "marketplace_leg_execution.v1",
        "states": ["READY", "READY_WITH_GATES", "CLARIFICATION_REQUIRED", "BLOCKED"],
        "input_policy": (
            "the controller independently rebuilds marketplace_query_plan from the user request and known context; clients do not author executor contracts"
        ),
        "compound_question_policy": (
            "a multi-source compound user question must never be copied unchanged into each lower executor; every leg receives a deterministic narrow executor contract"
        ),
        "dispatch_policy": (
            "invoke only the executor and exact arguments returned by marketplace_execution_control; do not rewrite, broaden, or supplement executor arguments client-side"
        ),
        "all_or_nothing_policy": (
            "if any required leg lacks an executor, source, context, or deterministic semantic contract, no required leg is dispatchable"
        ),
        "coverage_policy": (
            "READY_WITH_GATES may dispatch only through the returned contract; the executor must prove its coverage gate before the result is answerable"
        ),
        "join_policy": (
            "wait for every required contract, preserve provenance per leg, fail closed on a missing leg, and prohibit arithmetic without an explicit approved calculation contract"
        ),
    },
    "join_controller": {
        "status": "REGISTERED_JOIN_CONTRACTS_V1",
        "runtime_entry": "marketplace_join_control",
        "position": "after all required executor results and before multi-source synthesis or arithmetic",
        "controller_version": "marketplace_join_controller.v1",
        "join_contract_version": "marketplace_join_contract.v1",
        "calculation_contract_version": "marketplace_calculation_contract.v1",
        "states": ["READY", "CLARIFICATION_REQUIRED", "BLOCKED"],
        "result_binding_policy": (
            "the controller rebuilds marketplace_execution_control and binds each actual result to the exact required contract_id; missing, duplicate or unexpected results fail closed"
        ),
        "scope_policy": (
            "registered joins validate semantic target, marketplace, seller/cabinet, requested period and required coverage before synthesis"
        ),
        "approved_v1_join": "WB sale_and_return_operations + advertising_performance side-by-side comparison only",
        "arithmetic_policy": (
            "cross-source arithmetic is disabled in V1; a calculation_id must resolve to a separately registered formula before any ratio, difference, total, DRR, ROAS or profitability calculation"
        ),
        "currency_policy": (
            "currency alignment is never inferred; finance currencies and advertising money stay independently sourced and may not be arithmetically combined without an approved currency-aware calculation contract"
        ),
        "registered_cross_source_calculations": [],
    },
    "availability_facts": {
        "wb_orders_historical_archive": False,
        "wb_stock_historical_archive": False,
    },
    "rules": [
        "source family is selected before report/API fields",
        "code presence does not prove historical data exists",
        "missing archive history is never replaced with a current snapshot",
        "recent WB orders may use the operational cabinet API only inside its supported retention window",
        "historical WB stock is unavailable until a real historical source is populated and approved",
        "public marketplace facts are not substituted with private seller-cabinet values",
        "multi-domain questions produce separate source legs before any join",
        "every execution leg records executor, gate, missing context and forbidden substitutes",
        "a missing required leg blocks the final joined answer instead of allowing silent partial truth",
        "ambiguous, unknown, or context-incomplete meaning enters the clarification gate before any provider/archive read",
        "known semantic source gaps are reported as gaps and are not disguised as requests for clarification",
        "after clarification the request is replanned from the top; an old blocked/ambiguous plan is never resumed directly",
        "after a READY or READY_WITH_GATES plan, marketplace_execution_control is the only approved source of per-leg executor arguments",
        "compound multi-source wording is never forwarded unchanged to multiple lower executors",
        "if one required execution contract cannot be constructed, all dispatch is withheld until the plan is repaired or the gap is reported",
        "after all required leg results return, marketplace_join_control is the only approved source of permission to synthesize a registered multi-source join",
        "side-by-side comparison permission never implies arithmetic permission",
        "no cross-source calculation is registered in Join / Calculation Contracts V1",
    ],
}

_system_map.SYSTEM_MAP["request_source_router"] = _ROUTER_MAP
_system_map.SYSTEM_MAP["routing_policy"]["top_level_request"] = (
    "call marketplace_query_plan first; if meaning/context is ambiguous, unknown, or incomplete, enter the clarification gate and ask the user before any data read; "
    "when the replanned request is READY or READY_WITH_GATES, call marketplace_execution_control and use only its exact per-leg executor contracts; "
    "execute validated archive, live cabinet/API, public marketplace/card, or system-internal legs; when two or more required results must be synthesized, call marketplace_join_control with those exact contract IDs and results before presenting a joined answer or attempting arithmetic"
)

_EXTRA_INSTRUCTIONS = """
For every natural marketplace request, use marketplace_query_plan as the first server-side planning step before choosing archive, seller-cabinet/API, public-card/site, or other data tools.
The top-level planner chooses a source family before lower Semantic Core chooses a metric, report, field, or endpoint. Code/module presence never proves that historical data is actually populated.
The planner returns marketplace_execution_plan.v2: every required source leg has its own source family, source status, downstream executor, execution status, coverage gate, required context and forbidden substitutes.
Before any provider, archive, card-monitor, or calculation executor is called, apply the Clarification Gate. If execution_plan.status is NEEDS_CONTEXT, ask the user only for the missing context reported by the plan. If semantic_resolution is AMBIGUOUS, explain that several business meanings are possible and ask the user to choose or clarify the intended meaning. If semantic_resolution is UNKNOWN, or the upper source class is unresolved because the business meaning is not safely identified, say that the request is not yet recognized precisely enough and ask what exact business result the user means. Never guess the closest metric.
A recognized multi-source request is not ambiguous merely because it needs several data domains: create separate source legs and continue when each meaning is clear. Conversely, when the meaning is clear but a required source/executor is genuinely unavailable, report that source gap rather than asking an unnecessary semantic clarification.
When clarification is required, ChatGPT/Codex must present the clarification in normal user-facing language and must not execute any data leg. After the user answers, call marketplace_query_plan again using the clarified request while preserving known marketplace, seller/cabinet, period and product context. Do not resume the old ambiguous plan directly. Repeat this loop until the replanned request is READY or READY_WITH_GATES, or until a genuine source gap is established.
After a READY or READY_WITH_GATES plan, call marketplace_execution_control before every provider/archive/card executor. Query Execution Controller V1 is the canonical server-side owner of per-leg executor arguments. Use only the executor name and exact executor_arguments returned in dispatch_contracts; do not rewrite, broaden, merge, or supplement them client-side.
Never pass the original compound multi-source user question unchanged into several lower executors. The controller must turn every required leg into marketplace_leg_execution.v1 with its own narrow business meaning, source contract, coverage gate, forbidden substitutions and provenance requirements. If any required leg cannot get a deterministic execution contract, dispatch_contracts must be empty and no other required leg may run as a partial answer.
After every required multi-source executor result has returned, call marketplace_join_control before presenting a joined multi-source synthesis. Pass each result with the exact contract_id returned by marketplace_execution_control. Join Controller V1 rebuilds the execution control server-side, rejects missing/duplicate/unexpected results, checks semantic target and required coverage, and authorizes only a registered join contract.
Join permission and arithmetic permission are separate. V1 registers only a descriptive side-by-side WB comparison of sale_and_return_operations with advertising_performance for the same requested seller/period. It does not authorize subtraction, ratios, totals, percentages, DRR, ROAS, profitability or any other derived cross-source metric. If the user asks for such arithmetic, an explicit registered marketplace_calculation_contract is required; do not invent a formula or reinterpret the advertising-attribution DRR as overall business DRR.
There is currently no verified populated WB historical orders archive and no historical stock archive. Recent WB orders may use the operational cabinet API only within its supported retention window; older order history must fail closed. Historical stock must fail closed and must never receive today's snapshot.
Public marketplace/card facts are a separate source family from private seller-cabinet facts. If an on-demand public source is not connected, report that source gap instead of substituting cabinet or archive data.
Questions that require several domains must produce separate source legs and validate each leg before joining results. All required legs are mandatory by default; if one required leg is unavailable, the final joined answer must fail closed rather than silently returning a partial answer.
Never perform cross-source arithmetic or derived business calculations merely because two source legs exist. Arithmetic joins require an explicit approved Semantic calculation contract, currency alignment must be proven rather than inferred, and provenance must be preserved for every leg.
"""
if "marketplace_query_plan as the first server-side planning step" not in _system_map.SYSTEM_INSTRUCTIONS:
    _system_map.SYSTEM_INSTRUCTIONS += _EXTRA_INSTRUCTIONS
