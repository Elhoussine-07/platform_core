from datetime import timedelta

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, get_datetime, now

# NOTE : le calcul de suspension_days (durée entre validation_date et resume_date,
# cf. CDC 1.3.1) nécessite une soustraction de dates. frappe.utils.add_days ne permet
# pas de calculer une différence ; get_datetime + timedelta sont utilisés ici en
# dérogation ciblée, comme pour Proposal.response_deadline.


class ProjectSuspension(Document):
    """Logique cœur Frappe : validation humaine de la suspension et recalcul du
    délai de réalisation du projet (CDC 1.3.1, précision sur le calcul du délai).
    """

    def validate(self):
        # Vérifier que le projet existe
        if not self.project:
            frappe.throw("Un projet doit être associé à la suspension.")

        if not frappe.db.exists("Project", self.project):
            frappe.throw(f"Le projet {self.project} n'existe pas.")

        # Vérifier la justification
        if not self.justification:
            frappe.throw("Une justification est obligatoire pour toute demande de suspension.")

        # Vérifier les transitions de statut
        self._validate_status_transition()

    def _validate_status_transition(self):
        """Empêche les transitions de statut incohérentes (ex : Resumed sans
        passer par Validated, ou modification après Refused/Resumed)."""
        if not self.is_new():
            previous = frappe.db.get_value(self.doctype, self.name, "status")

            allowed_transitions = {
                "Requested": {"Requested", "Validated", "Refused"},
                "Validated": {"Validated", "Resumed"},
                "Refused": {"Refused"},
                "Resumed": {"Resumed"},
            }
            if previous and self.status not in allowed_transitions.get(previous, {self.status}):
                frappe.throw(f"Transition de statut invalide : {previous} → {self.status}.")

    def on_update(self):
        # Récupérer l'ancien statut
        old_status = frappe.db.get_value(self.doctype, self.name, "status") if not self.is_new() else None

        # Validation de la suspension (par Moderator)
        if self.status == "Validated" and old_status != "Validated":
            self._validate_suspension()

        # Reprise du projet (par le client)
        elif self.status == "Resumed" and old_status != "Resumed":
            self._resume_project()

        # Refus de la suspension (par Moderator)
        elif self.status == "Refused" and old_status != "Refused":
            self._refuse_suspension()

    def _validate_suspension(self):
        """Le projet ne passe réellement en Suspendu qu'au moment où le modérateur
        valide la justification (et non à la demande initiale) — cf. CDC 1.3.1."""
        # Vérifier que l'utilisateur est Moderator ou Administrator
        user_roles = frappe.get_roles(frappe.session.user)
        if "Moderator" not in user_roles and "Administrator" not in user_roles:
            frappe.throw("Seuls les Modérateurs et Administrateurs peuvent valider une suspension.")

        # Définir la date de validation
        frappe.db.set_value(self.doctype, self.name, "validation_date", now())

        # Mettre à jour le modérateur
        frappe.db.set_value(self.doctype, self.name, "moderator", frappe.session.user)

        # Passer le projet en Suspendu
        frappe.db.set_value("Project", self.project, "status", "Suspended")

        # Notifier le client
        self._notify_client(
            title="Projet suspendu",
            message=f"Votre projet a été suspendu suite à votre demande."
        )

        # Notifier l'agence
        self._notify_agency(
            title="Projet suspendu",
            message=f"Le projet a été suspendu par le client."
        )

    def _resume_project(self):
        """Au clic sur « Reprendre » : calcule le nombre de jours de suspension
        (validation_date → resume_date), l'ajoute au cumul du projet, recalcule
        expected_end_date, et repasse le projet En cours (In Progress)."""
        resume_date = now()
        frappe.db.set_value(self.doctype, self.name, "resume_date", resume_date)

        if not self.validation_date:
            frappe.throw("Impossible de reprendre : cette suspension n'a jamais été validée.")

        # Calculer les jours de suspension
        suspension_days = (get_datetime(resume_date) - get_datetime(self.validation_date)).days
        frappe.db.set_value(self.doctype, self.name, "suspension_days", suspension_days)

        # Mettre à jour le projet
        project = frappe.get_doc("Project", self.project)

        # Définir initial_end_date si vide
        if not project.initial_end_date:
            # Calculer initial_end_date à partir de la date de début et du délai
            if project.start_date and project.delivery_delay_days:
                initial_end_date = add_days(project.start_date, project.delivery_delay_days)
                frappe.db.set_value("Project", self.project, "initial_end_date", initial_end_date)

        # Ajouter les jours de suspension au cumul
        new_total_suspension_days = (project.total_suspension_days or 0) + suspension_days
        frappe.db.set_value("Project", self.project, "total_suspension_days", new_total_suspension_days)

        # Recalculer la nouvelle date de fin
        if project.initial_end_date:
            new_expected_end_date = add_days(project.initial_end_date, new_total_suspension_days)
            frappe.db.set_value("Project", self.project, "expected_end_date", new_expected_end_date)

        # Repasser le projet en cours
        frappe.db.set_value("Project", self.project, "status", "In Progress")

        # Notifier le client
        self._notify_client(
            title="Projet repris",
            message=f"Votre projet a été repris. Nouvelle date de fin prévue : {new_expected_end_date}"
        )

        # Notifier l'agence
        self._notify_agency(
            title="Projet repris",
            message=f"Le projet a été repris par le client."
        )

    def _refuse_suspension(self):
        """Refuse la demande de suspension"""
        # Vérifier que l'utilisateur est Moderator ou Administrator
        user_roles = frappe.get_roles(frappe.session.user)
        if "Moderator" not in user_roles and "Administrator" not in user_roles:
            frappe.throw("Seuls les Modérateurs et Administrateurs peuvent refuser une suspension.")

        # Mettre à jour le modérateur
        frappe.db.set_value(self.doctype, self.name, "moderator", frappe.session.user)

        # Notifier le client
        self._notify_client(
            title="Demande de suspension refusée",
            message=f"Votre demande de suspension a été refusée."
        )

    def _notify_client(self, title, message):
        """Notifie le client du projet"""
        project = frappe.get_doc("Project", self.project)
        if not project.client:
            return

        client_user = frappe.db.get_value("ClientProfile", project.client, "user")
        if not client_user:
            return

        frappe.get_doc(
            {
                "doctype": "Notification",
                "user": client_user,
                "type": "Suspension",
                "title": title,
                "message": message,
                "reference_doctype": "ProjectSuspension",
                "reference_name": self.name,
            }
        ).insert(ignore_permissions=True)

    def _notify_agency(self, title, message):
        """Notifie l'agence associée au projet"""
        # Récupérer l'agence via l'opportunité gagnée
        opportunity = frappe.get_all(
            "Opportunity",
            filters={"project": self.project, "status": "Gagnée"},
            limit=1,
            fields=["agency"]
        )
        if not opportunity:
            return

        agency_user = frappe.db.get_value("AgencyProfile", opportunity[0].agency, "user")
        if not agency_user:
            return

        frappe.get_doc(
            {
                "doctype": "Notification",
                "user": agency_user,
                "type": "Suspension",
                "title": title,
                "message": message,
                "reference_doctype": "ProjectSuspension",
                "reference_name": self.name,
            }
        ).insert(ignore_permissions=True)
