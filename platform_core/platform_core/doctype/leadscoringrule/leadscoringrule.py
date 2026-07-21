import frappe
from frappe.model.document import Document


class LeadScoringRule(Document):
    """Logique cœur Frappe : intégrité du barème de scoring de prospection
    (CDC 2.6.1). Le calcul du score d'un visiteur/lead à partir de ce barème
    est fait par le microservice de prospection (Node.js), pas ici.
    """

    def validate(self):
        self._validate_required_fields()
        self._validate_points()
        self._prevent_duplicate_active_rule()
        self._validate_bonus_condition()
        self._validate_bonus_syntax()

    def _validate_required_fields(self):
        """Vérifie que les champs obligatoires sont renseignés"""
        if not self.action_code:
            frappe.throw("Le code d'action (action_code) est obligatoire.")

        if self.base_points is None:
            frappe.throw("Les points de base (base_points) sont obligatoires.")

    def _validate_points(self):
        """Vérifie que les points ne sont pas négatifs"""
        if (self.base_points or 0) < 0:
            frappe.throw("Les points de base ne peuvent pas être négatifs.")

        if (self.bonus_points or 0) < 0:
            frappe.throw("Les points bonus ne peuvent pas être négatifs.")

        # Bonus : si bonus_points > 0, bonus_condition doit être remplie
        if (self.bonus_points or 0) > 0 and not self.bonus_condition:
            frappe.throw("Des points bonus sont définis mais aucune condition bonus n'est renseignée.")

    def _prevent_duplicate_active_rule(self):
        """Une seule règle active par action_code, pour éviter toute ambiguïté
        dans le barème utilisé par le moteur de scoring externe."""
        if not self.is_active:
            return

        existing = frappe.get_all(
            "LeadScoringRule",
            filters={
                "action_code": self.action_code,
                "is_active": 1,
                "name": ["!=", self.name or ""],
            },
            limit=1,
        )
        if existing:
            frappe.throw(f"Une règle active existe déjà pour l'action « {self.action_code} ».")

    def _validate_bonus_condition(self):
        """Si des points bonus sont définis, la condition associée doit être
        renseignée, sinon le bonus ne pourrait jamais être appliqué (CDC 2.6.1)."""
        if self.bonus_points and not self.bonus_condition:
            frappe.throw("Une condition bonus (bonus_condition) est requise si bonus_points est défini.")

    def _validate_bonus_syntax(self):
        """Valide la syntaxe de la condition bonus pour éviter les erreurs
        lors de l'application par le microservice Node.js."""
        if not self.bonus_condition:
            return

        # Vérifier que la condition a un format attendu
        # Exemples valides : "duration > 60", "count > 5"
        import re
        patterns = [
            r"^duration\s*>\s*\d+$",  # duration > 60
            r"^count\s*>\s*\d+$",     # count > 5
            r"^duration\s*>=\s*\d+$", # duration >= 60
            r"^count\s*>=\s*\d+$",    # count >= 5
        ]

        is_valid = any(re.match(pattern, self.bonus_condition.strip()) for pattern in patterns)
        if not is_valid:
            frappe.throw(
                f"Format de condition bonus invalide : '{self.bonus_condition}'. "
                "Formats acceptés : 'duration > 60', 'count > 5'"
            )
