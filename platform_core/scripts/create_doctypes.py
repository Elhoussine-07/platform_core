import frappe

MODULE = "platform_core"

CUSTOM_ROLES = ["Moderator", "Client", "Agency"]


def ensure_role(role_name):
    if frappe.db.exists("Role", role_name):
        return
    frappe.get_doc({
        "doctype": "Role",
        "role_name": role_name,
        "desk_access": 1,
    }).insert(ignore_permissions=True)
    print(f"✓ Rôle {role_name} créé")


def f(fieldname, label, fieldtype, options=None, default=None,
      reqd=0, unique=0, read_only=0, permlevel=0):
    """Raccourci pour définir un champ de DocType."""
    d = {
        "fieldname": fieldname,
        "label": label,
        "fieldtype": fieldtype,
    }
    if options is not None:
        d["options"] = options
    if default is not None:
        d["default"] = default
    if reqd:
        d["reqd"] = 1
    if unique:
        d["unique"] = 1
    if read_only:
        d["read_only"] = 1
    if permlevel:
        d["permlevel"] = permlevel
    return d


def p(role, read=1, write=0, create=0, delete=0, if_owner=0, permlevel=0):
    """Raccourci pour définir une ligne de permission."""
    return {
        "role": role,
        "permlevel": permlevel,
        "read": read,
        "write": write,
        "create": create,
        "delete": delete,
        "if_owner": if_owner,
    }


def create_doctype(name, fields, permissions=None, istable=0, is_single=0):

    if frappe.db.exists("DocType", name):
        print(f"✓ {name} existe déjà")
        return

    doc = frappe.get_doc({
        "doctype": "DocType",
        "name": name,
        "module": MODULE,
        "custom": 0,
        "istable": istable,
        "issingle": is_single,
        "track_changes": 1,
        "engine": "InnoDB",
        "fields": fields,
        "permissions": permissions or [],
    })

    doc.insert(ignore_permissions=True)

    print(f"✓ {name} créé")


def execute():

    # Rôles métier (au cas où non déjà créés)
    for role in CUSTOM_ROLES:
        ensure_role(role)

    ###########################################################
    # GROUPE 7 : AVIS & SCORES (suite)
    ###########################################################

    # 31. PQICriterion
    create_doctype(
        "PQICriterion",
        [
            f("criterion_code", "Code du critère", "Data", reqd=1, unique=1),
            f("label", "Libellé", "Data", reqd=1),
            f("description", "Description / exemple de pénalité", "Text"),
            f("weight", "Poids", "Float", default=1, reqd=1),
            f("is_active", "Actif", "Check", default=1),
        ],
        [
            p("System Manager", read=1, write=1, create=1, delete=1),
            p("Moderator"),
            p("Agency"),
        ],
    )

    # 32. PQIEvaluation
    create_doctype(
        "PQIEvaluation",
        [
            f("agency", "Agence", "Link", options="AgencyProfile", reqd=1),
            f("evaluation_date", "Date d'évaluation", "Datetime", default="Now", read_only=1, permlevel=1),
            f("results", "Résultats", "Table", options="PQICriterionResult", permlevel=1),
            f("total_score", "Score total", "Int", default=0, read_only=1, permlevel=1),
        ],
        [
            p("System Manager", read=1, write=1, create=1, delete=1),
            p("System Manager", read=1, write=1, permlevel=1),
            p("Moderator"),
            p("Agency", if_owner=1),
        ],
    )

    # 33. ImprovementAlert
    create_doctype(
        "ImprovementAlert",
        [
            f("agency", "Agence", "Link", options="AgencyProfile", reqd=1),
            f("alert_type", "Type d'alerte", "Select", reqd=1, options="\n".join([
                "Visibilité en baisse",
                "Critère PQI à corriger",
            ])),
            f("message", "Message", "Small Text", reqd=1),
            f("status", "Statut", "Select", default="Open", reqd=1, options="\n".join([
                "Open",
                "Resolved",
            ])),
        ],
        [
            p("System Manager", read=1, write=1, create=1, delete=1),
            p("Moderator"),
            p("Agency", read=1, write=1, if_owner=1),
        ],
    )

    ###########################################################
    # GROUPE 8 : PROSPECTION & ACTIVITÉ
    ###########################################################

    # 34. AgencyActivity
    create_doctype(
        "AgencyActivity",
        [
            f("client", "Client", "Link", options="ClientProfile", reqd=1),
            f("agency", "Agence", "Link", options="AgencyProfile", reqd=1),
            f("action_type", "Type d'action", "Select", reqd=1, options="\n".join([
                "profile_view",
                "portfolio_view",
                "reviews_view",
                "team_view",
                "certificates_view",
                "add_favorite",
                "send_message",
                "request_quote",
                "share_profile",
            ])),
            f("action_details", "Détails", "JSON"),
            f("time_spent", "Temps passé (secondes)", "Int", default=0),
            f("created_date", "Date", "Datetime", read_only=1),
        ],
        [
            p("System Manager", read=1, write=1, create=1, delete=1),
            p("Moderator"),
            p("Agency", if_owner=1),
        ],
    )

    # 35. VisitorLog
    create_doctype(
        "VisitorLog",
        [
            f("agency", "Agence", "Link", options="AgencyProfile", reqd=1),
            f("ip_hash", "Empreinte IP (hash)", "Data", reqd=1, permlevel=1),
            f("lead", "Lead consolidé", "Link", options="DetectedLead"),
            f("company_name", "Entreprise", "Data"),
            f("country", "Pays", "Data"),
            f("city", "Ville", "Data"),
            f("page_url", "Page visitée", "Data"),
            f("referrer", "Référent", "Data"),
            f("user_agent", "Navigateur", "Data"),
            f("visit_date", "Date de visite", "Datetime", read_only=1),
            f("points_awarded", "Points attribués", "Int", default=0, read_only=1, permlevel=1),
            f("bonus_awarded", "Bonus attribué", "Int", default=0, read_only=1, permlevel=1),
        ],
        [
            p("System Manager", read=1, write=1, create=1, delete=1),
            p("System Manager", read=1, write=1, permlevel=1),
            p("Moderator"),
            p("Agency", if_owner=1),
        ],
    )

    # 36. DetectedLead
    create_doctype(
        "DetectedLead",
        [
            f("agency", "Agence", "Link", options="AgencyProfile", reqd=1),
            f("resolved_company_name", "Entreprise résolue", "Data"),
            f("ip_hash", "Empreinte IP (hash)", "Data", reqd=1, permlevel=1),
            f("cumulative_score", "Score cumulé (fenêtre 7 j)", "Int", default=0, read_only=1, permlevel=1),
            f("classification", "Classification", "Select", default="cold", read_only=1, permlevel=1, options="\n".join([
                "hot",
                "warm",
                "cold",
            ])),
            f("has_favorited", "A ajouté en favori", "Check", default=0, read_only=1, permlevel=1),
            f("first_seen", "Première visite", "Datetime", read_only=1, permlevel=1),
            f("last_seen", "Dernière visite", "Datetime", read_only=1, permlevel=1),
        ],
        [
            p("System Manager", read=1, write=1, create=1, delete=1),
            p("System Manager", read=1, write=1, permlevel=1),
            p("Moderator"),
            p("Agency", if_owner=1),
        ],
    )

    # 37. ProspectionEmail
    create_doctype(
        "ProspectionEmail",
        [
            f("lead", "Lead", "Link", options="DetectedLead", reqd=1),
            f("agency", "Agence", "Link", options="AgencyProfile", reqd=1),
            f("subject", "Objet", "Data", reqd=1),
            f("ai_generated_body", "Corps (généré par IA)", "Text Editor"),
            f("status", "Statut", "Select", default="Draft", reqd=1, options="\n".join([
                "Draft",
                "Sent",
                "Replied",
            ])),
            f("sent_on", "Envoyé le", "Datetime", read_only=1, permlevel=1),
        ],
        [
            p("System Manager", read=1, write=1, create=1, delete=1),
            p("System Manager", read=1, write=1, permlevel=1),
            p("Moderator"),
            p("Agency", read=1, write=1, create=1, delete=1, if_owner=1),
        ],
    )

    ###########################################################
    # GROUPE 9 : NOTIFICATIONS & MODÉRATION
    ###########################################################

    # 38. Notification
    create_doctype(
        "Notification",
        [
            f("user", "Utilisateur", "Link", options="User", reqd=1),
            f("type", "Type", "Select", default="System", reqd=1, options="\n".join([
                "Project",
                "Proposal",
                "Message",
                "Review",
                "System",
                "Alert",
            ])),
            f("title", "Titre", "Data", reqd=1),
            f("message", "Message", "Text Editor", reqd=1),
            f("link", "Lien", "Data"),
            f("reference_doctype", "DocType concerné", "Link", options="DocType"),
            f("reference_name", "Document concerné", "Dynamic Link", options="reference_doctype"),
            f("agency_context", "Contexte agence", "Link", options="AgencyProfile"),
            f("action_required", "Action requise", "Check", default=0),
            f("is_read", "Lue", "Check", default=0),
            f("is_archived", "Archivée", "Check", default=0, permlevel=1),
            f("created_date", "Date de création", "Datetime", read_only=1),
            f("archived_date", "Date d'archivage", "Datetime", read_only=1, permlevel=1),
        ],
        [
            p("System Manager", read=1, write=1, create=1),
            p("System Manager", read=1, write=1, permlevel=1),
            p("Moderator"),
            p("Client", read=1, write=1, if_owner=1),
            p("Agency", read=1, write=1, if_owner=1),
        ],
    )

    # 39. ModerationTask
    create_doctype(
        "ModerationTask",
        [
            f("task_type", "Type de tâche", "Select", reqd=1, options="\n".join([
                "Validation",
                "Suspension",
                "Validation Terminé",
                "Annulation Devis",
                "Rattachement Agence",
                "Litige Client Inactif",
                "Escalade Sans Réponse",
            ])),
            f("reference_doctype", "DocType concerné", "Link", options="DocType", reqd=1),
            f("reference_name", "Document concerné", "Dynamic Link", options="reference_doctype", reqd=1),
            f("status", "Statut", "Select", default="Open", reqd=1, options="\n".join([
                "Open",
                "In Review",
                "Approved",
                "Rejected",
            ])),
            f("moderator", "Modérateur", "Link", options="User"),
            f("decision_note", "Note de décision", "Text"),
            f("decision_date", "Date de décision", "Datetime", read_only=1, permlevel=1),
        ],
        [
            p("System Manager", read=1, write=1, create=1, delete=1),
            p("System Manager", read=1, write=1, permlevel=1),
            p("Moderator", read=1, write=1, create=1),
        ],
    )

    ###########################################################
    # GROUPE 10 : PARAMÈTRES & DÉMONSTRATION
    ###########################################################

    # 40. UserPreference
    create_doctype(
        "UserPreference",
        [
            f("user", "Utilisateur", "Link", options="User", reqd=1, unique=1),
            f("language", "Langue", "Select", default="fr", options="\n".join([
                "fr",
                "en",
                "ar",
            ])),
            f("auto_detect_language", "Détection auto de la langue", "Check", default=1),
            f("theme", "Thème", "Select", default="Clair", options="\n".join([
                "Clair",
                "Sombre",
            ])),
            f("follow_system_theme", "Suivre le thème système", "Check", default=0),
            f("font_family", "Police", "Select", options="\n".join([
                "Default",
                "Inter",
                "Roboto",
                "Open Sans",
            ])),
            f("font_size", "Taille du texte", "Select", default="M", options="\n".join([
                "S",
                "M",
                "L",
            ])),
        ],
        [
            p("System Manager", read=1, write=1, create=1, delete=1),
            p("Moderator", read=1, write=1, create=1, if_owner=1),
            p("Client", read=1, write=1, create=1, if_owner=1),
            p("Agency", read=1, write=1, create=1, if_owner=1),
        ],
    )

    # 41. DemoVideo
    create_doctype(
        "DemoVideo",
        [
            f("title", "Titre", "Data", reqd=1),
            f("account_type", "Type de compte", "Select", default="Les deux", reqd=1, options="\n".join([
                "Client",
                "Agence",
                "Les deux",
            ])),
            f("sequence_order", "Ordre", "Int", default=1, reqd=1),
            f("video_url", "URL de la vidéo", "Data", reqd=1),
            f("duration_seconds", "Durée (secondes)", "Int"),
            f("feature_tag", "Fonctionnalité liée", "Data"),
            f("is_active", "Active", "Check", default=1),
        ],
        [
            p("System Manager", read=1, write=1, create=1, delete=1),
            p("Moderator"),
            p("Client"),
            p("Agency"),
        ],
    )

    # 42. DemoProgress
    create_doctype(
        "DemoProgress",
        [
            f("user", "Utilisateur", "Link", options="User", reqd=1, unique=1),
            f("last_video", "Dernière vidéo vue", "Link", options="DemoVideo"),
            f("completed", "Guide terminé", "Check", default=0),
            f("dismissed_on_first_login", "Reporté à la 1re connexion", "Check", default=0),
        ],
        [
            p("System Manager", read=1, write=1, create=1, delete=1),
            p("Client", read=1, write=1, create=1, if_owner=1),
            p("Agency", read=1, write=1, create=1, if_owner=1),
        ],
    )

    frappe.db.commit()

    print("DocTypes 31 à 42 créés.")
