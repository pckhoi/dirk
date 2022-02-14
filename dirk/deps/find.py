import ast
import os
from importlib.machinery import ModuleSpec
import typing

from dirk.deps.module import Loader, Node, Stack

from dirk.deps.expr import ExprPattern


class ModuleParseError(ValueError):
    pass


def is_name(node, id) -> bool:
    return isinstance(node, ast.Name) and node.id == id


def is_const(node, val) -> bool:
    return isinstance(node, ast.Constant) and node.value == val


def is_main_block(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.If)
        and is_name(stmt.test.left, "__name__")
        and isinstance(stmt.test.ops[0], ast.Eq)
        and is_const(stmt.test.comparators[0], "__main__")
    )


def trim_suffix(s: str, suffix: str) -> str:
    if s.endswith(suffix):
        return s[: -len(suffix)]
    return s


def build_module_from_filepath(loader: Loader, filepath: str) -> Node:
    if not os.path.isfile(filepath):
        if not os.path.isdir(filepath) or not os.path.isfile(
            os.path.join(filepath, "__init__.py")
        ):
            raise ValueError("not a file path: %s" % filepath)
    parent, filename = os.path.split(filepath)
    node = loader.find_module(trim_suffix(filename, ".py"), [parent])
    return node


def find_dependencies(
    loader: Loader,
    filepath: str,
    input_tmpls: typing.List[ExprPattern],
    output_tmpls: typing.List[ExprPattern],
) -> typing.Tuple[typing.List[str], typing.List[str]]:
    module_node = build_module_from_filepath(loader, filepath)
    stack = Stack()
    for stmt in module_node.ast.body:
        if is_main_block(stmt):
            return scan(
                loader,
                stmt,
                module_node.spec,
                stack.push(),
                input_tmpls,
                output_tmpls,
            )
        else:
            loader.populate_scope(module_node.spec, stack, module_node.ast, stmt)
    raise ModuleParseError("main block not found")


def scan(
    loader: Loader,
    node: ast.AST,
    spec: ModuleSpec,
    stack: Stack,
    input_tmpls: typing.List[ExprPattern],
    output_tmpls: typing.List[ExprPattern],
) -> typing.Tuple[typing.List[str], typing.List[str]]:
    inputs, outputs = [], []
    for stmt in node.body:
        loader.populate_scope(spec, stack, node, stmt)
        for t in ast.walk(stmt):
            if isinstance(t, ast.Call):
                for tmpl in output_tmpls:
                    s = tmpl.match_node(stack, t)
                    if s is not None:
                        outputs.append(s)
                        break
                else:
                    for tmpl in input_tmpls:
                        s = tmpl.match_node(stack, t)
                        if s is not None:
                            inputs.append(s)
                            break
                    else:
                        func = stack.dereference(t.func)
                        if func is None or not isinstance(func.ast, ast.FunctionDef):
                            continue
                        ins, outs = scan(
                            loader,
                            func.ast,
                            stack.push(),
                            func.spec.submodule_search_locations,
                            input_tmpls,
                            output_tmpls,
                        )
                        inputs = inputs + ins
                        outputs = outputs + outs
    return inputs, outputs