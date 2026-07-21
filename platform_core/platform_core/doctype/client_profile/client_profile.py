import re

import frappe
from frappe.model.document import Document
from frappe.utils import nowdate


class ClientProfile(Document):
    """Logique cœur Frappe : validation d'identité légale, score de confiance
    et complétion de profil (règles déterministes, cf. CDC 1.1). Aucune logique
    de matching/IA ici — le microservice de matching lit ces champs via API.
    """

    def before_insert(self):
        user_roles = frappe.get_roles(frappe.session.user)
        if "Client" not in user_roles and "Administrator" not in user_roles:
            frappe.throw("Seuls les Clients et Administrateurs peuvent créer un profil Entreprise.")

    def validate(self):
        self._validate_legal_id()
        self._calculate_trust_score()
        self._calculate_profile_completion()

        if not self.account_seniority:
            self.account_seniority = nowdate()

    def _validate_legal_id(self):
        """Validation format legal_id selon CountryLegalIDRule.validation_regex (CDC 1.1)."""
        if not self.country or not self.legal_id:
            return

        # Récupérer la règle pour le pays
        rules = frappe.get_all(
            "CountryLegalIDRule",
            filters={"country": self.country, "is_active": 1},
            fields=["name", "validation_regex", "id_label"],
            limit=1,
        )
        if not rules:
            return

        rule = rules[0]

        # Valider le format avec le regex
        if rule.validation_regex and not re.match(rule.validation_regex, self.legal_id or ""):
            frappe.throw(f"Format d'identifiant légal invalide pour {rule.id_label or 'ce pays'}.")

        # Vérifier les doublons
        existing = frappe.db.exists("ClientProfile", {
            "legal_id": self.legal_id,
            "country": self.country,
            "name": ["!=", self.name or ""]
        })
        if existing:
            frappe.throw(f"Un profil avec l'identifiant {self.legal_id} existe déjà pour ce pays.")

        self.legal_id_label = rule.id_label

    def _calculate_trust_score(self):
        """Score de confiance Entreprise (CDC 1.1) — règle déterministe basée sur
        des champs de complétion/vérification, distincte du matching IA."""
        score = 0

        if self.legal_id_verified:
            score += 30
        if self.phone_verified:
            score += 20
        if self.logo:
            score += 10
        if self.company_name:
            score += 10
        if self.sector:
            score += 10
        if (self.projects_published_count or 0) >= 5:
            score += 10

        # Bonus : ancienneté du compte (> 6 mois)
        if self.account_seniority:
            from frappe.utils import date_diff
            days = date_diff(nowdate(), self.account_seniority)
            if days >= 180:
                score += 10

        self.trust_score = min(score, 100)

    def _calculate_profile_completion(self):
        weights = {
            "first_name": 10,
            "last_name": 10,
            "company_name": 15,
            "phone": 10,
            "logo": 15,
            "country": 10,
            "legal_id": 15,
            "sector": 15,
        }
        self.profile_completion = min(
            sum(weight for field, weight in weights.items() if getattr(self, field, None)),
            100
        )
