"""Deferred shell commands with explicit, individually quoted file bindings."""

from dataclasses import dataclass
import shlex

from gwf.exceptions import WorkflowError


def _parts(template):
    if not isinstance(template, str):
        raise WorkflowError("A command template must be text")
    index = 0
    literal = []
    while index < len(template):
        char = template[index]
        if char in "{}":
            if template[index:index + 2] == char * 2:
                literal.append(char)
                index += 2
                continue
            if char == "{":
                end = template.find("}", index + 1)
                name = template[index + 1:end] if end >= 0 else ""
                if name.isidentifier():
                    yield "".join(literal), name
                    literal = []
                    index = end + 1
                    continue
            raise WorkflowError("Command placeholders must be named identifiers; escape literal braces by doubling them")
        literal.append(char)
        index += 1
    yield "".join(literal), None


@dataclass(frozen=True)
class Command:
    template: str
    bindings: dict

    def render(self, resolve):
        return "".join(text + (shlex.quote(str(resolve(self.bindings[name]))) if name else "")
                       for text, name in _parts(self.template))


def shell(template, **bindings):
    names = {name for _, name in _parts(template) if name is not None}
    if names != bindings.keys():
        raise WorkflowError(f"Command bindings mismatch: missing {sorted(names - bindings.keys())}; "
                            f"unused {sorted(bindings.keys() - names)}")
    return Command(template, dict(bindings))
