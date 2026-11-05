from __future__ import annotations

from collections.abc import Mapping
from textwrap import dedent

import libcst as cst


class ClassStubEditor:
    """Replace direct class members in stubgen output using its Python syntax tree.

    Other module statements, nested classes, decorators and comments are preserved.
    LibCST handles multiline headers, inline suites and UTF-8 names; this adapter
    only decides which generated declarations replace a model's members.
    """

    @classmethod
    def update(cls, source: str, declarations: Mapping[str, Mapping[str, str]]) -> str:
        module = cst.parse_module(source)
        return module.with_changes(
            body=[
                cls.update_class(statement, declarations[statement.name.value])
                if isinstance(statement, cst.ClassDef) and statement.name.value in declarations
                else statement
                for statement in module.body
            ]
        ).code

    @classmethod
    def update_class(cls, node: cst.ClassDef, declarations: Mapping[str, str]) -> cst.ClassDef:
        """Keep unrelated members and append the generated declarations once."""
        if not declarations:
            return node
        suite = node.body
        if isinstance(suite, cst.SimpleStatementSuite):
            suite = cst.IndentedBlock(
                body=[cst.SimpleStatementLine(body=suite.body, trailing_whitespace=suite.trailing_whitespace)]
            )
        # Parsed class suites are inline or indented; the inline form was converted above.
        assert isinstance(suite, cst.IndentedBlock)  # nosec B101
        body: list[cst.BaseStatement] = []
        comments: list[cst.EmptyLine] = []
        for statement in suite.body:
            if isinstance(statement, cst.FunctionDef) and statement.name.value in declarations:
                comments.extend(statement.leading_lines)
                continue
            if isinstance(statement, cst.SimpleStatementLine):
                members = [
                    replacement
                    for member in statement.body
                    if (replacement := cls.keep_member(member, declarations)) is not None
                ]
                if not members:
                    comments.extend(statement.leading_lines)
                    if statement.trailing_whitespace.comment is not None:
                        comments.append(cst.EmptyLine(comment=statement.trailing_whitespace.comment))
                    continue
                statement = statement.with_changes(body=members)
            body.append(statement)
        generated = list(
            cst.parse_module("\n".join(dedent(text).strip() for text in declarations.values()) + "\n").body
        )
        generated[0] = generated[0].with_changes(leading_lines=(*comments, *generated[0].leading_lines))
        return node.with_changes(body=suite.with_changes(body=[*body, *generated]))

    @staticmethod
    def keep_member(member: cst.BaseSmallStatement, declarations: Mapping[str, str]) -> cst.BaseSmallStatement | None:
        """Remove replaced attributes while retaining other targets of a chained assignment."""
        if isinstance(member, cst.AnnAssign) and isinstance(member.target, cst.Name):
            return None if member.target.value in declarations else member
        if isinstance(member, cst.Assign):
            targets = [
                target
                for target in member.targets
                if not isinstance(target.target, cst.Name) or target.target.value not in declarations
            ]
            return member.with_changes(targets=targets) if targets else None
        if isinstance(member, cst.Pass) or isinstance(member, cst.Expr) and isinstance(member.value, cst.Ellipsis):
            return None
        return member
