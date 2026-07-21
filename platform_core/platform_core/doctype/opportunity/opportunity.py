import frappe
from frappe.model.document import Document
from frappe.utils import now


class Opportunity(Document):
    """Logique cœur Frappe : création et cycle de vie de l'opportunité (CDC 2.3).

    matching_score et success_prediction (Read Only) ne sont jamais calculés ici :
    ils sont écrits par le microservice de matching via l'API REST Frappe.
    """

    def before_insert(self):
        # Vérifier que le projet et l'agence existent
        if not self.project or not self.agency:
            frappe.throw("Une opportunité doit être rattachée à un projet et à une agence.")

        # Vérifier que le projet existe
        if not frappe.db.exists("Project", self.project):
            frappe.throw(f"Le projet {self.project} n'existe pas.")

        # Vérifier que l'agence existe
        if not frappe.db.exists("AgencyProfile", self.agency):
            frappe.throw(f"L'agence {self.agency} n'existe pas.")

        # Empêcher les doublons
        self._prevent_duplicate_opportunity()

        # Définir le statut par défaut
        if not self.status:
            self.status = "Reçue"

        # Déduire la source si non définie
        if not self.source:
            self._infer_source_from_project_channel()

    def _prevent_duplicate_opportunity(self):
        """Empêche la création d'une deuxième opportunité active pour le même
        couple (project, agency) — une seule opportunité doit exister par agence
        contactée pour un projet donné, tant qu'elle n'est pas Archivée."""
        existing = frappe.get_all(
            "Opportunity",
            filters={
                "project": self.project,
                "agency": self.agency,
                "status": ["!=", "Archivée"],
            },
            limit=1,
        )
        if existing:
            frappe.throw("Une opportunité active existe déjà pour ce projet et cette agence.")

    def _infer_source_from_project_channel(self):
        """Project.channel (Smart Briefing / Unicast / Multicast) détermine
        Opportunity.source (Shortlist IA / Unicast / Multicast) — cf. CDC 1.3.2."""
        channel = frappe.db.get_value("Project", self.project, "channel")
        mapping = {
            "Smart Briefing": "Shortlist IA",
            "Unicast": "Unicast",
            "Multicast": "Multicast",
        }
        if channel in mapping:
            self.source = mapping[channel]

    def on_update(self):
        """Gère les changements de statut de l'opportunité"""
        # Récupérer l'ancien statut
        old_status = frappe.db.get_value(self.doctype, self.name, "status") if not self.is_new() else None

        # Si le statut passe à "Gagnée"
        if self.status == "Gagnée" and old_status != "Gagnée":
            self._handle_won()

        # Si le statut passe à "Archivée"
        elif self.status == "Archivée" and old_status != "Archivée":
            self._handle_archiving()

        # Si le statut passe à "Acceptée"
        elif self.status == "Acceptée" and old_status != "Acceptée":
            self._handle_accepted()

    def _handle_won(self):
        """Gère le passage au statut 'Gagnée' (client a accepté)"""
        # Définir la date d'acceptation
        if not self.accepted_on:
            frappe.db.set_value(self.doctype, self.name, "accepted_on", now())

        # Mettre à jour le projet : En cours
        frappe.db.set_value("Project", self.project, "status", "In Progress")

        # Mettre à jour la date de début du projet
        frappe.db.set_value("Project", self.project, "start_date", now())

        # Verrouiller le CDC
        frappe.db.set_value("Project", self.project, "cdc_locked", 1)

        # Notifier l'agence
        self._notify_agency(
            title="Opportunité gagnée",
            message=f"Félicitations ! Vous avez remporté le projet."
        )

    def _handle_accepted(self):
        """Gère le passage au statut 'Acceptée' (agence a accepté)"""
        # Mettre à jour la date d'acceptation si non définie
        if not self.accepted_on:
            frappe.db.set_value(self.doctype, self.name, "accepted_on", now())

        # Notifier le client (via le projet)
        self._notify_client(
            title="Offre acceptée par l'agence",
            message="L'agence a accepté votre projet et prépare un devis."
        )

    def _handle_archiving(self):
        """Gère le passage au statut 'Archivée'"""
        # Définir la date d'archivage
        if not self.archived_on:
            frappe.db.set_value(self.doctype, self.name, "archived_on", now())

        # Vérifier que le motif est présent
        if not self.archive_reason:
            frappe.throw("Un motif d'archivage (archive_reason) est requis pour une opportunité Archivée.")

        # Si l'opportunité est archivée après avoir été Gagnée, mettre à jour le projet
        if frappe.db.get_value(self.doctype, self.name, "status") == "Gagnée":
            frappe.db.set_value("Project", self.project, "status", "Rejected")

    def _notify_agency(self, title, message):
        """Notifie l'agence propriétaire de l'opportunité"""
        agency_user = frappe.db.get_value("AgencyProfile", self.agency, "user")
        if not agency_user:
            return

        frappe.get_doc(
            {
                "doctype": "Notification",
                "user": agency_user,
                "type": "Opportunity",
                "title": title,
                "message": message,
                "reference_doctype": "Opportunity",
                "reference_name": self.name,
            }
        ).insert(ignore_permissions=True)

    def _notify_client(self, title, message):
        """Notifie le client via le projet"""
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
                "type": "Opportunity",
                "title": title,
                "message": message,
                "reference_doctype": "Opportunity",
                "reference_name": self.name,
            }
        ).insert(ignore_permissions=True)
