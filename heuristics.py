"""High-risk, observation-based selection; never a correctness proof."""
import ast
import copy
import io
import tokenize


def _tokens(text):
    return [
        (token.type, token.string)
        for token in tokenize.generate_tokens(io.StringIO(text.lstrip() + "\n").readline)
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER,
                              tokenize.INDENT, tokenize.DEDENT)
    ]


def identity_change(before, after):
    """Return the old identity operator when only its comparison operator changes."""
    try:
        old, new = _tokens(before), _tokens(after)
    except (tokenize.TokenError, IndentationError):
        return None
    changes = []
    for i, token in enumerate(old):
        if token != (tokenize.NAME, "is"):
            continue
        is_not = i + 1 < len(old) and old[i + 1] == (tokenize.NAME, "not")
        count = 2 if is_not else 1
        replacement = (tokenize.OP, "!=" if is_not else "==")
        if old[:i] + [replacement] + old[i + count:] == new:
            changes.append("IsNot" if is_not else "Is")
    return changes[0] if len(changes) == 1 else None


def ordering_change(before, after):
    try:
        old, new = _tokens(before), _tokens(after)
    except (tokenize.TokenError, IndentationError):
        return None
    alternatives = {"<": ("<=", "Lt"), "<=": ("<", "LtE"),
                    ">": (">=", "Gt"), ">=": (">", "GtE")}
    changes = []
    for i, (kind, token) in enumerate(old):
        if kind == tokenize.OP and token in alternatives:
            replacement, operation = alternatives[token]
            if old[:i] + [(tokenize.OP, replacement)] + old[i + 1:] == new:
                changes.append(operation)
    return changes[0] if len(changes) == 1 else None


def _single_site(files, profile, classifier):
    if len(files) != 1:
        return None
    f = files[0]
    if f["status"] != "modified" or f["binary"] or len(f["groups"]) != 1:
        return None
    removed, added, _, removed_text = f["groups"][0]
    if len(removed) != 1 or len(removed_text) != 1 or len(added) != 1:
        return None
    operator = classifier(removed_text[0], added[0])
    if operator is None:
        return None
    sites = profile["sites"].get(f["old"], {}).get(removed[0], [])
    if len(sites) != 1 or sites[0][1] != operator:
        return None
    return sites[0]


def prune_identity(files, selected, profile, mode):
    """Prune tests based on observed old-versus-new operand outcomes.

    Missing observations and unsupported operand types are optionally treated
    optimistically. None of the modes guarantees generalization.
    """
    site = _single_site(files, profile, identity_change)
    if site is None:
        return selected
    _, _, seen, different, unknown = site
    if mode == "observed":
        removable = seen & ~(different | unknown)
    elif mode == "optimistic":
        removable = ~(different | unknown)
    elif mode == "type-optimistic":
        removable = ~different
    else:
        raise ValueError(f"unknown identity heuristic mode: {mode}")
    return selected & ~removable


def prune_ordering(files, selected, profile, mode):
    site = _single_site(files, profile, ordering_change)
    if site is None:
        return selected
    _, _, seen, different, unknown = site
    if mode == "observed":
        removable = seen & ~(different | unknown)
    elif mode == "optimistic":
        removable = ~(different | unknown)
    else:
        raise ValueError(f"unknown ordering heuristic mode: {mode}")
    return selected & ~removable


def fresh_copy_change(before, after):
    """Heuristically treat copying a just-created argument as low impact."""
    try:
        old, new = ast.parse(before.lstrip()), ast.parse(after.lstrip())
    except SyntaxError:
        return False
    old_calls = [
        node for node in ast.walk(old)
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Call)
        and any(keyword.arg == "copy" for keyword in node.keywords)
    ]
    new_calls = [
        node for node in ast.walk(new)
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Call)
        and any(keyword.arg == "copy" for keyword in node.keywords)
    ]
    if len(old_calls) != 1 or len(new_calls) != 1:
        return False
    old_keywords = [keyword for keyword in old_calls[0].keywords if keyword.arg == "copy"]
    new_keywords = [keyword for keyword in new_calls[0].keywords if keyword.arg == "copy"]
    if len(old_keywords) != 1 or len(new_keywords) != 1:
        return False
    a, b = old_keywords[0].value, new_keywords[0].value
    if not isinstance(a, ast.Constant) or not isinstance(b, ast.Constant):
        return False
    if type(a.value) is not bool or type(b.value) is not bool or a.value == b.value:
        return False
    old_keywords[0].value = copy.deepcopy(b)
    return ast.dump(old) == ast.dump(new)


def prune_fresh_copy(files, selected):
    if len(files) != 1:
        return selected
    f = files[0]
    if f["status"] != "modified" or f["binary"] or len(f["groups"]) != 1:
        return selected
    removed, added, _, removed_text = f["groups"][0]
    if len(removed) == len(removed_text) == len(added) == 1 and fresh_copy_change(removed_text[0], added[0]):
        return 0
    return selected


def mapping_change(before, after):
    """Identify a single value change inside a string-keyed assignment."""
    try:
        old = ast.parse(before.lstrip()).body
        new = ast.parse(after.lstrip()).body
    except SyntaxError:
        return None
    if len(old) != 1 or len(new) != 1:
        return None
    left, right = old[0], new[0]
    if not isinstance(left, ast.Assign) or not isinstance(right, ast.Assign):
        return None
    if len(left.targets) != 1 or not isinstance(left.targets[0], ast.Name):
        return None
    if ast.dump(left.targets[0]) != ast.dump(right.targets[0]):
        return None
    if not isinstance(left.value, ast.Dict) or not isinstance(right.value, ast.Dict):
        return None
    def key_shapes(keys):
        return [ast.dump(key) if key is not None else None for key in keys]

    if key_shapes(left.value.keys) != key_shapes(right.value.keys):
        return None
    if any(key is not None and not (
        isinstance(key, ast.Constant) and isinstance(key.value, str)
    ) for key in left.value.keys):
        return None
    changed = [index for index, (old_value, new_value) in enumerate(
        zip(left.value.values, right.value.values)
    ) if ast.dump(old_value) != ast.dump(new_value)]
    if len(changed) != 1 or left.value.keys[changed[0]] is None:
        return None
    key = left.value.keys[changed[0]].value
    if sum(isinstance(item, ast.Constant) and item.value == key for item in left.value.keys) != 1:
        return None
    return left.targets[0].id, key


def mapping_entry_change(before, after):
    try:
        old = ast.parse("{" + before.strip() + "}", mode="eval").body
        new = ast.parse("{" + after.strip() + "}", mode="eval").body
    except (SyntaxError, ValueError, TypeError):
        return None
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict):
        return None
    if len(old.keys) != 1 or len(new.keys) != 1 or old.keys[0] is None or new.keys[0] is None:
        return None
    if ast.dump(old.keys[0]) != ast.dump(new.keys[0]) or ast.dump(old.values[0]) == ast.dump(new.values[0]):
        return None
    try:
        key = ast.literal_eval(old.keys[0])
    except (ValueError, TypeError):
        return None
    return key if isinstance(key, str) else None


def prune_mapping(files, selected, profile, mode):
    if len(files) != 1:
        return selected
    f = files[0]
    if f["status"] != "modified" or f["binary"] or len(f["groups"]) != 1:
        return selected
    removed, added, _, removed_text = f["groups"][0]
    if len(removed) != 1 or len(removed_text) != 1 or len(added) != 1:
        return selected
    change = mapping_change(removed_text[0], added[0])
    if change is not None:
        name, key = change
        line = removed[0]
    else:
        key = mapping_entry_change(removed_text[0], added[0])
        matches = [item for item in profile["entries"].get(f["old"], {}).get(removed[0], [])
                   if item[2] == key]
        if key is None or len(matches) != 1:
            return selected
        line, name, _ = matches[0]
    site = profile["sites"].get(f["old"], {}).get(line, {}).get(name)
    if site is None:
        return selected
    affected = site["keys"].get(key, 0) | site["whole"]
    if mode == "observed":
        seen = site["whole"]
        for bits in site["keys"].values():
            seen |= bits
        return selected & ~(seen & ~affected)
    if mode == "import-guarded":
        if site["import_read"]:
            return selected
        return selected & affected
    if mode == "optimistic":
        return selected & affected
    raise ValueError(f"unknown mapping heuristic mode: {mode}")
