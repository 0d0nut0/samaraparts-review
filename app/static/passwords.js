document.querySelectorAll('[data-password]').forEach(password => {
    const form = password.form;
    const confirmation = form.querySelector('[data-password-confirm]');
    const strength = form.querySelector('[data-password-strength]');
    const check = () => {
        const value = password.value;
        const length = Array.from(value).length;
        const rules = [length >= 8 && length <= 128, /\p{Lu}/u.test(value), /\p{Ll}/u.test(value),
            /\p{Nd}/u.test(value), /[^\p{L}\p{N}\s]/u.test(value)];
        const valid = rules.every(Boolean);
        password.setCustomValidity(value && !valid ? '8–128 символов: заглавная и строчная буквы, цифра и специальный символ.' : '');
        confirmation.setCustomValidity(confirmation.value && confirmation.value !== value ? 'Пароли не совпадают' : '');
        strength.textContent = value ? (valid ? 'Пароль соответствует требованиям' : `Выполнено требований: ${rules.filter(Boolean).length} из 5`) : '';
    };
    password.addEventListener('input', check);
    confirmation.addEventListener('input', check);
    form.addEventListener('submit', event => {
        check();
        if (!form.reportValidity()) event.preventDefault();
    });
    form.querySelector('[data-password-toggle]').addEventListener('click', event => {
        const visible = password.type === 'password';
        password.type = visible ? 'text' : 'password';
        event.currentTarget.textContent = visible ? 'Скрыть' : 'Показать';
        event.currentTarget.setAttribute('aria-pressed', String(visible));
    });
});
