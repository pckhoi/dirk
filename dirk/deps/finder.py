import ast
import os
from importlib.machinery import ModuleSpec, PathFinder
import typing

from attr import define, field

from dirk.deps.node import Node, Scopes
from dirk.deps.expr import ExprTemplate


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


@define
class DepsFinder(object):
    paths: typing.List[str]
    module_asts: typing.Dict[str, ast.Module] = field(factory=dict)
    module_nodes: typing.Dict[str, Node] = field(factory=dict)

    def build_module_from_filepath(self, filepath: str) -> Node:
        if not os.path.isfile(filepath):
            raise ValueError("not a file path: %s" % filepath)
        parent, filename = os.path.split(filepath)
        return self.build_module(trim_suffix(filename, ".py"), [parent])

    def find_module(
        self,
        module_name: str,
        parent_module_paths: typing.Union[typing.List[str], None] = None,
    ) -> typing.Union[ModuleSpec, None]:
        parts = module_name.split(".")
        paths = (
            self.paths
            if parent_module_paths is None
            else parent_module_paths + self.paths
        )
        for name in parts:
            spec = PathFinder.find_spec(name, paths)
            if spec is None:
                return None
            paths = getattr(spec, "submodule_search_locations", [])
        return spec

    def parse_module(self, origin: str) -> ast.Module:
        if origin in self.module_asts:
            return self.module_asts[origin]
        with open(origin, "r") as f:
            root = ast.parse(f.read(), os.path.split(origin)[-1])
            self.module_asts[origin] = root
            return root

    def collect_submodules(self, spec: ModuleSpec, node: Node):
        for filename in spec.loader.get_resource_reader().contents():
            if filename == "__init__.py":
                continue
            if filename.endswith(".py"):
                name = trim_suffix(filename, ".py")
                node.children[name] = self.build_module(
                    name, spec.submodule_search_locations
                )
            elif os.path.isdir(
                os.path.join(spec.submodule_search_locations[0], filename)
            ) and os.path.exists(
                os.path.join(
                    spec.submodule_search_locations[0], filename, "__init__.py"
                )
            ):
                node.children[filename] = self.build_module(
                    filename, spec.submodule_search_locations
                )

    def dereference(
        self, spec: ModuleSpec, scopes: Scopes, node: ast.AST
    ) -> typing.Union[Node, None]:
        if isinstance(node, ast.Constant):
            return Node(node, spec)
        elif isinstance(node, ast.Tuple):
            for idx, el in enumerate(node.elts):
                child = self.dereference(spec, scopes, el)
                if child is None:
                    return None
                node.elts[idx] = child.t
            return Node(node, spec)
        else:
            return scopes.dereference(node)

    def build_module(
        self,
        module_name: str,
        parent_module_paths: typing.Union[typing.List[str], None] = None,
    ) -> typing.Union[Node, None]:
        spec = self.find_module(module_name, parent_module_paths)
        if spec is None:
            return None
        if spec.origin in self.module_nodes:
            return self.module_nodes[spec.origin]
        mod = self.parse_module(spec.origin)
        node = self.build_from_ast(spec, mod, spec.submodule_search_locations, Scopes())
        if os.path.split(spec.origin)[-1] == "__init__.py":
            self.collect_submodules(spec, node)
        self.module_nodes[spec.origin] = node
        return node

    def populate_scope(
        self,
        spec: ModuleSpec,
        peer_module_paths: typing.Union[typing.List[str], None],
        scopes: Scopes,
        scope: typing.Dict[str, Node],
        parent_node: ast.AST,
        stmt: ast.stmt,
    ):
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                node = self.build_module(alias.name, peer_module_paths)
                if node is not None:
                    scope[alias.name if alias.asname is None else alias.asname] = node
        elif isinstance(stmt, ast.ImportFrom):
            node = self.build_module(stmt.module, peer_module_paths)
            if node is not None:
                for alias in stmt.names:
                    scope[
                        alias.name if alias.asname is None else alias.asname
                    ] = node.get_descendant(alias.name)
        elif isinstance(stmt, ast.FunctionDef) or isinstance(
            stmt, ast.AsyncFunctionDef
        ):
            # ignore instance methods
            if isinstance(parent_node, ast.ClassDef):
                instmed = True
                for name in stmt.decorator_list:
                    if name.id in ["classmethod", "staticmethod"]:
                        instmed = False
                        break
                if instmed:
                    return
            scope[stmt.name] = Node(stmt, spec)
        elif isinstance(stmt, ast.ClassDef):
            scope[stmt.name] = self.build_from_ast(
                spec, stmt, peer_module_paths, scopes.add_scope(scope)
            )
        elif isinstance(stmt, ast.Assign):
            node = self.dereference(spec, scopes.add_scope(scope), stmt.value)
            if node is None:
                return
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Tuple):
                    if isinstance(node.t, ast.Tuple):
                        for idx, el in enumerate(tgt.elts):
                            if isinstance(el, ast.Name):
                                scope[el.id] = self.build_from_ast(
                                    spec,
                                    node.t.elts[idx],
                                    peer_module_paths,
                                    scopes.add_scope(scope),
                                )
                elif isinstance(tgt, ast.Name):
                    scope[tgt.id] = self.build_from_ast(
                        spec, node.t, peer_module_paths, scopes.add_scope(scope)
                    )

    def build_from_ast(
        self,
        spec: ModuleSpec,
        t: ast.AST,
        peer_module_paths: typing.Union[typing.List[str], None],
        scopes: Scopes,
    ) -> "Node":
        """Builds a tree from an ast"""
        if hasattr(t, "body"):
            children = dict()
            for stmt in t.body:
                self.populate_scope(spec, peer_module_paths, scopes, children, t, stmt)
        else:
            children = None
        return Node(t, spec, children)

    def find_dependencies(
        self,
        filepath: str,
        input_tmpls: typing.List[ExprTemplate],
        output_tmpls: typing.List[ExprTemplate],
    ) -> typing.Tuple[typing.List[str], typing.List[str]]:
        module_node = self.build_module_from_filepath(filepath)
        for stmt in module_node.t.body:
            if is_main_block(stmt):
                return self.scan(
                    stmt,
                    Scopes(),
                    module_node.spec.submodule_search_locations,
                    input_tmpls,
                    output_tmpls,
                )
        raise ModuleParseError("main block not found")

    def scan(
        self,
        node: ast.AST,
        scopes: Scopes,
        peer_module_paths: typing.Union[typing.List[str], None],
        input_tmpls: typing.List[ExprTemplate],
        output_tmpls: typing.List[ExprTemplate],
    ) -> typing.Tuple[typing.List[str], typing.List[str]]:
        inputs, outputs = [], []
        scope = dict()
        for stmt in node.body:
            for t in ast.walk(stmt):
                if isinstance(t, ast.Call):
                    for tmpl in output_tmpls:
                        s = tmpl.match_node(scopes, t)
                        if s is not None:
                            outputs.append(s)
                            break
                    else:
                        for tmpl in input_tmpls:
                            s = tmpl.match_node(scopes, t)
                            if s is not None:
                                inputs.append(s)
                                break
                        else:
                            func = scopes.dereference(t.func)
                            if func is None or not isinstance(func.t, ast.Call):
                                continue
                            ins, outs = self.scan(
                                func.t,
                                scopes.add_scope(scope),
                                func.spec.submodule_search_locations,
                                input_tmpls,
                                output_tmpls,
                            )
                            inputs = inputs + ins
                            outputs = outputs + outs
                else:
                    self.populate_scope(peer_module_paths, scopes, scope, node, stmt)
        return inputs, outputs