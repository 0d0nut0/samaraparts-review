"""Одинаковые требования при регистрации, смене и сбросе пароля."""

PASSWORD_HINT = "От 8 до 128 символов: заглавная и строчная буквы, цифра и специальный символ."


def password_error(password: str, confirmation: str) -> str | None:
    if not (8 <= len(password) <= 128):
        return PASSWORD_HINT
    if not (any(c.isupper() for c in password) and any(c.islower() for c in password)
            and any(c.isdigit() for c in password) and any(not c.isalnum() and not c.isspace() for c in password)):
        return PASSWORD_HINT
    if password != confirmation:
        return "Пароли не совпадают"
    return None
