import frappe
from frappe.model.document import Document
from frappe.utils import now


class VisitorLog(Document):
    """Rôle Frappe : stockage brut de la visite (résolution IP, page consultée, etc.).

    Le calcul du score cumulé et de la classification chaud/tiède/froid (CDC 2.6.1)
    est délégué au microservice de prospection Node.js, qui lit VisitorLog via l'API
    REST Frappe et écrit lui-même DetectedLead.cumulative_score / classification /
    points_awarded / bonus_awarded. Frappe ne fait ici que garantir l'horodatage.
    """

    def before_insert(self):
        # Définir la date de visite si vide
        if not self.visit_date:
            self.visit_date = now()

        # Valider les données
        self._validate_relations()
        self._validate_data()

    def _validate_relations(self):
        """Vérifie que l'agence existe"""
        if self.agency:
            if not frappe.db.exists("AgencyProfile", self.agency):
                frappe.throw(f"L'agence {self.agency} n'existe pas.")

        # Vérifier que le lead existe si renseigné
        if self.lead:
            if not frappe.db.exists("DetectedLead", self.lead):
                frappe.throw(f"Le lead {self.lead} n'existe pas.")

    def _validate_data(self):
        """Valide les données de la visite"""
        # Vérifier que ip_hash est présent (empreinte IP)
        if not self.ip_hash:
            frappe.throw("L'empreinte IP (ip_hash) est obligatoire.")

        # Vérifier que points_awarded n'est pas négatif
        if self.points_awarded and self.points_awarded < 0:
            frappe.throw("Les points attribués ne peuvent pas être négatifs.")

        # Vérifier que bonus_awarded n'est pas négatif
        if self.bonus_awarded and self.bonus_awarded < 0:
            frappe.throw("Les points bonus attribués ne peuvent pas être négatifs.")

    def on_update(self):
        """Gère les mises à jour (si besoin)"""
        # Action facultative : logging des modifications
        # Le microservice Node.js peut également lire les mises à jour
        pass
