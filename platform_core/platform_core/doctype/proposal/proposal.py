from datetime import timedelta

import frappe
from frappe.model.document import Document
from frappe.utils import now, now_datetime

# NOTE : response_deadline (Datetime) est exprimé en HEURES par le CDC (48h, cf. 1.3.3).
# frappe.utils.add_days ne permet pas cette précision ; timedelta est utilisé ici en
# dérogation ciblée, faute d'alternative horaire dans la liste d'API imposée.


class Proposal(Document):
    """Logique cœur Frappe : workflow d'acceptation/devis en deux étapes (CDC 1.3.3).
    Aucune logique de prédiction de succès ici (Opportunity.success_prediction est
    écrit par le microservice de matching via l'API REST Frappe).
    """

    def validate(self):
        if not self.amount or self.amount <= 0:
            frappe.throw("Le montant de l'offre doit être supérieur à 0.")
        self._prevent_duplicate_active_proposal()

    def _prevent_duplicate_active_proposal(self):
        """Empêche les doublons d'offres actives (Sent ou Accepted) pour un même projet et agence"""
        existing = frappe.get_all(
            "Proposal",
            filters={
                "project": self.project,
                "agency": self.agency,
                "status": ["in", ["Sent", "Accepted"]],
                "name": ["!=", self.name or ""],
            },
            limit=1,
        )
        if existing:
            frappe.throw("Une offre Sent ou Accepted existe déjà pour ce projet et cette agence.")

    def before_submit(self):
        # Vérifier que l'utilisateur est une Agence
        user_roles = frappe.get_roles(frappe.session.user)
        if "Agency" not in user_roles and "Administrator" not in user_roles:
            frappe.throw("Seules les Agences et Administrateurs peuvent soumettre une offre.")

        # Vérifier que le projet est au statut "Posted"
        project_status = frappe.db.get_value("Project", self.project, "status")
        if project_status != "Posted":
            frappe.throw("Le projet doit être au statut Posted pour envoyer une offre.")

        # Vérifier que l'agence n'a pas ses offres suspendues
        agency_suspended = frappe.db.get_value("AgencyProfile", self.agency, "offers_suspended")
        if agency_suspended:
            frappe.throw("Cette agence a ses offres suspendues (impayé) et ne peut pas soumettre d'offre.")

        self.submitted_date = now()
        self._set_response_deadline()

    def _set_response_deadline(self):
        """Définit la date limite de réponse (48h par défaut, configurable)"""
        quote_response_hours = (
            frappe.db.get_single_value("PlatformSettings", "quote_response_hours") or 48
        )
        self.response_deadline = now_datetime() + timedelta(hours=quote_response_hours)

    def on_submit(self):
        """Lors de la soumission de l'offre : met à jour l'opportunité et notifie le client"""
        self._update_opportunity(status="Devis envoyé")
        self._notify_client()

    def on_cancel(self):
        """Lors de l'annulation de l'offre : archive l'opportunité"""
        self._update_opportunity(status="Archivée")

    def _update_opportunity(self, status):
        """Met à jour le statut de l'opportunité associée"""
        opportunity = frappe.get_all(
            "Opportunity", filters={"project": self.project, "agency": self.agency}, limit=1
        )
        if opportunity:
            frappe.db.set_value("Opportunity", opportunity[0].name, "status", status)

    def _notify_client(self):
        """Notifie le client qu'un devis a été envoyé"""
        client = frappe.db.get_value("Project", self.project, "client")
        if not client:
            return

        client_user = frappe.db.get_value("ClientProfile", client, "user")
        if not client_user:
            return

        frappe.get_doc(
            {
                "doctype": "Notification",
                "user": client_user,
                "type": "Proposal",
                "title": "Nouveau devis reçu",
                "message": f"Un devis de {self.amount} a été envoyé pour votre projet.",
                "reference_doctype": "Proposal",
                "reference_name": self.name,
            }
        ).insert(ignore_permissions=True)

    def on_update(self):
        """Gère les changements de statut de l'offre"""
        # Récupérer l'ancien statut
        old_status = frappe.db.get_value(self.doctype, self.name, "status") if not self.is_new() else None

        # Si le statut passe à "Accepted" (client accepte)
        if self.status == "Accepted" and old_status != "Accepted":
            self._handle_acceptance()

        # Si le statut passe à "Refused" (client refuse)
        elif self.status == "Refused" and old_status != "Refused":
            self._handle_refusal()

    def _handle_acceptance(self):
        """Lorsque le client accepte l'offre : met à jour projet et opportunité"""
        # Mettre à jour l'opportunité
        self._update_opportunity(status="Gagnée")

        # Mettre à jour le projet : En cours
        frappe.db.set_value("Project", self.project, "status", "In Progress")

        # Mettre à jour la date de décision
        frappe.db.set_value(self.doctype, self.name, "decision_date", now())

        # Créer une notification pour l'agence
        self._notify_agency("Offre acceptée", f"Votre offre pour le projet a été acceptée.")

        # Générer automatiquement la facture (commission)
        self._create_invoice()

    def _handle_refusal(self):
        """Lorsque le client refuse l'offre : met à jour projet et opportunité"""
        # Mettre à jour l'opportunité
        self._update_opportunity(status="Archivée")

        # Mettre à jour le projet : Rejeté
        frappe.db.set_value("Project", self.project, "status", "Rejected")
        frappe.db.set_value("Project", self.project, "rejection_substatus", "Refusé")

        # Mettre à jour la date de décision
        frappe.db.set_value(self.doctype, self.name, "decision_date", now())

    def _notify_agency(self, title, message):
        """Notifie l'agence propriétaire de l'offre"""
        agency_user = frappe.db.get_value("AgencyProfile", self.agency, "user")
        if not agency_user:
            return

        frappe.get_doc(
            {
                "doctype": "Notification",
                "user": agency_user,
                "type": "Proposal",
                "title": title,
                "message": message,
                "reference_doctype": "Proposal",
                "reference_name": self.name,
            }
        ).insert(ignore_permissions=True)

    def _create_invoice(self):
        """Génère automatiquement une facture de commission lorsque l'offre est acceptée (CDC 2.5.1)"""
        # Récupérer le projet pour obtenir le client
        project = frappe.get_doc("Project", self.project)

        # Récupérer la commission_rate depuis PlatformSettings
        commission_rate = frappe.db.get_single_value("PlatformSettings", "commission_rate") or 10

        # Créer la facture
        frappe.get_doc(
            {
                "doctype": "Invoice",
                "agency": self.agency,
                "project": self.project,
                "proposal": self.name,
                "amount": self.amount,
                "commission_rate": commission_rate,
                "status": "Pending",
                "issue_date": frappe.utils.nowdate(),
                "due_date": frappe.utils.add_days(frappe.utils.nowdate(), 7),
            }
        ).insert(ignore_permissions=True)
