"""SharePoint file source — connection settings, crawl scope, extraction.

The connector produces the standard ``extract.duckdb`` contract; what makes it
different from the table-shaped sources is that its rows describe *documents*,
and each row carries the ``scope_id`` its content came from — the unit an admin
grants to a group.
"""
