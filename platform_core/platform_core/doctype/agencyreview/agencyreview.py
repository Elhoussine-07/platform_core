import frappe
from frappe.model.document import Document


class AgencyReview(Document):
    """Logique cœur Frappe : validation d'éligibilité (projet Completed, propriétaire)
    et recalcul déterministe de la note moyenne agence (CDC 1.4 / 2.2.6). Ce n'est
    pas du matching/IA, juste une agrégation arithmétique — reste en Frappe.
    """

    def before_insert(self):
        # Vérifier que le client existe
        if not self.client:
            frappe.throw("Le client est obligatoire pour déposer un avis.")

        if not frappe.db.exists("ClientProfile", self.client):
            frappe.throw(f"Le client {self.client} n'existe pas.")

        # Vérifier que l'agence existe
        if not self.agency:
            frappe.throw("L'agence est obligatoire pour déposer un avis.")

        if not frappe.db.exists("AgencyProfile", self.agency):
            frappe.throw(f"L'agence {self.agency} n'existe pas.")

        # Vérifier que le projet existe
        if not self.project:
            frappe.throw("Un projet doit être associé à l'avis.")

        if not frappe.db.exists("Project", self.project):
            frappe.throw(f"Le projet {self.project} n'existe pas.")

        # Vérifier que le projet est Completed
        project_status = frappe.db.get_value("Project", self.project, "status")
        if project_status != "Completed":
            frappe.throw("Un avis ne peut être déposé que sur un projet au statut Completed.")

        # Vérifier que le client est le propriétaire du projet
        project_client = frappe.db.get_value("Project", self.project, "client")
        if project_client != self.client:
            frappe.throw("Seul le client propriétaire du projet peut déposer cet avis.")

        # Vérifier que la note est valide (1-5)
        if self.rating is None:
            frappe.throw("La note est obligatoire.")

        if self.rating < 1 or self.rating > 5:
            frappe.throw("La note doit être comprise entre 1 et 5.")

        # Vérifier qu'un avis n'a pas déjà été déposé pour ce projet par ce client
        existing = frappe.db.exists("AgencyReview", {
            "project": self.project,
            "client": self.client,
            "name": ["!=", self.name or ""]
        })
        if existing:
            frappe.throw("Un avis a déjà été déposé pour ce projet par ce client.")

    def on_submit(self):
        """Après soumission, recalculer la note de l'agence"""
        self._recalculate_agency_rating()

    def on_update(self):
        """Gère les changements de statut de l'avis"""
        # Récupérer l'ancien statut pour éviter les boucles
        old_status = frappe.db.get_value(self.doctype, self.name, "status") if not self.is_new() else None

        # Si le statut passe à "Approved"
        if self.status == "Approved" and old_status != "Approved":
            # Marquer comme vérifié
            if not self.is_verified:
                frappe.db.set_value(self.doctype, self.name, "is_verified", 1)
            # Recalculer la note de l'agence
            self._recalculate_agency_rating()

        # Si le statut passe à "Rejected" (et qu'il était Approved avant)
        elif self.status == "Rejected" and old_status == "Approved":
            # Recalculer la note de l'agence (l'avis est retiré)
            self._recalculate_agency_rating()

    def _recalculate_agency_rating(self):
        """Recalcule la note moyenne de l'agence à partir des avis approuvés"""
        # Récupérer tous les avis approuvés pour cette agence
        reviews = frappe.get_all(
            "AgencyReview",
            filters={"agency": self.agency, "status": "Approved"},
            fields=["rating"]
        )

        if not reviews:
            # Pas d'avis approuvés → réinitialiser
            frappe.db.set_value("AgencyProfile", self.agency, "rating", 0)
            frappe.db.set_value("AgencyProfile", self.agency, "reviews_count", 0)
            return

        # Calculer la moyenne
        total_ratings = sum(r.rating or 0 for r in reviews)
        average = total_ratings / len(reviews)

        # Mettre à jour le profil de l'agence
        frappe.db.set_value("AgencyProfile", self.agency, "rating", round(average, 1))
        frappe.db.set_value("AgencyProfile", self.agency, "reviews_count", len(reviews))
