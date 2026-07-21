import frappe
from frappe.model.document import Document
from frappe.utils import now


class AgencyActivity(Document):
    """Rôle Frappe : journal brut de l'activité d'un client identifié sur un profil
    agence. Aucun scoring n'est calculé ici — le microservice de prospection (Node.js)
    lit ces enregistrements via l'API REST pour construire ses propres modèles.
    """

    def before_insert(self):
        """Définit la date de création et valide les données"""
        # Définir la date de création
        if not self.created_date:
            self.created_date = now()

        # Valider les relations
        self._validate_relations()

        # Valider les données
        self._validate_data()

    def _validate_relations(self):
        """Vérifie que le client et l'agence existent"""
        if self.client:
            if not frappe.db.exists("ClientProfile", self.client):
                frappe.throw(f"Le client {self.client} n'existe pas.")

        if self.agency:
            if not frappe.db.exists("AgencyProfile", self.agency):
                frappe.throw(f"L'agence {self.agency} n'existe pas.")

    def _validate_data(self):
        """Valide les données de l'activité"""
        # Vérifier que action_type est valide
        valid_actions = [
            "profile_view", "portfolio_view", "reviews_view",
            "team_view", "certificates_view", "add_favorite",
            "send_message", "request_quote", "share_profile"
        ]
        if self.action_type and self.action_type not in valid_actions:
            frappe.throw(f"Type d'action invalide: {self.action_type}")

        # Vérifier que time_spent n'est pas négatif
        if self.time_spent and self.time_spent < 0:
            frappe.throw("Le temps passé ne peut pas être négatif.")

    def on_update(self):
        """Gère les mises à jour (si besoin)"""
        # Action facultative : logging des modifications
        # Le microservice Node.js peut également lire les mises à jour
        pass
