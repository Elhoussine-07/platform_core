# -*- coding: utf-8 -*-
"""
platform_core/api/moderation.py

API Frontend — Espace Modérateur (CDC v5, §1.3.1, §2.1.1, §2.5.2, §2.2.6).

Couvre :
    get_moderation_tasks, approve_review, reject_review,
    validate_suspension, refuse_suspension, resolve_dispute,
    approve_agency_join, reject_agency_join, validate_completion

DUPLICATION SIGNALÉE : validate_completion() ici fait exactement la même
chose que project.py::validate_project_completion() (même workflow de
validation modérateur de fin de projet). C'est la 6e duplication de ce
type dans le projet — à trancher : lequel des deux fichiers doit être la
version canonique ?

PÉRIMÈTRE PARTIEL SIGNALÉ : approve_agency_join()/reject_agency_join() ne
couvrent que le chemin "validation par un modérateur" (§2.1.1, cas d'une
agence à un seul membre, ou escalade). Le chemin normal "validation par
un collaborateur déjà membre" n'existe dans aucun fichier livré jusqu'ici
(ni dans agency.py) — à ajouter séparément si besoin.

NOTE : ModerationTask.task_type n'a pas d'entrée dédiée pour les avis
(AgencyReview) dans l'énumération fournie (Validation, Suspension,
Validation Terminé, Annulation Devis, Rattachement Agence, Litige Client
Inactif, Escalade Sans Réponse). approve_review()/reject_review() opèrent
donc directement sur le statut d'AgencyReview, SANS passer par la file
ModerationTask — get_moderation_tasks() ne les listera donc pas ; il faut
une liste séparée des avis "Pending" côté frontend modération.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, cint, flt


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _require_moderator():
    user = frappe.session.user
    if not frappe.has_role("Moderator") and not frappe.has_role("Administrator"):
        frappe.throw(_("Action réservée aux modérateurs."), frappe.PermissionError)
    return user


def _notify(user, ntype, title, message, reference_doctype=None,
            reference_name=None, action_required=False, agency_context=None):
    if not user:
        return
    frappe.get_doc({
        "doctype": "Notification", "user": user, "type": ntype, "title": title,
        "message": message, "reference_doctype": reference_doctype,
        "reference_name": reference_name, "agency_context": agency_context,
        "action_required": cint(action_required), "is_read": 0,
        "created_date": now_datetime(),
    }).insert(ignore_permissions=True)


def _close_moderation_task(reference_doctype, reference_name, task_type, approved, decision_note=None):
    """Clôture la ModerationTask associée si elle existe (idempotent : ignore si absente)."""
    task_name = frappe.db.get_value(
        "ModerationTask",
        {"reference_doctype": reference_doctype, "reference_name": reference_name,
         "task_type": task_type, "status": "Open"},
        "name",
    )
    if not task_name:
        return None

    task = frappe.get_doc("ModerationTask", task_name)
    task.status = "Approved" if approved else "Rejected"
    task.moderator = frappe.session.user
    task.decision_note = decision_note
    task.decision_date = now_datetime()
    task.save(ignore_permissions=True)
    return task.name


_REFERENCE_LABEL_RESOLVERS = {
    "Project": ("title",),
    "AgencyProfile": ("agency_name",),
    "ProjectSuspension": None,  # résolu via le projet lié
    "InactivityDispute": None,  # résolu via le projet lié
    "AgencyJoinRequest": None,  # résolu via l'agence liée
    "Proposal": None,           # résolu via le projet lié
}


def _resolve_reference_label(reference_doctype, reference_name):
    """Retourne un libellé lisible pour affichage dans la liste des tâches."""
    try:
        if reference_doctype == "Project":
            return frappe.db.get_value("Project", reference_name, "title")
        if reference_doctype == "AgencyProfile":
            return frappe.db.get_value("AgencyProfile", reference_name, "agency_name")
        if reference_doctype == "ProjectSuspension":
            project = frappe.db.get_value("ProjectSuspension", reference_name, "project")
            return frappe.db.get_value("Project", project, "title") if project else reference_name
        if reference_doctype == "InactivityDispute":
            project = frappe.db.get_value("InactivityDispute", reference_name, "project")
            return frappe.db.get_value("Project", project, "title") if project else reference_name
        if reference_doctype == "AgencyJoinRequest":
            agency = frappe.db.get_value("AgencyJoinRequest", reference_name, "agency")
            return frappe.db.get_value("AgencyProfile", agency, "agency_name") if agency else reference_name
        if reference_doctype == "Proposal":
            project = frappe.db.get_value("Proposal", reference_name, "project")
            return frappe.db.get_value("Project", project, "title") if project else reference_name
    except Exception:
        pass
    return reference_name


# ---------------------------------------------------------------------------
# 1. get_moderation_tasks
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_moderation_tasks(task_type=None, status="Open", limit_start=0,
                          limit_page_length=20, order_by="creation asc"):
    """
    File d'attente de modération (ModerationTask). Portée globale (pas de
    restriction d'ownership) : réservé aux modérateurs/administrateurs.

    NB : n'inclut PAS les avis (AgencyReview) — cf. note en tête de
    fichier, ils sont modérés directement via leur propre statut.
    """
    _require_moderator()

    filters = {}
    if status:
        filters["status"] = status
    if task_type:
        filters["task_type"] = task_type

    tasks = frappe.get_all(
        "ModerationTask",
        filters=filters,
        fields=["name", "task_type", "reference_doctype", "reference_name",
                "status", "moderator", "decision_note", "decision_date", "creation"],
        limit_start=cint(limit_start),
        limit_page_length=cint(limit_page_length),
        order_by=order_by,
    )

    for t in tasks:
        t["reference_label"] = _resolve_reference_label(t.reference_doctype, t.reference_name)

    return tasks


# ---------------------------------------------------------------------------
# 2-3. Avis (AgencyReview) — §2.2.6
# ---------------------------------------------------------------------------

@frappe.whitelist()
def approve_review(review):
    """
    Approuve un avis client -> agence (§2.2.6). Marque également l'avis
    comme vérifié (anti-faux-avis). Le recalcul de AgencyProfile.rating/
    reviews_count est supposé géré par un hook sur AgencyReview.on_update
    (pas ici, pour éviter la duplication de logique de recalcul).
    """
    _require_moderator()

    doc = frappe.get_doc("AgencyReview", review)
    if doc.status != "Pending":
        frappe.throw(_("Seul un avis en attente peut être approuvé."))

    doc.status = "Approved"
    doc.is_verified = 1
    doc.save(ignore_permissions=True)

    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def reject_review(review, reason=None):
    """Rejette un avis client -> agence (§2.2.6)."""
    _require_moderator()

    doc = frappe.get_doc("AgencyReview", review)
    if doc.status != "Pending":
        frappe.throw(_("Seul un avis en attente peut être rejeté."))

    doc.status = "Rejected"
    doc.save(ignore_permissions=True)

    client_user = frappe.db.get_value("ClientProfile", doc.client, "user")
    _notify(
        user=client_user, ntype="Review",
        title=_("Avis non publié"),
        message=reason or _("Votre avis n'a pas été validé par la modération."),
        reference_doctype="AgencyReview", reference_name=doc.name,
    )

    return {"name": doc.name, "status": doc.status}


# ---------------------------------------------------------------------------
# 4-5. Suspensions de projet — §1.3.1
# ---------------------------------------------------------------------------

@frappe.whitelist()
def validate_suspension(suspension):
    """
    Valide une demande de suspension (§1.3.1). C'est CE moment — pas la
    demande initiale — qui marque le début du décompte du délai de
    suspension ("le temps réellement passé en pause ne doit jamais être
    compté dans le délai contractuel").
    """
    _require_moderator()

    doc = frappe.get_doc("ProjectSuspension", suspension)
    if doc.status != "Requested":
        frappe.throw(_("Cette demande n'est plus en attente (statut : {0}).").format(doc.status))

    doc.status = "Validated"
    doc.validation_date = now_datetime()
    doc.moderator = frappe.session.user
    doc.save(ignore_permissions=True)

    project = frappe.get_doc("Project", doc.project)
    project.status = "Suspended"
    project.save(ignore_permissions=True)

    _close_moderation_task("ProjectSuspension", suspension, "Suspension", approved=True)

    requester_field = "client" if doc.requested_by == "Client" else None
    if requester_field:
        client_user = frappe.db.get_value(
            "ClientProfile", frappe.db.get_value("Project", doc.project, "client"), "user"
        )
        _notify(client_user, "Project", _("Suspension validée"),
                _("La suspension de votre projet « {0} » a été validée.").format(project.title),
                "Project", project.name)

    return {"name": doc.name, "status": doc.status, "project_status": project.status}


@frappe.whitelist()
def refuse_suspension(suspension, reason=None):
    """
    Refuse une demande de suspension. NB : ProjectSuspension ne possède
    pas de champ dédié pour stocker un motif de refus dans le schéma
    fourni — `reason` n'est utilisé que pour la notification, non
    persisté sur le document lui-même.
    """
    _require_moderator()

    doc = frappe.get_doc("ProjectSuspension", suspension)
    if doc.status != "Requested":
        frappe.throw(_("Cette demande n'est plus en attente (statut : {0}).").format(doc.status))

    doc.status = "Refused"
    doc.moderator = frappe.session.user
    doc.save(ignore_permissions=True)

    _close_moderation_task("ProjectSuspension", suspension, "Suspension", approved=False,
                            decision_note=reason)

    project_title = frappe.db.get_value("Project", doc.project, "title")
    client_user = frappe.db.get_value(
        "ClientProfile", frappe.db.get_value("Project", doc.project, "client"), "user"
    )
    _notify(client_user, "Project", _("Demande de suspension refusée"),
            reason or _("Votre demande de suspension pour « {0} » a été refusée.").format(project_title),
            "Project", doc.project)

    return {"name": doc.name, "status": doc.status}


# ---------------------------------------------------------------------------
# 6. Litige client inactif — §2.5.2
# ---------------------------------------------------------------------------

@frappe.whitelist()
def resolve_dispute(dispute, founded=1, decision_note=None):
    """
    Résout un litige pour client inactif après paiement (§2.5.2).

    - Fondé (founded=1) : Project -> Rejected/"Client inactif" ; la
      commission déjà prélevée est créditée à l'agence via CommissionCredit
      (sans limite de validité dans le temps).
    - Non fondé (founded=0) : le projet reprend son cours normal
      (retour à "In Progress"), aucun crédit appliqué.
    """
    _require_moderator()
    founded = cint(founded)

    doc = frappe.get_doc("InactivityDispute", dispute)
    if doc.status not in ("Submitted", "Under Review"):
        frappe.throw(_("Ce litige a déjà été résolu (statut : {0}).").format(doc.status))

    doc.status = "Founded" if founded else "Not Founded"
    doc.moderator = frappe.session.user
    doc.decision_date = now_datetime()
    doc.decision_note = decision_note
    doc.save(ignore_permissions=True)

    project = frappe.get_doc("Project", doc.project)

    if founded:
        project.status = "Rejected"
        project.rejection_substatus = "Client inactif"
        project.save(ignore_permissions=True)

        invoice = frappe.get_all(
            "Invoice",
            filters={"project": doc.project, "agency": doc.agency},
            fields=["name", "commission_amount"],
            order_by="creation desc",
            limit=1,
        )
        if invoice and flt(invoice[0].commission_amount) > 0:
            credit = frappe.get_doc({
                "doctype": "CommissionCredit",
                "agency": doc.agency,
                "source_dispute": doc.name,
                "source_project": doc.project,
                "amount": invoice[0].commission_amount,
                "consumed_amount": 0,
                "balance": invoice[0].commission_amount,
            })
            credit.insert(ignore_permissions=True)
        else:
            frappe.log_error(
                title="Litige fondé sans facture de commission trouvée",
                message=f"Dispute {dispute}, project {doc.project}, agency {doc.agency}",
            )
    else:
        project.status = "In Progress"
        project.save(ignore_permissions=True)

    _close_moderation_task("InactivityDispute", dispute, "Litige Client Inactif",
                            approved=bool(founded), decision_note=decision_note)

    agency_users = frappe.get_all(
        "AgencyMember", filters={"agency": doc.agency, "status": "Active"}, pluck="user"
    )
    for u in agency_users:
        _notify(
            u, "Project",
            _("Litige résolu") if founded else _("Litige rejeté"),
            _("Le litige concernant le projet « {0} » a été jugé {1}.").format(
                project.title, _("fondé") if founded else _("non fondé")
            ),
            "Project", project.name, agency_context=doc.agency,
        )

    return {"name": doc.name, "status": doc.status, "project_status": project.status}


# ---------------------------------------------------------------------------
# 7-8. Rattachement agence — §2.1.1
# ---------------------------------------------------------------------------

@frappe.whitelist()
def approve_agency_join(request):
    """
    Approuve une demande de rattachement (§2.1.1) — chemin modérateur
    uniquement (cas agence à un seul membre / escalade). Crée l'AgencyMember.
    """
    _require_moderator()

    doc = frappe.get_doc("AgencyJoinRequest", request)
    if doc.status != "Pending":
        frappe.throw(_("Cette demande n'est plus en attente (statut : {0}).").format(doc.status))

    doc.status = "Approved"
    doc.decided_by = frappe.session.user
    doc.decision_date = now_datetime()
    doc.save(ignore_permissions=True)

    member = frappe.get_doc({
        "doctype": "AgencyMember",
        "user": doc.user,
        "agency": doc.agency,
        "member_role": "Member",
        "status": "Active",
        "joined_on": frappe.utils.nowdate(),
    })
    member.insert(ignore_permissions=True)

    _close_moderation_task("AgencyJoinRequest", request, "Rattachement Agence", approved=True)

    agency_name = frappe.db.get_value("AgencyProfile", doc.agency, "agency_name")
    _notify(doc.user, "System Alert", _("Rattachement approuvé"),
            _("Votre demande de rattachement à {0} a été approuvée.").format(agency_name),
            "AgencyMember", member.name, agency_context=doc.agency)

    return {"name": doc.name, "status": doc.status, "member": member.name}


@frappe.whitelist()
def reject_agency_join(request, reason=None):
    """Rejette une demande de rattachement (§2.1.1) — chemin modérateur."""
    _require_moderator()

    doc = frappe.get_doc("AgencyJoinRequest", request)
    if doc.status != "Pending":
        frappe.throw(_("Cette demande n'est plus en attente (statut : {0}).").format(doc.status))

    doc.status = "Rejected"
    doc.decided_by = frappe.session.user
    doc.decision_date = now_datetime()
    doc.rejection_reason = reason
    doc.save(ignore_permissions=True)

    _close_moderation_task("AgencyJoinRequest", request, "Rattachement Agence", approved=False,
                            decision_note=reason)

    agency_name = frappe.db.get_value("AgencyProfile", doc.agency, "agency_name")
    _notify(doc.user, "System Alert", _("Rattachement refusé"),
            reason or _("Votre demande de rattachement à {0} a été refusée.").format(agency_name),
            "AgencyJoinRequest", doc.name)

    return {"name": doc.name, "status": doc.status}


# ---------------------------------------------------------------------------
# 9. validate_completion — DUPLICATION cf. note en tête de fichier
# ---------------------------------------------------------------------------

@frappe.whitelist()
def validate_completion(project, approve=1, decision_note=None):
    """
    Valide (ou refuse) le passage définitif d'un projet à "Completed"
    (§1.3.1). Identique à project.py::validate_project_completion() —
    DUPLICATION VOLONTAIRE le temps de trancher quel fichier fait foi.
    """
    _require_moderator()
    approve = cint(approve)

    doc = frappe.get_doc("Project", project)

    if doc.status != "In Progress":
        frappe.throw(_("Ce projet n'est pas en cours ; validation impossible."))

    if not doc.completion_confirmed_by_client:
        frappe.throw(_("Le client n'a pas encore confirmé la fin du projet."))

    if approve:
        doc.completion_validated_by_moderator = 1
        doc.status = "Completed"
        doc.save(ignore_permissions=True)

        client_user = frappe.db.get_value("ClientProfile", doc.client, "user")
        _notify(client_user, "Project", _("Projet terminé"),
                _("Votre projet {0} a été validé comme terminé.").format(doc.title),
                "Project", doc.name)

        agency = frappe.db.get_value("Opportunity", {"project": project, "status": "Gagnée"}, "agency")
        if agency:
            agency_owner = frappe.db.get_value(
                "AgencyMember", {"agency": agency, "member_role": "Owner"}, "user"
            )
            _notify(agency_owner, "Project", _("Projet terminé"),
                    _("Le projet {0} a été validé comme terminé.").format(doc.title),
                    "Project", doc.name, agency_context=agency)
    else:
        doc.completion_confirmed_by_client = 0
        doc.save(ignore_permissions=True)

    _close_moderation_task("Project", project, "Validation Terminé", approved=bool(approve),
                            decision_note=decision_note)

    return {"name": doc.name, "status": doc.status, "approved": bool(approve)}
