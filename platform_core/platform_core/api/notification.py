# -*- coding: utf-8 -*-
"""
api/notification.py
====================
APIs Frontend (notifications actives + historique des notifications).

Références CDC v5 :
- Module 7 - Historique des notifications [NOUVEAU v5]
  - 7.1 Principe d'affichage : seules les notifications NON consultées
    s'affichent dans la vue active (Mes Projets / Opportunités). Dès
    consultation, la notification disparaît de la vue active et est
    archivée automatiquement (jamais supprimée définitivement).
  - 7.2 Contenu de l'historique : liste chronologique décroissante,
    filtrable par type, recherche textuelle, statut de lecture conservé.
  - 7.3 Accès : bouton dédié (icône cloche), indépendant du contexte
    d'agence actif pour un utilisateur multi-agences.

Référence doctypes_final.pdf : Notification (user, type, title, message,
link, reference_doctype, reference_name, agency_context, action_required,
is_read, is_archived, created_date, archived_date).
"""

import frappe
from frappe import _
from frappe.utils import now_datetime


NOTIFICATION_TYPES = ["Project", "Proposal", "Message", "Review", "System", "Alert"]


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _get_active_agency(user=None):
    """
    Retourne le nom (AgencyProfile) de l'agence active pour l'utilisateur
    connecté, selon le contexte de bascule multi-agences (2.1.1).
    Convention : cache Redis `active_agency:<user>` positionné par
    l'API de switch d'agence (cf. api/agency.py). Retourne None si
    l'utilisateur n'a pas de rattachement agence (compte Client pur).
    """
    user = user or frappe.session.user
    cached = frappe.cache().get_value(f"active_agency:{user}")
    if cached:
        return cached

    memberships = frappe.get_all(
        "AgencyMember", filters={"user": user, "status": "Active"}, pluck="agency",
    )
    if not memberships:
        return None
    if len(memberships) == 1:
        return memberships[0]
    # Plusieurs agences rattachées mais aucun contexte actif choisi :
    # on ne peut pas trancher côté notifications -> pas de filtre agence.
    return None


def _base_filters(user, agency_context_filter=True):
    """
    Filtres de base communs à toutes les requêtes de notifications :
    - toujours restreint à l'utilisateur courant (if_owner)
    - si l'utilisateur a un contexte d'agence actif, restreint aux
      notifications sans agency_context (génériques) OU liées à cette
      agence précise (7.3 - cohérence avec le switch multi-agences)
    """
    filters = [["Notification", "user", "=", user]]

    if agency_context_filter:
        agency = _get_active_agency(user)
        if agency:
            filters.append(
                ["Notification", "agency_context", "in", ["", None, agency]]
            )
    return filters


# ---------------------------------------------------------------------------
# 1. get_notifications - vue active (non consultées uniquement)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_notifications(notif_type=None, page=1, page_size=20):
    """
    Notifications actives non consultées de l'utilisateur (7.1), telles
    qu'affichées dans Mes Projets (Client) ou Opportunités (Agence).
    Dès qu'une notification est ouverte, elle doit être marquée lue via
    `mark_notification_read` pour disparaître de cette vue.
    """
    user = frappe.session.user
    page = int(page)
    page_size = int(page_size)

    filters = _base_filters(user)
    filters.append(["Notification", "is_archived", "=", 0])
    if notif_type:
        if notif_type not in NOTIFICATION_TYPES:
            frappe.throw(_("Type de notification invalide."))
        filters.append(["Notification", "type", "=", notif_type])

    notifications = frappe.get_all(
        "Notification",
        filters=filters,
        fields=[
            "name", "type", "title", "message", "link", "reference_doctype",
            "reference_name", "agency_context", "action_required", "is_read",
            "created_date",
        ],
        order_by="created_date desc",
        start=(page - 1) * page_size,
        page_length=page_size,
    )
    total_count = frappe.db.count("Notification", filters=filters)

    return {
        "notifications": notifications,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
    }


# ---------------------------------------------------------------------------
# 2. mark_notification_read
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def mark_notification_read(notification_name):
    """
    Marque une notification comme consultée. Conformément à 7.1, la
    consultation déclenche à la fois `is_read=1` ET l'archivage automatique
    (`is_archived=1`, `archived_date`=maintenant) : la notification quitte
    la vue active et devient consultable uniquement dans l'historique (7.2).
    """
    user = frappe.session.user
    owner = frappe.db.get_value("Notification", notification_name, "user")
    if not owner:
        frappe.throw(_("Notification introuvable."), frappe.DoesNotExistError)
    if owner != user:
        frappe.throw(_("Vous n'avez pas accès à cette notification."), frappe.PermissionError)

    frappe.db.set_value("Notification", notification_name, {
        "is_read": 1,
        "is_archived": 1,
        "archived_date": now_datetime(),
    })

    return {"notification": notification_name, "archived": True}


# ---------------------------------------------------------------------------
# 3. mark_all_read
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def mark_all_read(notif_type=None):
    """
    Marque en une seule action toutes les notifications actives (non
    consultées) de l'utilisateur comme lues et les archive (7.1).
    Optionnellement restreint à un type de notification donné.
    """
    user = frappe.session.user

    filters = _base_filters(user)
    filters.append(["Notification", "is_archived", "=", 0])
    if notif_type:
        if notif_type not in NOTIFICATION_TYPES:
            frappe.throw(_("Type de notification invalide."))
        filters.append(["Notification", "type", "=", notif_type])

    to_update = frappe.get_all("Notification", filters=filters, pluck="name")
    if not to_update:
        return {"updated_count": 0}

    frappe.db.set_value(
        "Notification",
        {"name": ["in", to_update]},
        {"is_read": 1, "is_archived": 1, "archived_date": now_datetime()},
    )

    return {"updated_count": len(to_update)}


# ---------------------------------------------------------------------------
# 4. get_notification_history
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_notification_history(notif_type=None, search=None, page=1, page_size=20):
    """
    Historique des notifications archivées (7.2) : liste chronologique
    décroissante, filtrable par type (relances de devis, alertes PQI,
    demandes de rattachement, notifications de projet, etc.), avec
    recherche textuelle sur le titre/le message (ex : nom de projet ou
    d'agence concerné). Le statut de lecture reste conservé ("lue").
    """
    user = frappe.session.user
    page = int(page)
    page_size = int(page_size)

    filters = _base_filters(user)
    filters.append(["Notification", "is_archived", "=", 1])
    if notif_type:
        if notif_type not in NOTIFICATION_TYPES:
            frappe.throw(_("Type de notification invalide."))
        filters.append(["Notification", "type", "=", notif_type])
    if search:
        # Recherche textuelle OR sur titre/message : Frappe applique un ET
        # entre les filtres de `get_all`, donc on repasse par une requête
        # SQL directe pour ce cas plutôt que d'empiler des filtres get_all.
        # Recherche OR sur titre et message : on repasse par une requête SQL
        # directe pour ne pas restreindre indûment via un ET implicite.
        agency = _get_active_agency(user)
        conditions = ["user = %(user)s", "is_archived = 1"]
        values = {"user": user, "search": f"%{search}%"}
        if notif_type:
            conditions.append("type = %(type)s")
            values["type"] = notif_type
        if agency:
            conditions.append("(agency_context IS NULL OR agency_context = '' OR agency_context = %(agency)s)")
            values["agency"] = agency
        conditions.append("(title LIKE %(search)s OR message LIKE %(search)s)")

        where_clause = " AND ".join(conditions)
        offset = (page - 1) * page_size

        notifications = frappe.db.sql(
            f"""
            SELECT name, type, title, message, link, reference_doctype,
                   reference_name, agency_context, action_required, is_read,
                   created_date, archived_date
            FROM `tabNotification`
            WHERE {where_clause}
            ORDER BY created_date DESC
            LIMIT %(page_size)s OFFSET %(offset)s
            """,
            {**values, "page_size": page_size, "offset": offset},
            as_dict=True,
        )
        total_count = frappe.db.sql(
            f"SELECT COUNT(*) AS cnt FROM `tabNotification` WHERE {where_clause}",
            values, as_dict=True,
        )[0].cnt
    else:
        notifications = frappe.get_all(
            "Notification",
            filters=filters,
            fields=[
                "name", "type", "title", "message", "link", "reference_doctype",
                "reference_name", "agency_context", "action_required", "is_read",
                "created_date", "archived_date",
            ],
            order_by="created_date desc",
            start=(page - 1) * page_size,
            page_length=page_size,
        )
        total_count = frappe.db.count("Notification", filters=filters)

    return {
        "notifications": notifications,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
    }


# ---------------------------------------------------------------------------
# 5. get_notification_count
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_notification_count():
    """
    Retourne le nombre de notifications actives (badge cloche, 7.1),
    avec un détail du nombre nécessitant une action de la part de
    l'utilisateur (action_required=1), utile pour distinguer les alertes
    urgentes (ex : devis en attente de réponse) des simples informations.
    """
    user = frappe.session.user

    filters = _base_filters(user)
    filters.append(["Notification", "is_archived", "=", 0])

    total_active = frappe.db.count("Notification", filters=filters)

    action_required_filters = filters + [["Notification", "action_required", "=", 1]]
    action_required_count = frappe.db.count("Notification", filters=action_required_filters)

    return {
        "total_active": total_active,
        "action_required_count": action_required_count,
    }