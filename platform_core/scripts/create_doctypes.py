import frappe

MODULE = "platform_core"


def create_doctype(name, fields, istable=0):
    """Crée un DocType s'il n'existe pas déjà."""
    if frappe.db.exists("DocType", name):
        print(f"✓ {name} existe déjà")
        return

    doc = frappe.get_doc({
        "doctype": "DocType",
        "name": name,
        "module": MODULE,
        "custom": 0,
        "istable": istable,
        "track_changes": 1,
        "engine": "InnoDB",
        "fields": fields
    })

    doc.insert(ignore_permissions=True)
    print(f"✓ {name} créé")


def execute():
    ###########################################################
    # 10. AgencyJoinRequest (Standard)
    ###########################################################
    create_doctype(
        "AgencyJoinRequest",
        [
            {"fieldname": "user", "label": "Utilisateur", "fieldtype": "Link", "options": "User", "reqd": 1},
            {"fieldname": "agency", "label": "Agence ciblée", "fieldtype": "Link", "options": "AgencyProfile", "reqd": 1},
            {"fieldname": "context", "label": "Contexte", "fieldtype": "Select", "options": "At Signup\nVia Plus Button", "reqd": 1},
            {"fieldname": "status", "label": "Statut", "fieldtype": "Select", "options": "Pending\nApproved\nRejected", "default": "Pending", "reqd": 1},
            {"fieldname": "decided_by", "label": "Décidé par", "fieldtype": "Link", "options": "User", "read_only": 1, "permlevel": 1},
            {"fieldname": "decision_date", "label": "Date de décision", "fieldtype": "Datetime", "read_only": 1, "permlevel": 1},
            {"fieldname": "rejection_reason", "label": "Motif de refus", "fieldtype": "Small Text"},
        ]
    )

    ###########################################################
    # 11. AgencyService (Child Table)
    ###########################################################
    create_doctype(
        "AgencyService",
        [
            {"fieldname": "service_name", "label": "Nom du service", "fieldtype": "Data", "reqd": 1},
            {"fieldname": "description", "label": "Description", "fieldtype": "Text"},
            {"fieldname": "price_range", "label": "Fourchette de prix", "fieldtype": "Data"},
            {"fieldname": "tech_stack", "label": "Stack technique", "fieldtype": "Data"},
            {"fieldname": "skills", "label": "Compétences", "fieldtype": "Data"},
            {"fieldname": "projects_in_progress", "label": "Projets en cours", "fieldtype": "Int", "default": 0},
        ],
        istable=1
    )

    ###########################################################
    # 12. AgencyPortfolio (Child Table)
    ###########################################################
    create_doctype(
        "AgencyPortfolio",
        [
            {"fieldname": "title", "label": "Titre", "fieldtype": "Data", "reqd": 1},
            {"fieldname": "status", "label": "Statut", "fieldtype": "Select", "options": "Completed\nIn Progress", "default": "Completed"},
            {"fieldname": "image", "label": "Image", "fieldtype": "Attach Image"},
            {"fieldname": "video_url", "label": "Lien vidéo", "fieldtype": "Data"},
            {"fieldname": "result_url", "label": "Lien résultat", "fieldtype": "Data"},
            {"fieldname": "budget", "label": "Budget", "fieldtype": "Currency"},
            {"fieldname": "collaboration_period", "label": "Période de collaboration", "fieldtype": "Data"},
            {"fieldname": "agency_feedback", "label": "Avis de l'agence", "fieldtype": "Text Editor"},
            {"fieldname": "problem_solution", "label": "Problématique/Solution", "fieldtype": "Text Editor"},
            {"fieldname": "client_confirmed", "label": "Projet confirmé", "fieldtype": "Check", "default": 0, "permlevel": 1},
        ],
        istable=1
    )

    ###########################################################
    # 13. AgencyTeam (Child Table)
    ###########################################################
    create_doctype(
        "AgencyTeam",
        [
            {"fieldname": "member", "label": "Membre rattaché", "fieldtype": "Link", "options": "AgencyMember"},
            {"fieldname": "photo", "label": "Photo", "fieldtype": "Attach Image"},
            {"fieldname": "role", "label": "Rôle", "fieldtype": "Data", "reqd": 1},
            {"fieldname": "description", "label": "Description", "fieldtype": "Text Editor"},
            {"fieldname": "history", "label": "Parcours", "fieldtype": "Text Editor"},
            {"fieldname": "linkedin_url", "label": "Lien LinkedIn", "fieldtype": "Data"},
            {"fieldname": "show_linked_agencies", "label": "Agences liées visibles", "fieldtype": "Check", "default": 1},
        ],
        istable=1
    )

    frappe.db.commit()
    print("\n✅ Tous les DocTypes ont été créés avec succès !")


# Exécuter le script
execute()
