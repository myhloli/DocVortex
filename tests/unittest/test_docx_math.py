from __future__ import annotations

import pytest
from lxml import etree

from docvortex.render._internal.docx.math import DocxFormulaError, latex_to_omml, split_formula_tag

_OFFICE_MATH_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/math"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (r"x + y\tag{1}", ("x + y", "1")),
        (r"x + y\tag { A_{n} }  ", ("x + y", "A_{n}")),
        (r"x + y\tag{\mathrm{A}_{n}}", ("x + y", r"\mathrm{A}_{n}")),
        (r"x + y\tag{\{1\}}", ("x + y", r"\{1\}")),
    ],
)
def test_split_formula_tag_strips_only_balanced_terminal_tag(
    content: str,
    expected: tuple[str, str | None],
) -> None:
    """Validate end tag supports whitespace, nested braces, and escaped braces."""
    assert split_formula_tag(content) == expected


@pytest.mark.parametrize(
    "content",
    [
        r"x + y\tag{1} + z",
        r"x + y\tag{1",
        r"x + y\tag{1}}",
        r"x + y\tagged{1}",
        r"x + y\\tag{1}",
        "plain formula  ",
    ],
)
def test_split_formula_tag_preserves_non_terminal_or_malformed_content(content: str) -> None:
    """Verify that non-terminal, corrupted, or non-command tag text is not accidentally stripped."""
    assert split_formula_tag(content) == (content, None)


def test_latex_to_omml_returns_inline_equation_with_bound_namespace() -> None:
    """Verify that the inline formula returns a m:oMath node that is independently serializable."""
    equation = latex_to_omml(r"x^2 + \frac{a}{b}", display=False)

    assert equation.tag == etree.QName(_OFFICE_MATH_NAMESPACE, "oMath")
    assert equation.getparent() is None
    assert equation.find(f".//{{{_OFFICE_MATH_NAMESPACE}}}f") is not None
    reparsed = etree.fromstring(etree.tostring(equation))
    assert reparsed.tag == equation.tag


def test_latex_to_omml_wraps_display_matrix_in_math_paragraph() -> None:
    """The verification block formula is wrapped in m:oMathPara and retains the matrix OMML."""
    paragraph = latex_to_omml(r"\begin{matrix}a&b\\c&d\end{matrix}", display=True)

    assert paragraph.tag == etree.QName(_OFFICE_MATH_NAMESPACE, "oMathPara")
    equations = paragraph.findall(f"{{{_OFFICE_MATH_NAMESPACE}}}oMath")
    assert len(equations) == 1
    assert equations[0].find(f".//{{{_OFFICE_MATH_NAMESPACE}}}m") is not None


@pytest.mark.parametrize("latex", [r"\bar p", r"\vec{u}"])
def test_latex_to_omml_repairs_group_character_property_closing_tag(latex: str) -> None:
    """Verify that horizontal lines and vector symbols do not degrade into text due to incorrect closing tags in third-party libraries."""
    equation = latex_to_omml(latex, display=False)

    assert equation.find(f".//{{{_OFFICE_MATH_NAMESPACE}}}groupChr") is not None
    etree.fromstring(etree.tostring(equation))


def test_latex_to_omml_hides_square_root_degree_placeholder() -> None:
    """Verify that ordinary square roots contain hidden degree and prevent Word/LibreOffice from displaying the placeholder."""
    equation = latex_to_omml(r"\sqrt{x}", display=False)

    radical = equation.find(f".//{{{_OFFICE_MATH_NAMESPACE}}}rad")
    assert radical is not None
    assert radical.find(f"{{{_OFFICE_MATH_NAMESPACE}}}deg") is not None
    degree_hidden = radical.find(f"{{{_OFFICE_MATH_NAMESPACE}}}radPr/{{{_OFFICE_MATH_NAMESPACE}}}degHide")
    assert degree_hidden is not None
    assert degree_hidden.get(f"{{{_OFFICE_MATH_NAMESPACE}}}val") == "1"


@pytest.mark.parametrize("latex", [r"^{2}", r"_{0}"])
def test_latex_to_omml_uses_zero_width_script_base(latex: str) -> None:
    """Verify that subscripts and subscripts without an explicit base use zero-width characters to suppress visible boxes."""
    equation = latex_to_omml(latex, display=False)
    base = equation.find(f".//{{{_OFFICE_MATH_NAMESPACE}}}sSup/{{{_OFFICE_MATH_NAMESPACE}}}e")
    if base is None:
        base = equation.find(f".//{{{_OFFICE_MATH_NAMESPACE}}}sSub/{{{_OFFICE_MATH_NAMESPACE}}}e")

    assert base is not None
    assert "\u200b" in "".join(base.itertext())


def test_latex_to_omml_removes_explicitly_empty_operator_limits() -> None:
    """Verify that empty upper and lower bounds do not produce visible placeholders next to integrals or other operators."""
    equation = latex_to_omml(r"\int_{}^{} x + E_{}", display=False)
    serialized = etree.tostring(equation, encoding="unicode")

    assert "<m:t></m:t>" not in serialized
    assert "<m:sub>" not in serialized and "<m:sup>" not in serialized


def test_latex_to_omml_normalizes_no_bar_genfrac_in_binomial_formula() -> None:
    """Verify that Office fractions without horizontal lines can be converted to double lines with delimiters OMML matrix."""
    formula = (
        r"\left(x+a\right)^{n}=\sum_{k=0}^{n}"
        r"\left(\genfrac{}{}{0pt}{}{n}{k}\right)x^{k}a^{n-k}"
    )

    equation = latex_to_omml(formula, display=False)
    matrix = equation.find(f".//{{{_OFFICE_MATH_NAMESPACE}}}m")
    delimiter = equation.find(f".//{{{_OFFICE_MATH_NAMESPACE}}}d")

    assert matrix is not None
    assert delimiter is not None
    rows = matrix.findall(f"{{{_OFFICE_MATH_NAMESPACE}}}mr")
    assert ["".join(row.itertext()) for row in rows] == ["n", "k"]


@pytest.mark.parametrize(
    ("latex", "expected_matrix_count"),
    [
        (r"\genfrac{}{}{0pt}{}{a_{i}}{\frac{b}{c}}", 1),
        (r"\genfrac{}{}{0pt}{}{a}{b}+\genfrac{}{}{0pt}{}{c}{d}", 2),
        (r"\genfrac{}{}{0pt}{}{\genfrac{}{}{0pt}{}{a}{b}}{c}", 2),
    ],
)
def test_latex_to_omml_normalizes_nested_and_multiple_genfrac(
    latex: str,
    expected_matrix_count: int,
) -> None:
    """Verify that genfrac parameters can contain nested structures and that one formula can convert multiple instances."""
    equation = latex_to_omml(latex, display=False)

    assert len(equation.findall(f".//{{{_OFFICE_MATH_NAMESPACE}}}m")) == expected_matrix_count


@pytest.mark.parametrize(
    "latex",
    [
        r"\frac{a",
        r"\genfrac{(}{)}{1pt}{}{a}{b}",
        "x\x00y",
    ],
)
def test_latex_to_omml_wraps_conversion_failures_with_original_cause(latex: str) -> None:
    """Retain the original exception chain when validation fails on corrupted or non-canonical input."""
    with pytest.raises(DocxFormulaError, match="无法转换为 OMML") as exc_info:
        latex_to_omml(latex, display=True)

    assert exc_info.value.__cause__ is not None
