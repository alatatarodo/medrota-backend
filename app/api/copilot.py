from __future__ import annotations

from datetime import date
import json
import re
import uuid

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.operations import build_operations_workspace_payload, _record_audit_event
from app.core.config import settings
from app.core.schemas import CopilotQueryRequest, CopilotQueryResponse, CopilotStatusResponse
from app.db.database import get_db

router = APIRouter(prefix="/api/v1/copilot", tags=["copilot"])

STARTER_PROMPTS = [
    "Where are the biggest rota risks this week?",
    "Which locum requests need attention first and why?",
    "Show me the most urgent compliance issues across both sites.",
    "What cover actions should I take for Trafford tonight?",
]

GUARDRAILS = [
    "AI can recommend actions, but approvals and bookings still follow the policy workflow.",
    "The copilot should never override grade rules, compliance thresholds, or finance controls.",
    "Every recommendation is grounded in the same rota, locum, and absence data shown in the workspace.",
]

ALLOWED_TABS = {
    "command",
    "planning",
    "board",
    "patterns",
    "availability",
    "locums",
    "compliance",
    "upload",
    "schedule",
    "reports",
}


def _copilot_mode() -> str:
    return "live_ai" if settings.openai_api_key.strip() else "fallback"


def _extract_site_name(item: dict) -> str:
    return item.get("hospital_site") or item.get("site") or "Unknown"


def _detect_site_from_message(message: str, workspace: dict) -> str | None:
    lowered = message.lower()
    for site_name in workspace.get("reference_data", {}).get("hospital_sites", []):
        if site_name.lower() in lowered:
            return site_name
        simplified = site_name.replace(" Hospital", "").lower()
        if simplified and simplified in lowered:
            return site_name
    return None


def _workspace_context_snapshot(workspace: dict, hospital_site: str | None, schedule_id: str | None, active_module: str | None) -> dict:
    filtered_shortfalls = workspace.get("rota_planning", {}).get("ward_shortfalls", [])
    filtered_locums = workspace.get("locum_requests", [])
    filtered_events = workspace.get("leave_events", [])
    escalation_flags = workspace.get("compliance", {}).get("escalation_flags", [])

    if hospital_site:
        filtered_shortfalls = [item for item in filtered_shortfalls if _extract_site_name(item) == hospital_site]
        filtered_locums = [item for item in filtered_locums if item.get("hospital_site") == hospital_site]
        filtered_events = [item for item in filtered_events if item.get("hospital_site") == hospital_site]
        escalation_flags = [item for item in escalation_flags if item.get("site") == hospital_site]

    active_events = [
        item for item in filtered_events
        if item.get("status") not in {"CANCELLED", "DECLINED", "REJECTED"}
        and item.get("end_date", "") >= date.today().isoformat()
    ]
    sickness_events = [item for item in active_events if item.get("event_type") == "SICKNESS"]
    pending_locums = [item for item in filtered_locums if item.get("approval_status") == "PENDING_APPROVAL"]
    finance_locums = [item for item in filtered_locums if item.get("requires_finance_signoff")]

    return {
        "active_module": active_module,
        "hospital_site": hospital_site,
        "schedule_id": schedule_id,
        "summary": workspace.get("summary", {}),
        "approval_overview": workspace.get("compliance", {}).get("approval_overview", {}),
        "top_escalations": escalation_flags[:4],
        "ward_shortfalls": filtered_shortfalls[:6],
        "pending_locums": pending_locums[:6],
        "finance_requests": finance_locums[:6],
        "active_sickness_events": sickness_events[:6],
        "active_leave_events": active_events[:8],
        "shift_patterns": workspace.get("shift_patterns", [])[:8],
        "activity_feed": workspace.get("activity_feed", [])[:6],
        "reference_data": {
            "hospital_sites": workspace.get("reference_data", {}).get("hospital_sites", []),
            "doctor_grades": workspace.get("reference_data", {}).get("doctor_grades", []),
            "compliance_levels": workspace.get("reference_data", {}).get("compliance_levels", []),
            "staff_types": workspace.get("reference_data", {}).get("staff_types", []),
        },
    }


def _normalise_action(raw_action: dict | None) -> dict | None:
    if not isinstance(raw_action, dict):
        return None

    label = str(raw_action.get("label") or raw_action.get("title") or "").strip()
    action_type = str(raw_action.get("action_type") or raw_action.get("type") or "").strip().lower()
    payload = raw_action.get("payload") if isinstance(raw_action.get("payload"), dict) else {}

    if action_type == "navigate":
        tab = payload.get("tab") or raw_action.get("target")
        if tab not in ALLOWED_TABS:
            return None
        return {
            "label": label or f"Open {tab.title()}",
            "action_type": "navigate",
            "payload": {"tab": tab},
        }

    if action_type == "open_reports":
        schedule_id = payload.get("schedule_id")
        hospital_filter = payload.get("hospital_filter") or "all"
        return {
            "label": label or "Open reports",
            "action_type": "open_reports",
            "payload": {
                "schedule_id": schedule_id,
                "hospital_filter": hospital_filter,
            },
        }

    if action_type == "open_locum_form":
        allowed_fields = {
            "hospital_site",
            "department",
            "ward",
            "requested_date",
            "shift_code",
            "required_grade",
            "compliance_level",
            "staff_type",
            "approval_required",
            "requested_hours",
            "shortage_reason",
            "requested_by",
            "notes",
        }
        cleaned_payload = {key: value for key, value in payload.items() if key in allowed_fields}
        if not cleaned_payload.get("hospital_site"):
            return None
        return {
            "label": label or "Open locum request",
            "action_type": "open_locum_form",
            "payload": cleaned_payload,
        }

    return None


def _default_quick_actions(workspace: dict, hospital_site: str | None) -> list[dict]:
    actions: list[dict] = []
    approval_overview = workspace.get("compliance", {}).get("approval_overview", {})
    shortfalls = workspace.get("rota_planning", {}).get("ward_shortfalls", [])
    if hospital_site:
        shortfalls = [item for item in shortfalls if _extract_site_name(item) == hospital_site]

    if approval_overview.get("pending_locum_approvals"):
        actions.append(
            {
                "label": "Open compliance queue",
                "action_type": "navigate",
                "payload": {"tab": "compliance"},
            }
        )

    if shortfalls:
        shortfall = shortfalls[0]
        actions.append(
            {
                "label": f"Raise cover for {shortfall.get('ward', 'gap')}",
                "action_type": "open_locum_form",
                "payload": {
                    "hospital_site": shortfall.get("site") or shortfall.get("hospital_site") or hospital_site or "Wythenshawe Hospital",
                    "department": shortfall.get("department") or "Medicine",
                    "ward": shortfall.get("ward") or "Unallocated Ward",
                    "requested_date": date.today().isoformat(),
                    "shift_code": shortfall.get("shift_code") or "LONG_DAY",
                    "required_grade": shortfall.get("required_grade") or "FY2",
                    "compliance_level": "ENHANCED",
                    "staff_type": "BANK",
                    "approval_required": True,
                    "requested_hours": 10,
                    "shortage_reason": f"Copilot escalation for {shortfall.get('shift_name', 'open shift')} cover gap.",
                    "requested_by": "AI Copilot",
                },
            }
        )

    latest_schedule_id = workspace.get("summary", {}).get("latest_schedule_id")
    if latest_schedule_id:
        actions.append(
            {
                "label": "Open latest reports",
                "action_type": "open_reports",
                "payload": {
                    "schedule_id": latest_schedule_id,
                    "hospital_filter": hospital_site or "all",
                },
            }
        )

    return actions[:3]


def _build_fallback_response(message: str, workspace: dict, hospital_site: str | None) -> dict:
    lowered = message.lower()
    approval_overview = workspace.get("compliance", {}).get("approval_overview", {})
    shortfalls = workspace.get("rota_planning", {}).get("ward_shortfalls", [])
    leave_events = workspace.get("leave_events", [])
    locum_requests = workspace.get("locum_requests", [])
    escalation_flags = workspace.get("compliance", {}).get("escalation_flags", [])

    if hospital_site:
        shortfalls = [item for item in shortfalls if _extract_site_name(item) == hospital_site]
        leave_events = [item for item in leave_events if item.get("hospital_site") == hospital_site]
        locum_requests = [item for item in locum_requests if item.get("hospital_site") == hospital_site]
        escalation_flags = [item for item in escalation_flags if item.get("site") == hospital_site]

    risk_level = "ROUTINE"
    headline = "Operational snapshot"
    answer_lines: list[str] = []

    if escalation_flags or approval_overview.get("breached_items"):
        risk_level = "URGENT"
        headline = "Escalations need immediate attention"
        answer_lines.append(
            f"There are {approval_overview.get('breached_items', 0)} breached approval items and {len(escalation_flags)} live escalation flags."
        )
        if escalation_flags:
            first_flag = escalation_flags[0]
            answer_lines.append(
                f"The first flagged item is {first_flag.get('title', 'an approval item')} at {first_flag.get('site', 'an unknown site')} with status {first_flag.get('status', 'UNKNOWN')}."
            )
    elif "sick" in lowered or "sickness" in lowered:
        headline = "Active sickness view"
        sickness_events = [item for item in leave_events if item.get("event_type") == "SICKNESS" and item.get("end_date", "") >= date.today().isoformat()]
        answer_lines.append(f"There are {len(sickness_events)} active sickness events in the current view.")
        if sickness_events:
            sample_names = ", ".join(item.get("doctor_name", "Unknown") for item in sickness_events[:4])
            answer_lines.append(f"Current sickness coverage includes {sample_names}.")
    elif "locum" in lowered or "bank" in lowered or "cost" in lowered:
        risk_level = "WATCH" if approval_overview.get("pending_locum_approvals", 0) else "ROUTINE"
        headline = "Locum demand summary"
        pending_requests = [item for item in locum_requests if item.get("approval_status") == "PENDING_APPROVAL"]
        total_cost = round(sum(float(item.get("estimated_cost") or 0) for item in locum_requests), 2)
        answer_lines.append(
            f"There are {len(pending_requests)} pending locum approvals with an estimated active pipeline cost of GBP {total_cost:,.0f}."
        )
        if pending_requests:
            first_request = pending_requests[0]
            answer_lines.append(
                f"The next request to review is {first_request.get('ward', 'an open ward')} at {first_request.get('hospital_site', 'an unknown site')} for a {first_request.get('required_grade', 'doctor')} on the {first_request.get('shift_name', 'shift')}."
            )
    elif "compliance" in lowered or "approval" in lowered or "risk" in lowered:
        risk_level = "WATCH" if approval_overview.get("at_risk_items", 0) or approval_overview.get("finance_signoffs", 0) else "ROUTINE"
        headline = "Compliance queue summary"
        answer_lines.append(
            f"Pending locum approvals: {approval_overview.get('pending_locum_approvals', 0)}. Pending shift swaps: {approval_overview.get('pending_shift_swaps', 0)}. Finance checks: {approval_overview.get('finance_signoffs', 0)}."
        )
        answer_lines.append(
            f"At-risk items: {approval_overview.get('at_risk_items', 0)}. Overdue items: {approval_overview.get('overdue_items', 0)}."
        )
    else:
        risk_level = "WATCH" if shortfalls or approval_overview.get("pending_locum_approvals", 0) else "ROUTINE"
        answer_lines.append(
            f"The workspace is tracking {workspace.get('summary', {}).get('doctor_count', 0)} doctors, {workspace.get('summary', {}).get('active_absence_count', 0)} active availability events, and {workspace.get('summary', {}).get('pending_locum_requests', 0)} pending locum requests."
        )
        if shortfalls:
            first_shortfall = shortfalls[0]
            answer_lines.append(
                f"The biggest current cover gap is {first_shortfall.get('ward', 'an open ward')} at {first_shortfall.get('site', 'an unknown site')} for {first_shortfall.get('shift_name', 'a shift')} requiring {first_shortfall.get('required_grade', 'doctor-grade')} cover."
            )

    if not answer_lines:
        answer_lines.append("The current workspace does not show a pressing issue in this view, but I can still help you inspect locums, leave, board gaps, or compliance.")

    return {
        "mode": "fallback",
        "configured": False,
        "headline": headline,
        "answer": " ".join(answer_lines),
        "risk_level": risk_level,
        "quick_actions": _default_quick_actions(workspace, hospital_site),
        "follow_up_questions": [
            "Which locum approvals should I deal with first?",
            "Show me the riskiest shift gaps by hospital site.",
            "Summarise current leave and sickness pressure.",
        ],
    }


def _extract_output_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str) and payload.get("output_text").strip():
        return payload["output_text"].strip()

    text_parts: list[str] = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict):
                text_value = content.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    text_parts.append(text_value.strip())
    return "\n".join(text_parts).strip()


def _extract_json_block(raw_text: str) -> dict | None:
    if not raw_text:
        return None

    try:
        parsed = json.loads(raw_text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def _call_openai_copilot(message: str, context_snapshot: dict) -> dict | None:
    if not settings.openai_api_key.strip():
        return None

    instructions = (
        "You are Med Rota AI Copilot, an NHS medical staffing assistant. "
        "Base every statement only on the supplied workspace data. "
        "Do not invent doctors, shifts, approvals, or financial controls. "
        "You may recommend actions, but never claim to approve, book, or override governance. "
        "Return JSON only with keys: headline, answer, risk_level, quick_actions, follow_up_questions. "
        "risk_level must be one of ROUTINE, WATCH, URGENT. "
        "quick_actions must be an array of up to 3 actions. "
        "Each action must contain label, action_type, and payload. "
        "Allowed action_type values are navigate, open_locum_form, and open_reports. "
        "For navigate, payload must include tab. "
        "For open_locum_form, payload should include hospital_site and any known shift details. "
        "For open_reports, payload may include schedule_id and hospital_filter. "
        "Keep the answer concise and operational."
    )

    user_prompt = (
        f"User question: {message}\n"
        f"Workspace context JSON:\n{json.dumps(context_snapshot, default=str)}"
    )

    with httpx.Client(timeout=settings.openai_timeout_seconds) as client:
        response = client.post(
            f"{settings.openai_base_url.rstrip('/')}/responses",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.openai_model,
                "instructions": instructions,
                "input": user_prompt,
                "max_output_tokens": 900,
            },
        )
        response.raise_for_status()

    raw_text = _extract_output_text(response.json())
    parsed = _extract_json_block(raw_text)
    if not parsed:
        return None
    return parsed


def _normalise_copilot_response(raw_response: dict | None, workspace: dict, hospital_site: str | None) -> dict:
    fallback = _build_fallback_response("summary", workspace, hospital_site)
    if not isinstance(raw_response, dict):
        return fallback

    quick_actions = []
    for action in raw_response.get("quick_actions", []):
        normalised = _normalise_action(action)
        if normalised:
            quick_actions.append(normalised)

    follow_ups = [
        str(item).strip()
        for item in raw_response.get("follow_up_questions", [])
        if str(item).strip()
    ][:3]

    return {
        "mode": "live_ai",
        "configured": True,
        "headline": str(raw_response.get("headline") or fallback["headline"]).strip(),
        "answer": str(raw_response.get("answer") or fallback["answer"]).strip(),
        "risk_level": str(raw_response.get("risk_level") or fallback["risk_level"]).upper(),
        "quick_actions": quick_actions or fallback["quick_actions"],
        "follow_up_questions": follow_ups or fallback["follow_up_questions"],
    }


@router.get("/status", response_model=CopilotStatusResponse)
def get_copilot_status():
    mode = _copilot_mode()
    return {
        "configured": mode == "live_ai",
        "mode": mode,
        "model": settings.openai_model,
        "starter_prompts": STARTER_PROMPTS,
        "guardrails": GUARDRAILS,
    }


@router.post("/query", response_model=CopilotQueryResponse)
def query_copilot(payload: CopilotQueryRequest, db: Session = Depends(get_db)):
    workspace = build_operations_workspace_payload(db)
    effective_hospital_site = payload.hospital_site or _detect_site_from_message(payload.message, workspace)
    context_snapshot = _workspace_context_snapshot(
        workspace,
        effective_hospital_site,
        payload.schedule_id,
        payload.active_module,
    )

    live_response = None
    mode = _copilot_mode()
    if mode == "live_ai":
        try:
            live_response = _call_openai_copilot(payload.message, context_snapshot)
        except Exception:
            live_response = None

    response_payload = (
        _normalise_copilot_response(live_response, workspace, effective_hospital_site)
        if live_response
        else _build_fallback_response(payload.message, workspace, effective_hospital_site)
    )

    _record_audit_event(
        db,
        entity_type="copilot_query",
        entity_id=str(uuid.uuid4()),
        action="ANSWERED",
        hospital_site=effective_hospital_site,
        actor_name="AI Copilot",
        summary=f"Copilot responded to: {payload.message[:80]}",
        detail=response_payload.get("headline"),
    )
    db.commit()

    return response_payload
