"""Small evidence graph for cross-party requirements; no document contents or clinical conclusions.

A requirement belongs to one requester (supplier, PCP or patient). Only the requester can accept;
any other external party can provide. Sharing the patient's information with a third-party requester
needs her permission for that exact reference; answering the patient's own question does not.
"""

from mira.coordinator.models import (
    CaseDocument,
    DocumentNode,
    FollowupTask,
    IncomingEvent,
    KnowledgeGraph,
    Party,
    RequirementNode,
)
from mira.models import CoordinationObservation


def _actor(requirement: RequirementNode, party: Party) -> str:
    return requirement.requester if party.value == requirement.requester_role else party.value


def _needs_patient_permission(requirement: RequirementNode) -> bool:
    return requirement.requester_role != Party.PATIENT


def observe(doc: CaseDocument, task: FollowupTask, event: IncomingEvent) -> tuple[KnowledgeGraph, str | None]:
    graph = doc.knowledge
    observation = event.observation
    if observation is None or task.task_id not in graph.requirements:
        raise ValueError("Observation must belong to an existing requirement")
    requirement = graph.requirements[task.task_id]
    documents = dict(graph.documents)
    edges = list(graph.edges)
    review = None
    actor = _actor(requirement, event.party)
    edges.append({"source": actor, "relation": "reported", "target": task.task_id, "evidence": event.event_id})
    if observation.clinical_decision_required:
        review = "Clinical interpretation or prescription decision requires an authorized human"
    if event.party.value == requirement.requester_role:
        requirement = _record_requester_response(requirement, observation, event)
        if requirement.accepted_event_id == event.event_id:
            edges.append({"source": actor, "relation": "accepted", "target": requirement.accepted_reference,
                          "requirement": task.task_id, "evidence": event.event_id})
    else:
        requirement, provider_review = _record_provider_response(doc, task, requirement, documents, edges, event, actor)
        review = review or provider_review
    return graph.model_copy(update={"requirements": {**graph.requirements, task.task_id: requirement},
                                    "documents": documents, "edges": edges}), review


def _record_requester_response(requirement: RequirementNode, observation: CoordinationObservation,
                               event: IncomingEvent) -> RequirementNode:
    if observation.requester_accepted is True:
        reference = observation.accepted_reference
        exact = reference and reference == requirement.current_reference == requirement.forwarded_reference
        if exact and reference == requirement.patient_confirmed_reference:
            return requirement.model_copy(update={"status": "accepted", "accepted_reference": reference,
                                                  "accepted_event_id": event.event_id})
        return requirement.model_copy(update={"status": "acceptance_unconfirmed"})
    if observation.requester_accepted is False:
        return requirement.model_copy(update={"status": "requester_rejected", "accepted_reference": None,
                                              "accepted_event_id": None})
    return requirement


def _record_provider_response(doc: CaseDocument, task: FollowupTask, requirement: RequirementNode,
                              documents: dict[str, DocumentNode], edges: list[dict], event: IncomingEvent,
                              actor: str) -> tuple[RequirementNode, str | None]:
    """Mutates the local `documents`/`edges` copies built by `observe`; returns the updated requirement."""
    observation = event.observation
    review = None
    reference = observation.document_reference or (event.event_id if observation.information else None)
    if reference:
        if reference != requirement.current_reference:
            requirement = requirement.model_copy(update={
                "current_reference": reference, "patient_confirmed_reference": None,
                "forwarded_reference": None, "accepted_reference": None, "accepted_event_id": None,
                "status": "provided",
            })
        if not _needs_patient_permission(requirement):
            requirement = requirement.model_copy(update={"patient_confirmed_reference": reference})
        if observation.document_reference:
            review = _record_document(doc, task, documents, event, reference)
        edges.append({"source": actor, "relation": "provided", "target": reference, "evidence": event.event_id})
    if event.party is Party.PATIENT and observation.sharing_authorized is True:
        confirmed = observation.confirmed_reference or reference
        if confirmed and confirmed == requirement.current_reference:
            requirement = requirement.model_copy(update={"patient_confirmed_reference": confirmed})
            edges.append({"source": "patient", "relation": "authorized_for_requester",
                          "target": confirmed, "requirement": task.task_id, "evidence": event.event_id})
    elif event.party is Party.PATIENT and observation.sharing_authorized is False:
        requirement = requirement.model_copy(update={"patient_confirmed_reference": None,
            "accepted_reference": None, "accepted_event_id": None})
        review = "Patient declined or revoked sharing permission"
    if observation.needs_provider:
        requirement = requirement.model_copy(update={"status": "awaiting_provider"})
    return requirement, review


def _record_document(doc: CaseDocument, task: FollowupTask, documents: dict[str, DocumentNode],
                     event: IncomingEvent, reference: str) -> str | None:
    observation = event.observation
    old = documents.get(reference)
    owner = observation.document_patient_name or (old.patient_name if old else None)
    document_type = observation.document_type or (old.document_type if old else None)
    mismatch = bool(owner and owner.strip().casefold() != doc.patient_name.strip().casefold())
    expected_type = task.requirement_request.requirement.document_type if task.requirement_request else None
    mismatch = mismatch or bool(expected_type and document_type and expected_type.casefold() != document_type.casefold())
    documents[reference] = DocumentNode(
        reference=reference, patient_name=owner, document_type=document_type,
        # A patient re-confirming a reference keeps the original provider as its source.
        source_event_id=event.event_id if not old or event.party is not Party.PATIENT else old.source_event_id,
        verification_status="metadata_mismatch" if mismatch else "not_checked",
    )
    return "Document metadata conflicts with the requested patient or document type" if mismatch else None


def forward(doc: CaseDocument, task: FollowupTask, evidence: dict | None) -> KnowledgeGraph:
    requirement = doc.knowledge.requirements[task.task_id]
    if not evidence or evidence["party"] in {Party.HUMAN, requirement.requester_role}:
        raise ValueError("Forwarding requires source evidence from a party other than the requester")
    observation = evidence.get("observation") or {}
    reference = observation.get("document_reference") or (evidence["event_id"] if observation.get("information") else None)
    if not reference or reference != requirement.current_reference:
        raise ValueError("Forward only the current evidence reference, never a superseded reference")
    if requirement.patient_confirmed_reference != reference:
        raise ValueError("Patient must confirm this exact information/document for this requester before forwarding")
    document = doc.knowledge.documents.get(reference)
    expected_type = task.requirement_request.requirement.document_type
    if expected_type and document is None:
        raise ValueError("A document requirement cannot be satisfied by plain information")
    if document:
        if not document.patient_name or document.patient_name.strip().casefold() != doc.patient_name.strip().casefold():
            raise ValueError("Document patient metadata is missing or conflicts")
        if expected_type and (not document.document_type or expected_type.casefold() != document.document_type.casefold()):
            raise ValueError("Requested document-type metadata must match before forwarding")
    requirement = requirement.model_copy(update={"forwarded_reference": reference, "status": "forwarded_unverified"})
    return doc.knowledge.model_copy(update={
        "requirements": {**doc.knowledge.requirements, task.task_id: requirement},
        "edges": [*doc.knowledge.edges, {"source": reference, "relation": "forwarded_for_requester_review",
            "target": requirement.requester, "requirement": task.task_id,
            "evidence": evidence["event_id"]}],
    })
