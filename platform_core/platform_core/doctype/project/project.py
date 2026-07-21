import frappe
from frappe.model.document import Document


class Project(Document):
    """Logique cœur Frappe : rattachement client, validations de saisie, workflow
    de statuts (Postulé -> Posted -> En cours -> Suspendu -> Terminé/Rejeté, CDC 1.3.1).
    Le champ shortlist_ia (JSON) est renseigné par le microservice de matching via
    l'API REST Frappe — aucun calcul de shortlist ici.
    """

    def before_insert(self):
        user_roles = frappe.get_roles(frappe.session.user)
        if "Client" not in user_roles and "Administrator" not in user_roles:
            frappe.throw("Seuls les Clients et Administrateurs peuvent créer un projet.")

        client = frappe.get_all("ClientProfile", filters={"user": frappe.session.user}, limit=1)
        if not client:
            frappe.throw("Vous devez être un client pour créer un projet")
        self.client = client[0].name

    def validate(self):
        if self.budget_min and self.budget_max and self.budget_min > self.budget_max:
            frappe.throw("Le budget minimum ne peut pas être supérieur au budget maximum.")

        if self.delivery_delay_days is not None and self.delivery_delay_days <= 0:
            frappe.throw("Le délai de réalisation doit être un nombre de jours positif.")
        # Note : Project n'a pas de champ "deadline" dans le DocType final ; le seul délai
        # porté par ce document est delivery_delay_days, utilisé pour expected_end_date.

    def on_submit(self):
        self.status = "Posted"
        self._increment_client_projects_published_count()

    def _increment_client_projects_published_count(self):
        if not self.client:
            return
        current = frappe.db.get_value("ClientProfile", self.client, "projects_published_count") or 0
        frappe.db.set_value("ClientProfile", self.client, "projects_published_count", current + 1)

    def on_update(self):
        # Récupérer l'ancien statut pour détecter les transitions
        old_status = frappe.db.get_value(self.doctype, self.name, "status") if not self.is_new() else None

        if self.status == "Completed" and old_status != "Completed":
            self._notify_client(
                title="Projet terminé",
                message=f"Le projet « {self.title} » est passé au statut Terminé.",
            )
            # Pas de champ "completion_date" : la date de fin est déjà tracée via
            # expected_end_date et completion_confirmed_by_client /
            # completion_validated_by_moderator (CDC 1.3.1).

        elif self.status == "Awaiting" and old_status != "Awaiting":
            # Le délai de 48h est porté par Proposal.response_deadline (cf. proposal.py),
            # pas par Project. Aucune écriture supplémentaire nécessaire ici.
            self._notify_client(
                title="Devis en attente de réponse",
                message=f"Un devis a été envoyé pour le projet « {self.title} ». "
                "Vous disposez de 48h pour répondre.",
            )

        elif self.status == "In Progress" and old_status != "In Progress":
            # Le projet démarre : définir la date de début
            frappe.db.set_value("Project", self.name, "start_date", frappe.utils.now())
            # Verrouiller le CDC (lecture seule)
            frappe.db.set_value("Project", self.name, "cdc_locked", 1)

        elif self.status == "Suspended" and old_status != "Suspended":
            # Le détail de la suspension (justification, dates, jours cumulés) est porté
            # par ProjectSuspension, lié via son champ "project". Project n'ayant pas de
            # champ "agency" direct, l'agence est retrouvée via l'Opportunity Gagnée.
            self._notify_agency(
                title="Projet suspendu",
                message=f"Le projet « {self.title} » est passé au statut Suspendu.",
            )

        elif self.status == "Rejected" and old_status != "Rejected":
            self._notify_client(
                title="Projet rejeté",
                message=f"Le projet « {self.title} » a été rejeté.",
            )

    def _notify_client(self, title, message):
        if not self.client:
            return
        client_user = frappe.db.get_value("ClientProfile", self.client, "user")
        if not client_user:
            return
        self._create_notification(client_user, "Project", title, message)

    def _notify_agency(self, title, message):
        opportunity = frappe.get_all(
            "Opportunity", filters={"project": self.name, "status": "Gagnée"}, limit=1, fields=["agency"]
        )
        if not opportunity:
            return
        agency_members = frappe.get_all(
            "AgencyMember", filters={"agency": opportunity[0].agency, "status": "Active"}, fields=["user"]
        )
        for member in agency_members:
            self._create_notification(member.user, "Project", title, message)

    def _create_notification(self, user, ntype, title, message):
        # Notification = simple enregistrement de données côté Frappe (cœur métier).
        # L'orchestration avancée (canaux, escalade) reste au microservice notifications.
        frappe.get_doc(
            {
                "doctype": "Notification",
                "user": user,
                "type": ntype,
                "title": title,
                "message": message,
                "reference_doctype": "Project",
                "reference_name": self.name,
            }
        ).insert(ignore_permissions=True)
