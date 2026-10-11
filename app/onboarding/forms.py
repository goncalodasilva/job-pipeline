"""Reading submitted form fields, shared by step handlers and the routes (M2.25)."""

from app.onboarding.registry import Form


def first_value(form: Form, name: str) -> str:
    """The field's first value without NUL (which can't appear in HTML); "" when absent."""
    values = form.get(name)
    return values[0].replace("\x00", "") if values else ""
