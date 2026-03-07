"""
Generate skeleton versions of the scheduling engine for RL training.

Strips method bodies so the RL agent starts with incomplete code
and must learn which actions restore functionality.
"""
import ast
import re
import textwrap


# Methods to strip and the action that restores them
STRIPPABLE_METHODS = {
    'has_conflict': 'add_conflict_detection',
    'get_conflicts': 'add_conflict_detection',
    'enforce_buffer': 'add_buffer_enforcement',
    'convert_timezone': 'add_timezone_handling',
    'normalize_to_utc': 'add_timezone_handling',
    'display_in_timezone': 'add_timezone_handling',
    'get_available_slots': 'add_availability_masking',
    'get_masked_availability': 'add_availability_masking',
    'create_booking': 'add_booking_creation',
    '_check_daily_limit': 'add_daily_limit',
}


def generate_skeleton(full_engine_code, difficulty=1.0):
    """Generate a skeleton of engine.py with method bodies stripped.

    Args:
        full_engine_code: Complete engine.py source code.
        difficulty: 0.0 = full code (no stripping), 1.0 = all methods stripped.
                    Intermediate values strip a proportional random subset.

    Returns:
        Modified source code with selected method bodies replaced by
        ``raise NotImplementedError()``.
    """
    methods = list(STRIPPABLE_METHODS.keys())

    if difficulty < 1.0:
        import random
        count = max(1, int(len(methods) * difficulty))
        methods = random.sample(methods, count)

    skeleton = full_engine_code
    for method_name in methods:
        skeleton = _strip_method(skeleton, method_name)

    # Verify the skeleton parses
    try:
        ast.parse(skeleton)
    except SyntaxError:
        # If stripping broke syntax, return original
        return full_engine_code

    return skeleton


def _strip_method(source, method_name):
    """Replace a method body with raise NotImplementedError().

    Preserves the def line, decorators, and docstring.
    """
    lines = source.split('\n')
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        # Detect "def method_name("
        if (stripped.startswith('def {}'.format(method_name))
                and '(' in stripped):

            # Determine the indentation of the def line
            def_indent = len(line) - len(stripped)
            body_indent = def_indent + 4

            # Collect decorator lines that precede this def
            # (they're already in result, leave them)

            # Add the def line
            result.append(line)
            i += 1

            # Collect the docstring if present
            docstring_collected = False
            while i < len(lines):
                bline = lines[i]
                bstripped = bline.lstrip()

                if not bstripped or bstripped.startswith('#'):
                    # Empty line or comment right after def — keep
                    result.append(bline)
                    i += 1
                    continue

                # Check for docstring
                if not docstring_collected and (
                    bstripped.startswith('"""') or bstripped.startswith("'''")):
                    # Single-line docstring
                    quote = bstripped[:3]
                    result.append(bline)
                    if bstripped.count(quote) >= 2:
                        docstring_collected = True
                        i += 1
                    else:
                        # Multi-line docstring
                        i += 1
                        while i < len(lines):
                            result.append(lines[i])
                            if quote in lines[i]:
                                docstring_collected = True
                                i += 1
                                break
                            i += 1
                    continue

                # Anything else — this is the body, skip it
                break

            # Skip body lines: everything indented more than def_indent
            while i < len(lines):
                bline = lines[i]
                if not bline.strip():
                    # Empty line — could be between methods
                    # Peek ahead to see if next non-empty line is still body
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines):
                        next_indent = len(lines[j]) - len(lines[j].lstrip())
                        if next_indent > def_indent:
                            i += 1
                            continue
                    break
                current_indent = len(bline) - len(bline.lstrip())
                if current_indent <= def_indent and bline.strip():
                    break
                i += 1

            # Insert the replacement body
            indent_str = ' ' * body_indent
            result.append('{}raise NotImplementedError('
                          '"{} not implemented yet")'.format(
                              indent_str, method_name))
            result.append('')
        else:
            result.append(line)
            i += 1

    return '\n'.join(result)


def get_stripped_methods(skeleton_code):
    """Return set of method names that have NotImplementedError."""
    pattern = re.compile(
        r'def (\w+)\([^)]*\).*?raise NotImplementedError',
        re.DOTALL,
    )
    # Simple approach: find methods containing NotImplementedError
    methods = set()
    for match in re.finditer(r'def (\w+)\(', skeleton_code):
        name = match.group(1)
        # Check if the method body is just NotImplementedError
        pos = match.end()
        rest = skeleton_code[pos:pos + 500]
        if 'NotImplementedError' in rest.split('\n    def ')[0]:
            methods.add(name)
    return methods
