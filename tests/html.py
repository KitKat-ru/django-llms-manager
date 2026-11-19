from html.parser import HTMLParser


class PublicationFormParser(HTMLParser):
    """Read publication form fields and the selected submit button from HTML."""

    def __init__(self, button: str) -> None:
        super().__init__()
        self.button = button
        self.fields: dict[str, str] = {}
        self.action = ""
        self.method = ""
        self.form_found = False
        self.button_found = False
        self.in_form = False
        self.select_name: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self.in_form = attributes.get("id") == "publication_form"
            if self.in_form:
                self.form_found = True
                self.action = attributes.get("action") or ""
                self.method = (attributes.get("method") or "get").lower()
            return

        if not (self.in_form or attributes.get("form") == "publication_form"):
            return
        if "disabled" in attributes:
            return

        name = attributes.get("name")
        if tag == "select":
            self.select_name = name
        elif tag == "option" and self.select_name:
            if "selected" in attributes or self.select_name not in self.fields:
                self.fields[self.select_name] = attributes.get("value") or ""
        elif tag in ("input", "button") and name:
            control_type = attributes.get("type") or (
                "submit" if tag == "button" else "text"
            )
            if control_type not in ("text", "hidden", "submit"):
                return
            if control_type == "submit":
                if name != self.button:
                    return
                self.button_found = True
            self.fields[name] = attributes.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.in_form = False
        elif tag == "select":
            self.select_name = None
