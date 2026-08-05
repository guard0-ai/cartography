import re
from typing import List


def remove_leading_whitespace_and_empty_lines(text: str) -> str:
    """
    Helper function for tests.
    On the given text string, remove all leading whitespace on each line and remove blank lines,
    :param text: Text string
    :return: The text string but with no leading whitespace and no blank lines.
    """
    # These long-lived snapshot tests verify the non-tenancy shape of generated
    # Cypher. Tenant-boundary assertions live in test_guard0_tenancy.py, so
    # canonicalize the mandatory Guard0 clauses here instead of duplicating every
    # snapshot with security boilerplate.
    normalized = "\n".join(
        [line.lstrip() for line in text.split("\n") if line.lstrip() != ""],
    )
    org_parameter = r"\$GUARD0_ORG_ID"

    # Remove tenant-only WHERE predicates while preserving the first legacy
    # predicate as WHERE.
    normalized = re.sub(
        rf"WHERE\n\w+\.guard0_org_id = {org_parameter}\nAND\n",
        "WHERE\n",
        normalized,
    )
    normalized = re.sub(
        rf"WHERE\n\w+\.guard0_org_id = {org_parameter} AND\n",
        "WHERE\n",
        normalized,
    )
    previous = None
    while previous != normalized:
        previous = normalized
        normalized = re.sub(
            rf"WHERE \w+\.guard0_org_id = {org_parameter}\nAND ",
            "WHERE ",
            normalized,
        )
    normalized = re.sub(
        rf"\nAND \w+\.guard0_org_id = {org_parameter}",
        "",
        normalized,
    )

    # Remove tenant identity/stamps from node maps, relationship maps, and SET
    # clauses. The dedicated tenancy tests assert that the unmodified query has
    # all of these clauses.
    normalized = re.sub(
        rf"guard0_org_id: {org_parameter}, ?\n?",
        "",
        normalized,
    )
    normalized = re.sub(
        rf" \{{guard0_org_id: {org_parameter}\}}",
        "",
        normalized,
    )
    normalized = re.sub(
        rf"\n\w+\.guard0_org_id = {org_parameter},",
        "",
        normalized,
    )

    # Tenant identity makes formerly inline property maps multiline.
    normalized = re.sub(
        r"\{\n([A-Za-z_]\w*: [^\n{}]+)\n\}",
        r"{\1}",
        normalized,
    )
    normalized = re.sub(r"(\w) \{([A-Za-z_]\w*:)", r"\1{\2", normalized)
    return " ".join(normalized.split())


def clean_query_list(queries: List[str]) -> List[str]:
    """
    Helper function to remove leading whitespace and blank lines for all strings in the input list.
    :param queries: The list of strings to clean
    :return: A list of text strings with no leading whitespace and no blank lines.
    """
    return [remove_leading_whitespace_and_empty_lines(query) for query in queries]
