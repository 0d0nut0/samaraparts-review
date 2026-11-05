"""Информационные страницы: о нас, контакты и реквизиты, доставка, оплата, гарантия и возврат."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templating import templates

router = APIRouter(tags=["pages"])

PAGES = {
    "about": "О компании",
    "contacts": "Контакты и реквизиты",
    "delivery": "Доставка и самовывоз",
    "payment": "Оплата",
    "warranty": "Гарантия и возврат",
    "privacy": "Политика конфиденциальности",
}


def _page(name: str):
    async def view(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name=f"pages/{name}.html", context={"page_title": PAGES[name], "page": name}
        )
    view.__name__ = f"page_{name}"
    return view


for _name in PAGES:
    router.add_api_route(f"/{_name}", _page(_name), methods=["GET"], response_class=HTMLResponse)
