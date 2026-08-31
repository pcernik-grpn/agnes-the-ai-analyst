"""The exists-but-not-shared 403 page, rendered rather than grepped.

This file exists because of a bug that shipped and survived: the page's
PRIMARY action — "Copy request for your admin" — never worked. Its attributes
were built as ``'id="x" data-pkg-name="' ~ (name | e) ~ '"'``, and Jinja's
``~`` joins through ``markup_join``, which escapes every plain-string operand
as soon as ONE operand is Markup. ``| e`` makes one. So the literal quotes in
the template's own string were escaped and the button shipped as
``id=&#34;copy-access-request&#34;`` — no id at all, so the script that binds
the click never found it.

The existing test asserted that ``copy-access-request`` appeared in the
response body. It did appear, escaped, on every render. A test that greps for
a string its own source contains cannot tell a working button from a broken
one; these assert the parsed ATTRIBUTE instead.
"""

import re

import pytest

from app.web.router import templates


def _render(message: str) -> str:
    return templates.get_template("error.html").render(
        code=403,
        title="Forbidden",
        message=message,
        path="/x",
        request=None,
        config=type("C", (), {"INSTANCE_NAME": "Test"})(),
    )


def _button(html: str) -> str:
    m = re.search(r"<button[^>]*copy-access-request[^>]*>", html)
    assert m, "the copy-request button is missing entirely"
    return m.group(0)


class TestTheButtonIsActuallyAddressable:
    def test_the_id_is_an_id_and_not_an_escaped_string(self):
        btn = _button(_render("not_shared:data package:Finance Core"))
        assert 'id="copy-access-request"' in btn
        assert "&#34;" not in btn, "markup_join escaped the template's own quotes again"

    def test_the_script_can_find_what_it_binds_to(self):
        """The two halves have to agree: the script looks the id up by name."""
        html = _render("not_shared:data package:Finance Core")
        assert 'id="copy-access-request"' in _button(html)
        assert "getElementById('copy-access-request')" in html

    def test_the_kind_and_name_reach_the_message_the_reader_sends(self):
        btn = _button(_render("not_shared:memory domain:Pricing Playbook"))
        assert 'data-pkg-name="Pricing Playbook"' in btn
        assert 'data-pkg-kind="memory domain"' in btn

    def test_a_name_carrying_a_quote_cannot_break_out(self):
        """The values are still escaped — the fix marks the template's own
        fragments safe, not the data interpolated between them."""
        btn = _button(_render('not_shared:data package:Ops " onclick=alert(1) x="'))
        # The payload survives as TEXT inside the value — that is correct and
        # is what escaping looks like. What must not exist is a real attribute:
        # the quote that would end data-pkg-name early is `&#34;`, so the
        # handler never becomes one.
        assert '" onclick=alert' not in btn
        assert "&#34; onclick=alert(1) x=&#34;" in btn


class TestOneDoorForEveryKind:
    @pytest.mark.parametrize(
        "kind,name",
        [
            ("data package", "Finance Core"),
            ("memory domain", "Pricing Playbook"),
            ("table", "orders_daily"),
            ("data app", "Churn Explorer"),
        ],
    )
    def test_every_kind_reads_as_language_with_its_own_noun(self, kind, name):
        """Four routes used to raise three different machine strings for one
        situation. The reader's question is identical in all four — it exists,
        who can share it, and what do I send them — so the page is identical
        and only the noun moves."""
        html = _render(f"not_shared:{kind}:{name}")
        assert "Not shared with you yet" in html
        assert f"The {kind} <b>{name}</b>" in html
        assert "not_shared" not in html, "the machine token must not print"

    def test_the_original_package_only_token_still_reads_as_language(self):
        """`package_not_shared:<name>` is kept as an alias rather than removed:
        a bookmarked or cached 403 must not regress to printing a raw token."""
        html = _render("package_not_shared:Finance Core")
        assert "The data package <b>Finance Core</b>" in html
        assert "package_not_shared" not in html
